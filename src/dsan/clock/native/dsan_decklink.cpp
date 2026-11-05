// dsan_decklink — a small C interface over the Blackmagic DeckLink SDK for Confidence.
//
// Python (dsan.clock.decklink) renders frames and calls dsan_dl_push() with the
// latest BGRA image whenever it changes. Each output runs DeckLink *scheduled
// playback*: on every completed frame the driver calls back, we copy the latest
// image into that frame and schedule it again. Output cadence therefore comes
// from the card's clock (and genlock, if reference is connected), independent
// of how often Python renders.
//
// Cut/fill:
//   keying 0  fill only (alpha dropped by the card)
//   keying 1  hardware external keyer: fill on channel A, key on channel B
//             (IDeckLinkKeyer::Enable(true) — DeckLink Duo 2, Quad 2, 4K Extreme…)
//   keying 2  separate key output: a second DeckLink output carries the key
//             (alpha as greyscale) — any two outputs; lock both to reference
//             for frame-accurate fill/key alignment.
//
// Build: packaging/build_decklink.sh (needs the DeckLink SDK's include folder).
// macOS and Linux. Written against DeckLink SDK 16.0.

#include "DeckLinkAPI.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <vector>

#if defined(__APPLE__)
#include <CoreFoundation/CoreFoundation.h>
// Apple's CFPlugInCOM.h provides IUnknownUUID but not IID_IUnknown (the SDK
// samples define it the same way in com_utils.h).
#ifndef IID_IUnknown
#define IID_IUnknown CFUUIDGetUUIDBytes(IUnknownUUID)
#endif
using DLString = CFStringRef;
static std::string toStd(DLString s) {
    if (!s) return {};
    char buf[512] = {0};
    CFStringGetCString(s, buf, sizeof buf, kCFStringEncodingUTF8);
    CFRelease(s);
    return buf;
}
#elif defined(_WIN32)
#error "dsan_decklink: Windows isn't supported yet"
#else
using DLString = const char*;
static std::string toStd(DLString s) {
    if (!s) return {};
    std::string r(s);
    free((void*)s);
    return r;
}
#endif

#define DSAN_EXPORT extern "C" __attribute__((visibility("default")))

