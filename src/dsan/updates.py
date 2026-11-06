"""Check GitHub Releases for a newer Confidence. Standard library only.

    >>> info = check()                      # UpdateInfo, or raises UpdateError
    >>> info.available, info.latest, info.download_url

Releases are made by the GitHub workflow when a version tag (v0.5.0…) is pushed.
Their assets are named like the build scripts name them:

    Confidence-<version>-macOS-arm64.dmg / -macOS-x86_64.dmg
    Confidence-<version>-Windows.zip
    Confidence-<version>-Linux-x86_64.tar.gz / -Linux-aarch64.tar.gz

Nothing is downloaded or installed here: the app shows what's new and opens the
download for this computer in the browser.
"""
from __future__ import annotations

import json
import platform
import re
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import __version__

REPO = "rickmcgill929/Confidence"
API = "https://api.github.com/repos/{repo}/releases/latest"
TIMEOUT = 8.0


class UpdateError(Exception):
    """The check couldn't be completed (offline, no releases yet, GitHub error…)."""


def parse_version(text: str) -> Tuple[int, ...]:
    """'v0.5.0' → (0, 5, 0). Pre-release suffixes ('0.5.0-beta') sort before the release."""
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)(.*)$", text or "")
    if not m:
        raise ValueError(f"not a version: {text!r}")
    nums = tuple(int(x) for x in m.group(1).split("."))
    nums = nums + (0,) * (3 - len(nums)) if len(nums) < 3 else nums
    return nums + ((0,) if m.group(2).strip(" .-+") else (1,))   # final release > pre-release


def is_newer(latest: str, current: str = __version__) -> bool:
    return parse_version(latest) > parse_version(current)


def platform_key(system: Optional[str] = None, machine: Optional[str] = None) -> str:
    """The asset name fragment for this computer: macOS-arm64, macOS-x86_64, Windows, Linux-x86_64…"""
    system = system or sys.platform
    machine = (machine or platform.machine()).lower()
    if system == "darwin":
        return "macOS-arm64" if machine in ("arm64", "aarch64") else "macOS-x86_64"
    if system.startswith("win"):
        return "Windows"
    arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    return f"Linux-{arch}"


@dataclass
class Asset:
    name: str
    url: str
    size: int = 0


@dataclass
class UpdateInfo:
    current: str
    latest: str
    available: bool
    page_url: str                       # the release page
    notes: str                          # release notes (Markdown)
    published: str = ""
    assets: List[Asset] = field(default_factory=list)
    asset: Optional[Asset] = None       # the one for this computer, if there is one

    @property
    def download_url(self) -> str:
        return self.asset.url if self.asset else self.page_url


def from_release(data: dict, current: str = __version__, key: Optional[str] = None) -> UpdateInfo:
    """Build UpdateInfo from a GitHub 'latest release' API response."""
    tag = str(data.get("tag_name") or data.get("name") or "")
    try:
        latest = ".".join(str(n) for n in parse_version(tag)[:3])
    except ValueError:
        raise UpdateError(f"the latest release has no version number ({tag!r})") from None
    assets = [Asset(a.get("name", ""), a.get("browser_download_url", ""), int(a.get("size") or 0))
              for a in data.get("assets") or []]
    key = key or platform_key()
    mine = next((a for a in assets if key in a.name), None)
    return UpdateInfo(current=current, latest=latest, available=is_newer(tag, current),
                      page_url=str(data.get("html_url") or f"https://github.com/{REPO}/releases/latest"),
                      notes=str(data.get("body") or "").strip(), published=str(data.get("published_at") or "")[:10],
                      assets=assets, asset=mine)


def ssl_context() -> ssl.SSLContext:
    """Verify GitHub with certifi's certificate list when it's installed.

    python.org's Python on macOS, and apps packaged from it, don't use the Mac's
    trusted certificates, so the default context fails with CERTIFICATE_VERIFY_FAILED.
    certifi ships Mozilla's list with the app; without it, use the system default.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except (ImportError, OSError):
        return ssl.create_default_context()


def fetch_latest(repo: str = REPO, timeout: float = TIMEOUT) -> dict:
    req = urllib.request.Request(API.format(repo=repo), headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"Confidence/{__version__}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("No releases have been published yet.") from None
        if e.code in (403, 429):
            raise UpdateError("GitHub is limiting update checks right now — try again later.") from None
        raise UpdateError(f"GitHub answered {e.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, ssl.SSLError) or isinstance(e, ssl.SSLError):
            raise UpdateError("Couldn't make a secure connection to GitHub (the certificate couldn't be "
                              "verified). A network filter or proxy may be in the way.") from None
        raise UpdateError(f"Couldn't reach GitHub ({reason}). Are you online?") from None
    except ValueError:
        raise UpdateError("GitHub sent something unexpected.") from None


def check(repo: str = REPO, current: str = __version__, timeout: float = TIMEOUT) -> UpdateInfo:
    return from_release(fetch_latest(repo, timeout), current)


__all__ = ["REPO", "Asset", "UpdateError", "UpdateInfo", "check", "fetch_latest", "from_release",
           "is_newer", "parse_version", "platform_key"]
