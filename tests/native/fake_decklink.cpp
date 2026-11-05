// A fake DeckLink driver (libDeckLinkAPI.so) for testing dsan_decklink without hardware.
//
// The SDK's dispatch code dlopen()s "libDeckLinkAPI.so" and calls
// CreateDeckLinkIteratorInstance_0004(). This library provides that, with three
// pretend devices whose outputs really run scheduled playback on a clock and
// record what was "on air":
//
//   0  "DeckLink Duo 2 (1)"   output, internal + external keying
//   1  "DeckLink Duo 2 (2)"   output, no keyer
//   2  "DeckLink Mini Recorder" input only (no IDeckLinkOutput)
//
// fake_* functions let tests inspect the last displayed frame, keyer state and
// counters. With the external keyer enabled, channel B ("key") output is derived
// from the fill frame's alpha, like the real hardware.
//
// Linux only; built by tests/test_decklink.py.
#include "DeckLinkAPI.h"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#define FAKE_EXPORT extern "C" __attribute__((visibility("default")))

namespace {

bool same(REFIID a, REFIID b) { return std::memcmp(&a, &b, sizeof(REFIID)) == 0; }
const char* dupstr(const char* s) { return strdup(s); }

template <class Base>
struct RefCounted : Base {
    std::atomic<ULONG> refs{1};
    virtual ~RefCounted() {}
    ULONG AddRef() override { return ++refs; }
    ULONG Release() override { ULONG n = --refs; if (!n) delete this; return n; }
};

struct ModeDef { BMDDisplayMode id; const char* name; int w, h; int64_t dur, scale; BMDFieldDominance fd; };
const ModeDef kModes[] = {
    {bmdModeHD1080i5994, "1080i59.94", 1920, 1080, 1001, 30000, bmdUpperFieldFirst},
    {bmdModeHD1080p2997, "1080p29.97", 1920, 1080, 1001, 30000, bmdProgressiveFrame},
    {bmdModeHD720p5994, "720p59.94", 1280, 720, 1001, 60000, bmdProgressiveFrame},
};
const ModeDef* findMode(BMDDisplayMode id) {
    for (auto& m : kModes) if (m.id == id) return &m;
    return nullptr;
}

// ── frames ──
struct Frame final : IDeckLinkMutableVideoFrame, IDeckLinkVideoBuffer {
    std::atomic<ULONG> refs{1};
    int w, h, rb;
    std::vector<uint8_t> bytes;
    Frame(int w_, int h_, int rb_) : w(w_), h(h_), rb(rb_), bytes((size_t)rb_ * h_, 0) {}
    HRESULT QueryInterface(REFIID iid, LPVOID* ppv) override {
        if (same(iid, IID_IDeckLinkVideoBuffer)) { *ppv = static_cast<IDeckLinkVideoBuffer*>(this); AddRef(); return S_OK; }
        if (same(iid, IID_IUnknown) || same(iid, IID_IDeckLinkVideoFrame) || same(iid, IID_IDeckLinkMutableVideoFrame)) {
            *ppv = static_cast<IDeckLinkMutableVideoFrame*>(this); AddRef(); return S_OK;
        }
        *ppv = nullptr; return E_NOINTERFACE;
    }
    ULONG AddRef() override { return ++refs; }
    ULONG Release() override { ULONG n = --refs; if (!n) delete this; return n; }
    long GetWidth() override { return w; }
    long GetHeight() override { return h; }
    long GetRowBytes() override { return rb; }
    BMDPixelFormat GetPixelFormat() override { return bmdFormat8BitBGRA; }
    BMDFrameFlags GetFlags() override { return bmdFrameFlagDefault; }
    HRESULT GetTimecode(BMDTimecodeFormat, IDeckLinkTimecode**) override { return E_NOTIMPL; }
    HRESULT GetAncillaryData(IDeckLinkVideoFrameAncillary**) override { return E_NOTIMPL; }
    HRESULT SetFlags(BMDFrameFlags) override { return S_OK; }
    HRESULT SetTimecode(BMDTimecodeFormat, IDeckLinkTimecode*) override { return E_NOTIMPL; }
    HRESULT SetTimecodeFromComponents(BMDTimecodeFormat, uint8_t, uint8_t, uint8_t, uint8_t, BMDTimecodeFlags) override { return E_NOTIMPL; }
    HRESULT SetAncillaryData(IDeckLinkVideoFrameAncillary*) override { return E_NOTIMPL; }
    HRESULT SetTimecodeUserBits(BMDTimecodeFormat, BMDTimecodeUserBits) override { return E_NOTIMPL; }
    HRESULT SetInterfaceProvider(REFIID, IUnknown*) override { return E_NOTIMPL; }
    HRESULT GetBytes(void** b) override { *b = bytes.data(); return S_OK; }
    HRESULT GetSize(uint64_t* s) override { *s = bytes.size(); return S_OK; }
    HRESULT StartAccess(BMDBufferAccessFlags) override { return S_OK; }
    HRESULT EndAccess(BMDBufferAccessFlags) override { return S_OK; }
};

// ── display modes ──
struct Mode final : RefCounted<IDeckLinkDisplayMode> {
    const ModeDef* d;
    explicit Mode(const ModeDef* m) : d(m) {}
    HRESULT QueryInterface(REFIID, LPVOID* ppv) override { *ppv = nullptr; return E_NOINTERFACE; }
    HRESULT GetName(const char** n) override { *n = dupstr(d->name); return S_OK; }
    BMDDisplayMode GetDisplayMode() override { return d->id; }
    long GetWidth() override { return d->w; }
    long GetHeight() override { return d->h; }
    HRESULT GetFrameRate(BMDTimeValue* dur, BMDTimeScale* sc) override { *dur = d->dur; *sc = d->scale; return S_OK; }
    BMDFieldDominance GetFieldDominance() override { return d->fd; }
    BMDDisplayModeFlags GetFlags() override { return 0; }
};

struct ModeIterator final : RefCounted<IDeckLinkDisplayModeIterator> {
    size_t i = 0;
    HRESULT QueryInterface(REFIID, LPVOID* ppv) override { *ppv = nullptr; return E_NOINTERFACE; }
    HRESULT Next(IDeckLinkDisplayMode** m) override {
        if (i >= sizeof(kModes) / sizeof(kModes[0])) return S_FALSE;
        *m = new Mode(&kModes[i++]);
        return S_OK;
    }
};

// ── per-device recorded state (what the tests look at) ──
struct Recorder {
    std::mutex m;
    std::vector<uint8_t> fillA, keyB;   // last displayed channel A (fill) and B (key) images
    int w = 0, h = 0, rb = 0;
    uint64_t displayed = 0;
    int keyer = 0;                      // 0 off, 1 internal, 2 external
    int level = 0;
    uint32_t enabledMode = 0;
    bool playing = false;
};
Recorder gRec[3];

// ── output ──
struct Output final : IDeckLinkOutput {
    int dev;
    std::atomic<ULONG> refs{1};
    IDeckLinkVideoOutputCallback* cb = nullptr;
    const ModeDef* mode = nullptr;
    std::mutex m;
    std::condition_variable cv;
    std::deque<std::pair<IDeckLinkVideoFrame*, BMDTimeValue>> queue;
    std::thread player;
    bool running = false;
    int64_t streamScale = 0;
    std::chrono::steady_clock::time_point t0;

