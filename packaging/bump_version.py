"""Bump the Confidence / dsan version.

    python packaging/bump_version.py patch      # 0.3.0 → 0.3.1  (fixes)
    python packaging/bump_version.py minor      # 0.3.0 → 0.4.0  (new features)
    python packaging/bump_version.py major      # 0.3.0 → 1.0.0
    python packaging/bump_version.py 1.2.3      # set exactly

Updates src/dsan/__init__.py and turns CHANGELOG.md's "Unreleased" section into
the new version's section (dated today), leaving a fresh empty "Unreleased".
"""
import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "src" / "dsan" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"


def current() -> str:
    m = re.search(r'^__version__ = "(\d+)\.(\d+)\.(\d+)"', INIT.read_text(), re.M)
    if not m:
        raise SystemExit("couldn't find __version__ in src/dsan/__init__.py")
    return ".".join(m.groups())


def bumped(version: str, part: str) -> str:
    if re.fullmatch(r"\d+\.\d+\.\d+", part):
        return part
    major, minor, patch = map(int, version.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise SystemExit(__doc__)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    old = current()
    new = bumped(old, sys.argv[1])
    INIT.write_text(re.sub(r'^__version__ = ".*"', f'__version__ = "{new}"', INIT.read_text(), count=1, flags=re.M))
    if CHANGELOG.exists():
        text = CHANGELOG.read_text()
        today = dt.date.today().isoformat()
        if "## [Unreleased]" in text:
            text = text.replace("## [Unreleased]", f"## [Unreleased]\n\n## [{new}] — {today}", 1)
        else:
            text = text.replace("\n## ", f"\n## [{new}] — {today}\n\n## ", 1)
        CHANGELOG.write_text(text)
    print(f"{old} → {new}")
    print("Next: describe the changes in CHANGELOG.md under the new version, then build.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
