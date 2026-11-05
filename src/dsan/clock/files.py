"""File dialogs that always land somewhere writable.

An app opened from Finder starts with "/" as its working directory, so a bare
file name ("Rick Look.json") would be written to the read-only root of the
disk. Every dialog here starts in a real folder — the last one used for that
kind of file, else ~/Documents/Confidence — and any relative path that comes
back is made absolute against that folder.
"""
from __future__ import annotations

import os
from typing import Optional

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QFileDialog, QWidget

_KEY = "files/last_dir/"


def default_dir() -> str:
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    base = docs if os.path.isdir(docs) else os.path.expanduser("~")
    path = os.path.join(base, "Confidence")
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        return base


def _settings() -> QSettings:
    return QSettings("TRUCUE", "Confidence")


def last_dir(kind: str) -> str:
    d = _settings().value(_KEY + kind, "", type=str)
    return d if d and os.path.isdir(d) and os.access(d, os.W_OK) else default_dir()


def _remember(kind: str, path: str) -> None:
    _settings().setValue(_KEY + kind, os.path.dirname(path))


def absolute(path: str, kind: str) -> str:
    path = os.path.expanduser(path)
    return path if os.path.isabs(path) else os.path.join(last_dir(kind), path)


def save_path(parent: Optional[QWidget], title: str, name: str, filt: str, kind: str,
              suffix: str = "") -> str:
    start = os.path.join(last_dir(kind), os.path.basename(name))
    path, _ = QFileDialog.getSaveFileName(parent, title, start, filt)
    if not path:
        return ""
    path = absolute(path, kind)
    if suffix and not path.lower().endswith(suffix):
        path += suffix
    _remember(kind, path)
    return path


def open_path(parent: Optional[QWidget], title: str, filt: str, kind: str) -> str:
    path, _ = QFileDialog.getOpenFileName(parent, title, last_dir(kind), filt)
    if not path:
        return ""
    path = absolute(path, kind)
    _remember(kind, path)
    return path