    explicit Output(int d) : dev(d) {}
    HRESULT QueryInterface(REFIID, LPVOID* ppv) override { *ppv = nullptr; return E_NOINTERFACE; }
    ULONG AddRef() override { return ++refs; }
    ULONG Release() override { ULONG n = --refs; if (!n) delete this; return n; }

    HRESULT DoesSupportVideoMode(BMDVideoConnection, BMDDisplayMode req, BMDPixelFormat pf, BMDVideoOutputConversionMode,
                                 BMDSupportedVideoModeFlags flags, BMDDisplayMode*, bool* ok) override {
        bool keying = flags & bmdSupportedVideoModeKeying;
        *ok = findMode(req) && (pf == bmdFormat8BitBGRA || pf == bmdFormatUnspecified) && (!keying || dev == 0);
        return S_OK;
    }
    HRESULT GetDisplayMode(BMDDisplayMode id, IDeckLinkDisplayMode** out) override {
        const ModeDef* d = findMode(id);
        if (!d) return E_FAIL;
        *out = new Mode(d);
        return S_OK;
    }
    HRESULT GetDisplayModeIterator(IDeckLinkDisplayModeIterator** it) override { *it = new ModeIterator(); return S_OK; }
    HRESULT SetScreenPreviewCallback(IDeckLinkScreenPreviewCallback*) override { return S_OK; }
    HRESULT EnableVideoOutput(BMDDisplayMode id, BMDVideoOutputFlags) override {
        std::lock_guard<std::mutex> l(gRec[dev].m);
        if (gRec[dev].enabledMode) return E_ACCESSDENIED;      // already in use
        mode = findMode(id);
        if (!mode) return E_FAIL;
        gRec[dev].enabledMode = (uint32_t)id;
        return S_OK;
    }
    HRESULT DisableVideoOutput() override {
        std::lock_guard<std::mutex> l(gRec[dev].m);
        gRec[dev].enabledMode = 0;
        return S_OK;
    }
    HRESULT CreateVideoFrame(int32_t w, int32_t h, int32_t rb, BMDPixelFormat, BMDFrameFlags, IDeckLinkMutableVideoFrame** f) override {
        *f = new Frame(w, h, rb);
        return S_OK;
    }
    HRESULT CreateVideoFrameWithBuffer(int32_t, int32_t, int32_t, BMDPixelFormat, BMDFrameFlags, IDeckLinkVideoBuffer*,
                                       IDeckLinkMutableVideoFrame**) override { return E_NOTIMPL; }
    HRESULT RowBytesForPixelFormat(BMDPixelFormat, int32_t w, int32_t* rb) override { *rb = w * 4; return S_OK; }
    HRESULT CreateAncillaryData(BMDPixelFormat, IDeckLinkVideoFrameAncillary**) override { return E_NOTIMPL; }
    HRESULT DisplayVideoFrameSync(IDeckLinkVideoFrame*) override { return E_NOTIMPL; }
    HRESULT ScheduleVideoFrame(IDeckLinkVideoFrame* f, BMDTimeValue t, BMDTimeValue, BMDTimeScale) override {
        f->AddRef();
        std::lock_guard<std::mutex> l(m);
        queue.emplace_back(f, t);
        cv.notify_all();
        return S_OK;
    }
    HRESULT SetScheduledFrameCompletionCallback(IDeckLinkVideoOutputCallback* c) override {
        if (c) c->AddRef();
        if (cb) cb->Release();
        cb = c;
        return S_OK;
    }
    HRESULT GetBufferedVideoFrameCount(uint32_t* n) override { std::lock_guard<std::mutex> l(m); *n = (uint32_t)queue.size(); return S_OK; }
    HRESULT EnableAudioOutput(BMDAudioSampleRate, BMDAudioSampleType, uint32_t, BMDAudioOutputStreamType) override { return E_NOTIMPL; }
    HRESULT DisableAudioOutput() override { return S_OK; }
    HRESULT WriteAudioSamplesSync(void*, uint32_t, uint32_t*) override { return E_NOTIMPL; }
    HRESULT BeginAudioPreroll() override { return E_NOTIMPL; }
    HRESULT EndAudioPreroll() override { return E_NOTIMPL; }
    HRESULT ScheduleAudioSamples(void*, uint32_t, BMDTimeValue, BMDTimeScale, uint32_t*) override { return E_NOTIMPL; }
    HRESULT GetBufferedAudioSampleFrameCount(uint32_t*) override { return E_NOTIMPL; }
    HRESULT FlushBufferedAudioSamples() override { return E_NOTIMPL; }
    HRESULT SetAudioCallback(IDeckLinkAudioOutputCallback*) override { return E_NOTIMPL; }