namespace {

constexpr int kAbiVersion = 1;
constexpr uint32_t kPrerollFrames = 3;     // ~50 ms at 59.94: low latency for a clock
constexpr uint32_t kPoolFrames = kPrerollFrames + 2;

void setErr(char* err, int len, const std::string& msg) {
    if (err && len > 0) {
        std::strncpy(err, msg.c_str(), (size_t)len - 1);
        err[len - 1] = 0;
    }
}

std::string hrText(HRESULT hr) {
    char buf[32];
    std::snprintf(buf, sizeof buf, "0x%08X", (unsigned)hr);
    return buf;
}

template <class T>
struct Ref {
    T* p = nullptr;
    Ref() = default;
    explicit Ref(T* raw) : p(raw) {}
    Ref(const Ref&) = delete;
    Ref& operator=(const Ref&) = delete;
    Ref(Ref&& o) noexcept : p(o.p) { o.p = nullptr; }
    Ref& operator=(Ref&& o) noexcept { reset(); p = o.p; o.p = nullptr; return *this; }
    ~Ref() { reset(); }
    void reset() { if (p) { p->Release(); p = nullptr; } }
    T** out() { reset(); return &p; }
    T* operator->() const { return p; }
    explicit operator bool() const { return p != nullptr; }
};

template <class T>
Ref<T> query(IUnknown* obj, REFIID iid) {
    Ref<T> r;
    if (obj) obj->QueryInterface(iid, (void**)r.out());
    return r;
}

bool sameIID(REFIID a, REFIID b) { return std::memcmp(&a, &b, sizeof(REFIID)) == 0; }

// ── device list ──────────────────────────────────────────────────────────────

enum : int { CAP_OUTPUT = 1, CAP_INTERNAL_KEY = 2, CAP_EXTERNAL_KEY = 4, CAP_INACTIVE = 8 };
enum : int { MODE_BGRA = 1, MODE_KEYING = 2, MODE_INTERLACED = 4 };

struct ModeInfo {
    uint32_t id;
    std::string name;
    int width, height;
    int64_t duration, scale;
    int flags;
};

struct Device {
    Ref<IDeckLink> deckLink;
    std::string name;
    int caps = 0;
    std::vector<ModeInfo> modes;
};

std::mutex gDevicesMutex;
std::vector<Device> gDevices;

void scanModes(Device& d) {
    d.modes.clear();
    auto out = query<IDeckLinkOutput>(d.deckLink.p, IID_IDeckLinkOutput);
    if (!out) return;
    Ref<IDeckLinkDisplayModeIterator> it;
    if (out->GetDisplayModeIterator(it.out()) != S_OK) return;
    Ref<IDeckLinkDisplayMode> mode;
    while (it->Next(mode.out()) == S_OK) {
        ModeInfo m{};
        m.id = (uint32_t)mode->GetDisplayMode();
        DLString n = nullptr;
        if (mode->GetName(&n) == S_OK) m.name = toStd(n);
        m.width = (int)mode->GetWidth();
        m.height = (int)mode->GetHeight();
        BMDTimeValue dur = 0;
        BMDTimeScale scale = 0;
        mode->GetFrameRate(&dur, &scale);
        m.duration = dur;
        m.scale = scale;
        BMDFieldDominance fd = mode->GetFieldDominance();
        if (fd != bmdProgressiveFrame && fd != bmdProgressiveSegmentedFrame) m.flags |= MODE_INTERLACED;
        bool ok = false;
        if (out->DoesSupportVideoMode(bmdVideoConnectionUnspecified, mode->GetDisplayMode(), bmdFormat8BitBGRA,
                                      bmdNoVideoOutputConversion, bmdSupportedVideoModeDefault, nullptr, &ok) == S_OK && ok)
            m.flags |= MODE_BGRA;
        ok = false;
        if (out->DoesSupportVideoMode(bmdVideoConnectionUnspecified, mode->GetDisplayMode(), bmdFormat8BitBGRA,
                                      bmdNoVideoOutputConversion, bmdSupportedVideoModeKeying, nullptr, &ok) == S_OK && ok)
            m.flags |= MODE_KEYING;
        if (m.flags & MODE_BGRA) d.modes.push_back(m);
    }
}

int scanDevices() {
    std::lock_guard<std::mutex> lock(gDevicesMutex);
    gDevices.clear();
    Ref<IDeckLinkIterator> it(CreateDeckLinkIteratorInstance());
    if (!it) return -1;
    Ref<IDeckLink> dl;
    while (it->Next(dl.out()) == S_OK) {
        Device d;
        d.deckLink = std::move(dl);
        DLString n = nullptr;
        d.name = d.deckLink->GetDisplayName(&n) == S_OK ? toStd(n) : "DeckLink";
        if (query<IDeckLinkOutput>(d.deckLink.p, IID_IDeckLinkOutput)) d.caps |= CAP_OUTPUT;
        if (auto attr = query<IDeckLinkProfileAttributes>(d.deckLink.p, IID_IDeckLinkProfileAttributes)) {
            bool flag = false;
            if (attr->GetFlag(BMDDeckLinkSupportsInternalKeying, &flag) == S_OK && flag) d.caps |= CAP_INTERNAL_KEY;
            flag = false;
            if (attr->GetFlag(BMDDeckLinkSupportsExternalKeying, &flag) == S_OK && flag) d.caps |= CAP_EXTERNAL_KEY;
            int64_t duplex = 0;
            if (attr->GetInt(BMDDeckLinkDuplex, &duplex) == S_OK && duplex == bmdDuplexInactive) d.caps |= CAP_INACTIVE;
        }
        if (d.caps & CAP_OUTPUT) scanModes(d);
        gDevices.push_back(std::move(d));
    }
    return (int)gDevices.size();
}

// ── output session ───────────────────────────────────────────────────────────

struct Session;

class Channel final : public IDeckLinkVideoOutputCallback {
public:
    Channel(Session* s, IDeckLinkOutput* out, bool isKey) : session(s), output(out), key(isKey) { output->AddRef(); }