    void display(IDeckLinkVideoFrame* f) {
        Frame* fr = static_cast<Frame*>(static_cast<IDeckLinkMutableVideoFrame*>(f));
        Recorder& r = gRec[dev];
        std::lock_guard<std::mutex> l(r.m);
        r.w = fr->w; r.h = fr->h; r.rb = fr->rb;
        r.fillA = fr->bytes;
        if (r.keyer == 2) {                    // external keyer: channel B = alpha as luma
            r.keyB.assign(fr->bytes.size(), 0);
            for (size_t i = 0; i + 3 < fr->bytes.size(); i += 4) {
                uint8_t a = (uint8_t)((fr->bytes[i + 3] * r.level) / 255);
                r.keyB[i] = r.keyB[i + 1] = r.keyB[i + 2] = a;
                r.keyB[i + 3] = 255;
            }
        }
        r.displayed++;
    }

    void run() {
        const auto period = std::chrono::microseconds((int64_t)(1e6 * mode->dur / mode->scale));
        auto next = std::chrono::steady_clock::now();
        std::unique_lock<std::mutex> l(m);
        while (running) {
            next += period;
            cv.wait_until(l, next, [this] { return !running; });
            if (!running) break;
            if (queue.empty()) continue;
            auto item = queue.front();
            queue.pop_front();
            l.unlock();
            display(item.first);
            if (cb) cb->ScheduledFrameCompleted(item.first, bmdOutputFrameCompleted);
            item.first->Release();
            l.lock();
        }
    }

    HRESULT StartScheduledPlayback(BMDTimeValue, BMDTimeScale scale, double) override {
        std::lock_guard<std::mutex> l(m);
        if (running) return E_FAIL;
        running = true;
        streamScale = scale;
        t0 = std::chrono::steady_clock::now();
        { std::lock_guard<std::mutex> lr(gRec[dev].m); gRec[dev].playing = true; }
        player = std::thread([this] { run(); });
        return S_OK;
    }
    HRESULT StopScheduledPlayback(BMDTimeValue, BMDTimeValue*, BMDTimeScale) override {
        {
            std::lock_guard<std::mutex> l(m);
            if (!running) return S_OK;
            running = false;
            cv.notify_all();
        }
        if (player.joinable()) player.join();
        std::deque<std::pair<IDeckLinkVideoFrame*, BMDTimeValue>> rest;
        { std::lock_guard<std::mutex> l(m); rest.swap(queue); }
        for (auto& item : rest) {
            if (cb) cb->ScheduledFrameCompleted(item.first, bmdOutputFrameFlushed);
            item.first->Release();
        }
        { std::lock_guard<std::mutex> lr(gRec[dev].m); gRec[dev].playing = false; }
        std::thread([cb = cb] { if (cb) cb->ScheduledPlaybackHasStopped(); }).join();
        return S_OK;
    }
    HRESULT IsScheduledPlaybackRunning(bool* a) override { std::lock_guard<std::mutex> l(m); *a = running; return S_OK; }
    HRESULT GetScheduledStreamTime(BMDTimeScale scale, BMDTimeValue* t, double* speed) override {
        auto el = std::chrono::steady_clock::now() - t0;
        *t = (BMDTimeValue)(std::chrono::duration<double>(el).count() * scale);
        *speed = 1.0;
        return S_OK;
    }
    HRESULT GetReferenceStatus(BMDReferenceStatus* st) override { *st = dev == 0 ? bmdReferenceLocked : 0; return S_OK; }
    HRESULT GetHardwareReferenceClock(BMDTimeScale, BMDTimeValue*, BMDTimeValue*, BMDTimeValue*) override { return E_NOTIMPL; }
    HRESULT GetFrameCompletionReferenceTimestamp(IDeckLinkVideoFrame*, BMDTimeScale, BMDTimeValue*) override { return E_NOTIMPL; }
};

struct Keyer final : RefCounted<IDeckLinkKeyer> {
    int dev;
    explicit Keyer(int d) : dev(d) {}
    HRESULT QueryInterface(REFIID, LPVOID* ppv) override { *ppv = nullptr; return E_NOINTERFACE; }
    HRESULT Enable(bool ext) override { std::lock_guard<std::mutex> l(gRec[dev].m); gRec[dev].keyer = ext ? 2 : 1; return S_OK; }
    HRESULT SetLevel(uint8_t level) override { std::lock_guard<std::mutex> l(gRec[dev].m); gRec[dev].level = level; return S_OK; }
    HRESULT RampUp(uint32_t) override { return S_OK; }
    HRESULT RampDown(uint32_t) override { return S_OK; }
    HRESULT Disable() override { std::lock_guard<std::mutex> l(gRec[dev].m); gRec[dev].keyer = 0; return S_OK; }
};

struct Attributes final : RefCounted<IDeckLinkProfileAttributes> {
    int dev;
    explicit Attributes(int d) : dev(d) {}
    HRESULT QueryInterface(REFIID, LPVOID* ppv) override { *ppv = nullptr; return E_NOINTERFACE; }
    HRESULT GetFlag(BMDDeckLinkAttributeID id, bool* v) override {
        if (id == BMDDeckLinkSupportsInternalKeying || id == BMDDeckLinkSupportsExternalKeying) { *v = dev == 0; return S_OK; }
        return E_NOTIMPL;
    }
    HRESULT GetInt(BMDDeckLinkAttributeID id, int64_t* v) override {
        if (id == BMDDeckLinkDuplex) { *v = bmdDuplexHalf; return S_OK; }
        return E_NOTIMPL;
    }
    HRESULT GetFloat(BMDDeckLinkAttributeID, double*) override { return E_NOTIMPL; }
    HRESULT GetString(BMDDeckLinkAttributeID, const char**) override { return E_NOTIMPL; }
    HRESULT GetStringWithParam(BMDDeckLinkAttributeID, uint64_t, const char**) override { return E_NOTIMPL; }
};

Output* gOutputs[2] = {nullptr, nullptr};

struct Device final : RefCounted<IDeckLink> {
    int dev;
    explicit Device(int d) : dev(d) {}
    HRESULT QueryInterface(REFIID iid, LPVOID* ppv) override {
        *ppv = nullptr;
        if (same(iid, IID_IDeckLinkOutput) && dev < 2) {
            if (!gOutputs[dev]) gOutputs[dev] = new Output(dev);
            gOutputs[dev]->AddRef();
            *ppv = gOutputs[dev];
            return S_OK;
        }
        if (same(iid, IID_IDeckLinkKeyer) && dev == 0) { *ppv = new Keyer(dev); return S_OK; }
        if (same(iid, IID_IDeckLinkProfileAttributes)) { *ppv = new Attributes(dev); return S_OK; }
        return E_NOINTERFACE;
    }
    HRESULT GetModelName(const char** n) override { return GetDisplayName(n); }
    HRESULT GetDisplayName(const char** n) override {
        static const char* names[] = {"DeckLink Duo 2 (1)", "DeckLink Duo 2 (2)", "DeckLink Mini Recorder"};
        *n = dupstr(names[dev]);
        return S_OK;
    }
};

struct Iterator final : RefCounted<IDeckLinkIterator> {
    int i = 0;
    HRESULT QueryInterface(REFIID, LPVOID* ppv) override { *ppv = nullptr; return E_NOINTERFACE; }
    HRESULT Next(IDeckLink** d) override {
        if (i >= 3) return S_FALSE;
        *d = new Device(i++);
        return S_OK;
    }
};

struct ApiInfo final : RefCounted<IDeckLinkAPIInformation> {
    HRESULT QueryInterface(REFIID, LPVOID* ppv) override { *ppv = nullptr; return E_NOINTERFACE; }
    HRESULT GetFlag(BMDDeckLinkAPIInformationID, bool*) override { return E_NOTIMPL; }
    HRESULT GetInt(BMDDeckLinkAPIInformationID, int64_t* v) override { *v = 0x10000000; return S_OK; }
    HRESULT GetFloat(BMDDeckLinkAPIInformationID, double*) override { return E_NOTIMPL; }
    HRESULT GetString(BMDDeckLinkAPIInformationID, const char**) override { return E_NOTIMPL; }
};

}  // namespace