    HRESULT QueryInterface(REFIID iid, LPVOID* ppv) override {
        if (!ppv) return E_INVALIDARG;
        if (sameIID(iid, IID_IUnknown) || sameIID(iid, IID_IDeckLinkVideoOutputCallback)) {
            *ppv = static_cast<IDeckLinkVideoOutputCallback*>(this);
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    ULONG AddRef() override { return ++refs; }
    ULONG Release() override {
        ULONG n = --refs;
        if (n == 0) delete this;
        return n;
    }

    HRESULT ScheduledFrameCompleted(IDeckLinkVideoFrame* frame, BMDOutputFrameCompletionResult result) override;
    HRESULT ScheduledPlaybackHasStopped() override {
        std::lock_guard<std::mutex> lock(stopMutex);
        stopped = true;
        stopCv.notify_all();
        return S_OK;
    }

    bool start(std::string& err);
    void stop();
    void scheduleFrame(IDeckLinkVideoFrame* frame);

    Session* session;
    IDeckLinkOutput* output;
    bool key;
    Ref<IDeckLinkKeyer> keyer;
    std::vector<IDeckLinkMutableVideoFrame*> pool;
    uint64_t nextIndex = 0;
    bool started = false;

private:
    ~Channel() {
        for (auto* f : pool) f->Release();
        output->Release();
    }
    std::atomic<ULONG> refs{1};
    std::mutex stopMutex;
    std::condition_variable stopCv;
    bool stopped = false;
};

struct Session {
    int width = 0, height = 0, rowBytes = 0;
    int64_t duration = 0, scale = 0;
    uint32_t mode = 0;
    int keying = 0;
    std::atomic<bool> stopping{false};

    std::mutex frameMutex;
    std::vector<uint8_t> latest;         // straight-alpha BGRA, rowBytes stride
    bool haveFrame = false;

    std::atomic<uint64_t> frames{0}, late{0}, dropped{0};
    Channel* fill = nullptr;
    Channel* key = nullptr;
    Ref<IDeckLink> fillDevice, keyDevice;
};

void copyInto(Session* s, uint8_t* dst, int dstRowBytes, bool asKey) {
    std::lock_guard<std::mutex> lock(s->frameMutex);
    for (int y = 0; y < s->height; ++y) {
        uint8_t* d = dst + (size_t)y * dstRowBytes;
        if (!s->haveFrame) {
            // nothing rendered yet: transparent black fill / black key
            std::memset(d, 0, (size_t)s->width * 4);
            if (asKey) for (int x = 0; x < s->width; ++x) d[x * 4 + 3] = 255;
            continue;
        }
        const uint8_t* src = s->latest.data() + (size_t)y * s->rowBytes;
        if (!asKey) {
            std::memcpy(d, src, (size_t)s->width * 4);
        } else {
            for (int x = 0; x < s->width; ++x) {
                uint8_t a = src[x * 4 + 3];
                d[x * 4 + 0] = a;
                d[x * 4 + 1] = a;
                d[x * 4 + 2] = a;
                d[x * 4 + 3] = 255;
            }
        }
    }
}

void Channel::scheduleFrame(IDeckLinkVideoFrame* frame) {
    if (auto buf = query<IDeckLinkVideoBuffer>(frame, IID_IDeckLinkVideoBuffer)) {
        if (buf->StartAccess(bmdBufferAccessWrite) == S_OK) {
            void* bytes = nullptr;
            if (buf->GetBytes(&bytes) == S_OK && bytes)
                copyInto(session, (uint8_t*)bytes, (int)frame->GetRowBytes(), key);
            buf->EndAccess(bmdBufferAccessWrite);
        }
    }
    output->ScheduleVideoFrame(frame, (BMDTimeValue)(nextIndex * (uint64_t)session->duration),
                               session->duration, session->scale);
    ++nextIndex;
    if (!key) ++session->frames;
}

HRESULT Channel::ScheduledFrameCompleted(IDeckLinkVideoFrame* frame, BMDOutputFrameCompletionResult result) {
    if (result == bmdOutputFrameFlushed || session->stopping) return S_OK;
    if (result == bmdOutputFrameDisplayedLate || result == bmdOutputFrameDropped) {
        if (!key) (result == bmdOutputFrameDropped ? session->dropped : session->late)++;
        // We fell behind the card's clock: jump the schedule ahead of "now".
        BMDTimeValue now = 0;
        double speed = 0;
        if (output->GetScheduledStreamTime(session->scale, &now, &speed) == S_OK && session->duration > 0) {
            uint64_t nowIndex = (uint64_t)(now / session->duration);
            if (nextIndex <= nowIndex + 1) nextIndex = nowIndex + 2;
        }
    }
    scheduleFrame(frame);
    return S_OK;
}

bool Channel::start(std::string& err) {
    HRESULT hr = output->EnableVideoOutput((BMDDisplayMode)session->mode, bmdVideoOutputFlagDefault);
    if (hr != S_OK) { err = "EnableVideoOutput failed (" + hrText(hr) + ") — is another app using this output?"; return false; }
    for (uint32_t i = 0; i < kPoolFrames; ++i) {
        IDeckLinkMutableVideoFrame* f = nullptr;
        hr = output->CreateVideoFrame(session->width, session->height, session->rowBytes, bmdFormat8BitBGRA,
                                      bmdFrameFlagDefault, &f);
        if (hr != S_OK || !f) { err = "CreateVideoFrame failed (" + hrText(hr) + ")"; return false; }
        pool.push_back(f);
    }
    hr = output->SetScheduledFrameCompletionCallback(this);
    if (hr != S_OK) { err = "SetScheduledFrameCompletionCallback failed (" + hrText(hr) + ")"; return false; }
    for (uint32_t i = 0; i < kPrerollFrames; ++i) scheduleFrame(pool[i]);
    started = true;
    return true;
}

void Channel::stop() {
    if (started) {
        bool running = false;
        if (output->IsScheduledPlaybackRunning(&running) == S_OK && running) {
            output->StopScheduledPlayback(0, nullptr, 0);
            std::unique_lock<std::mutex> lock(stopMutex);
            stopCv.wait_for(lock, std::chrono::seconds(2), [this] { return stopped; });
        }
    }
    output->SetScheduledFrameCompletionCallback(nullptr);
    if (keyer) keyer->Disable();
    output->DisableVideoOutput();
    started = false;
}

void closeSession(Session* s) {
    if (!s) return;
    s->stopping = true;
    for (Channel* c : {s->fill, s->key}) {
        if (!c) continue;
        c->stop();
        c->Release();
    }
    delete s;
}

}  // namespace

// ── C API ────────────────────────────────────────────────────────────────────

DSAN_EXPORT int dsan_dl_abi_version() { return kAbiVersion; }

// DeckLink driver API version as 0xMMmmpp00, or -1 if Desktop Video isn't installed.
DSAN_EXPORT int64_t dsan_dl_api_version() {
    Ref<IDeckLinkAPIInformation> info(CreateDeckLinkAPIInformationInstance());
    if (!info) return -1;
    int64_t v = 0;
    if (info->GetInt(BMDDeckLinkAPIVersion, &v) != S_OK) return 0;
    return v;
}

// Re-scan devices. Returns the count, or -1 if the DeckLink driver couldn't be loaded.
DSAN_EXPORT int dsan_dl_scan() { return scanDevices(); }

DSAN_EXPORT int dsan_dl_device(int index, char* name, int nameLen, int* caps) {
    std::lock_guard<std::mutex> lock(gDevicesMutex);
    if (index < 0 || index >= (int)gDevices.size()) return -1;
    setErr(name, nameLen, gDevices[index].name);
    if (caps) *caps = gDevices[index].caps;
    return 0;
}

DSAN_EXPORT int dsan_dl_mode_count(int index) {
    std::lock_guard<std::mutex> lock(gDevicesMutex);
    if (index < 0 || index >= (int)gDevices.size()) return -1;
    return (int)gDevices[index].modes.size();
}

DSAN_EXPORT int dsan_dl_mode(int index, int m, uint32_t* id, char* name, int nameLen, int* width, int* height,
                             int64_t* duration, int64_t* scale, int* flags) {
    std::lock_guard<std::mutex> lock(gDevicesMutex);
    if (index < 0 || index >= (int)gDevices.size()) return -1;
    auto& modes = gDevices[index].modes;
    if (m < 0 || m >= (int)modes.size()) return -1;
    const ModeInfo& mi = modes[m];
    if (id) *id = mi.id;
    setErr(name, nameLen, mi.name);
    if (width) *width = mi.width;
    if (height) *height = mi.height;
    if (duration) *duration = mi.duration;
    if (scale) *scale = mi.scale;
    if (flags) *flags = mi.flags;
    return 0;
}

DSAN_EXPORT void* dsan_dl_open(int fillIndex, int keyIndex, uint32_t mode, int keying, char* err, int errLen) {
    Ref<IDeckLink> fillDl, keyDl;
    {
        std::lock_guard<std::mutex> lock(gDevicesMutex);
        if (fillIndex < 0 || fillIndex >= (int)gDevices.size()) { setErr(err, errLen, "No such DeckLink device"); return nullptr; }
        fillDl.p = gDevices[fillIndex].deckLink.p;
        fillDl.p->AddRef();
        if (keying == 2) {
            if (keyIndex < 0 || keyIndex >= (int)gDevices.size() || keyIndex == fillIndex) {
                setErr(err, errLen, "Pick a different DeckLink output for the key"); return nullptr;
            }
            keyDl.p = gDevices[keyIndex].deckLink.p;
            keyDl.p->AddRef();
        }
    }
    auto fillOut = query<IDeckLinkOutput>(fillDl.p, IID_IDeckLinkOutput);
    if (!fillOut) { setErr(err, errLen, "That DeckLink device has no output"); return nullptr; }

    Ref<IDeckLinkDisplayMode> dm;
    if (fillOut->GetDisplayMode((BMDDisplayMode)mode, dm.out()) != S_OK || !dm) {
        setErr(err, errLen, "That video mode isn't available on this device"); return nullptr;
    }
    bool ok = false;
    BMDSupportedVideoModeFlags need = keying == 1 ? bmdSupportedVideoModeKeying : bmdSupportedVideoModeDefault;
    if (fillOut->DoesSupportVideoMode(bmdVideoConnectionUnspecified, (BMDDisplayMode)mode, bmdFormat8BitBGRA,
                                      bmdNoVideoOutputConversion, need, nullptr, &ok) != S_OK || !ok) {
        setErr(err, errLen, keying == 1 ? "This device can't key in that video mode" : "This device can't output that video mode");
        return nullptr;
    }

    auto* s = new Session();
    s->mode = mode;
    s->keying = keying;
    s->width = (int)dm->GetWidth();
    s->height = (int)dm->GetHeight();
    BMDTimeValue dur = 0;
    BMDTimeScale sc = 0;
    dm->GetFrameRate(&dur, &sc);
    s->duration = dur;
    s->scale = sc;
    int32_t rb = 0;
    fillOut->RowBytesForPixelFormat(bmdFormat8BitBGRA, s->width, &rb);
    s->rowBytes = rb > 0 ? rb : s->width * 4;
    s->latest.assign((size_t)s->rowBytes * s->height, 0);
    s->fillDevice = std::move(fillDl);

    std::string e;
    s->fill = new Channel(s, fillOut.p, false);
    if (keying == 1) {
        s->fill->keyer = query<IDeckLinkKeyer>(s->fillDevice.p, IID_IDeckLinkKeyer);
        if (!s->fill->keyer) { setErr(err, errLen, "This device has no keyer"); closeSession(s); return nullptr; }
    }
    if (!s->fill->start(e)) { setErr(err, errLen, "Fill: " + e); closeSession(s); return nullptr; }
    if (keying == 1) {
        // External keying: fill on channel A, key on channel B, at full level.
        HRESULT hr = s->fill->keyer->Enable(true);
        if (hr != S_OK) { setErr(err, errLen, "Keyer Enable(external) failed (" + hrText(hr) + ")"); closeSession(s); return nullptr; }
        s->fill->keyer->SetLevel(255);
    }

    if (keying == 2) {
        auto keyOut = query<IDeckLinkOutput>(keyDl.p, IID_IDeckLinkOutput);
        ok = false;
        if (!keyOut || keyOut->DoesSupportVideoMode(bmdVideoConnectionUnspecified, (BMDDisplayMode)mode, bmdFormat8BitBGRA,
                                                    bmdNoVideoOutputConversion, bmdSupportedVideoModeDefault, nullptr, &ok) != S_OK || !ok) {
            setErr(err, errLen, "The key output can't do that video mode"); closeSession(s); return nullptr;
        }
        s->keyDevice = std::move(keyDl);
        s->key = new Channel(s, keyOut.p, true);
        if (!s->key->start(e)) { setErr(err, errLen, "Key: " + e); closeSession(s); return nullptr; }
    }

    // Start both outputs together so fill and key share a timeline.
    for (Channel* c : {s->fill, s->key}) {
        if (!c) continue;
        HRESULT hr = c->output->StartScheduledPlayback(0, s->scale, 1.0);
        if (hr != S_OK) {
            setErr(err, errLen, std::string(c->key ? "Key" : "Fill") + ": StartScheduledPlayback failed (" + hrText(hr) + ")");
            closeSession(s);
            return nullptr;
        }
    }
    return s;
}

// Hand over the latest rendered image: straight-alpha BGRA (Qt Format_ARGB32 on
// little-endian), exactly the session's width × height.
DSAN_EXPORT int dsan_dl_push(void* handle, const void* bgra, int rowBytes, int width, int height) {
    auto* s = static_cast<Session*>(handle);
    if (!s || !bgra) return -1;
    if (width != s->width || height != s->height) return -2;
    std::lock_guard<std::mutex> lock(s->frameMutex);
    const uint8_t* src = static_cast<const uint8_t*>(bgra);
    for (int y = 0; y < s->height; ++y)
        std::memcpy(s->latest.data() + (size_t)y * s->rowBytes, src + (size_t)y * rowBytes, (size_t)s->width * 4);
    s->haveFrame = true;
    return 0;
}

// Stats. ref: bit0 = reference not supported, bit1 = locked (BMDReferenceStatus).
DSAN_EXPORT int dsan_dl_status(void* handle, uint64_t* frames, uint64_t* late, uint64_t* dropped, int* ref,
                               int* width, int* height) {
    auto* s = static_cast<Session*>(handle);
    if (!s) return -1;
    if (frames) *frames = s->frames;
    if (late) *late = s->late;
    if (dropped) *dropped = s->dropped;
    if (width) *width = s->width;
    if (height) *height = s->height;
    if (ref) {
        BMDReferenceStatus st = 0;
        *ref = s->fill && s->fill->output->GetReferenceStatus(&st) == S_OK ? (int)st : -1;
    }
    return 0;
}

DSAN_EXPORT void dsan_dl_close(void* handle) { closeSession(static_cast<Session*>(handle)); }