FAKE_EXPORT IDeckLinkIterator* CreateDeckLinkIteratorInstance_0004() { return new Iterator(); }
FAKE_EXPORT IDeckLinkAPIInformation* CreateDeckLinkAPIInformationInstance_0001() { return new ApiInfo(); }
FAKE_EXPORT void* CreateVideoConversionInstance_0003() { return nullptr; }
FAKE_EXPORT void* CreateDeckLinkDiscoveryInstance_0003() { return nullptr; }
FAKE_EXPORT void* CreateVideoFrameAncillaryPacketsInstance_0002() { return nullptr; }

// ── test inspection ──
// Pixel (x, y) of the last frame on a channel (0 = A/fill, 1 = B/key) as 0xAARRGGBB, or 0 if none.
FAKE_EXPORT uint32_t fake_pixel(int dev, int channel, int x, int y) {
    Recorder& r = gRec[dev];
    std::lock_guard<std::mutex> l(r.m);
    const std::vector<uint8_t>& img = channel ? r.keyB : r.fillA;
    if (img.empty() || x < 0 || y < 0 || x >= r.w || y >= r.h) return 0;
    const uint8_t* p = img.data() + (size_t)y * r.rb + (size_t)x * 4;
    return (uint32_t)p[3] << 24 | (uint32_t)p[2] << 16 | (uint32_t)p[1] << 8 | p[0];
}
FAKE_EXPORT uint64_t fake_displayed(int dev) { std::lock_guard<std::mutex> l(gRec[dev].m); return gRec[dev].displayed; }
FAKE_EXPORT int fake_keyer(int dev) { std::lock_guard<std::mutex> l(gRec[dev].m); return gRec[dev].keyer; }
FAKE_EXPORT int fake_level(int dev) { std::lock_guard<std::mutex> l(gRec[dev].m); return gRec[dev].level; }
FAKE_EXPORT uint32_t fake_enabled_mode(int dev) { std::lock_guard<std::mutex> l(gRec[dev].m); return gRec[dev].enabledMode; }
FAKE_EXPORT int fake_playing(int dev) { std::lock_guard<std::mutex> l(gRec[dev].m); return gRec[dev].playing; }
