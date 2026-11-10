"""Edit and import layouts from the browser remote.

Every change goes through the window, exactly like its own editor: the element list
and properties panel follow, the layout autosaves, and Undo (in the app or on the
web page) steps back over web edits too.

``LayoutEditor.state()`` is the layout plus a revision number (a hash), so the page
can tell when someone changed it in the app. ``LayoutEditor.op(data)`` applies one
edit — see :data:`OPS`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional

from . import files
from .layout import (COMMON_FIELDS, DEFAULT_COLORS, ELEMENT_TYPES, NO_SIGNAL_MODES, TYPE_FIELDS, Element, Layout,
                     default_element, parse_color)

OPS = ("set", "set_layout", "add", "duplicate", "delete", "move", "undo", "redo", "save", "save_as", "new")
MAX_ELEMENTS = 200


class LayoutError(ValueError):
    pass


def _choices():
    from .app import _CHOICES
    return {k: [{"value": v, "label": l} for v, l in items] for k, items in _CHOICES.items()}


def schema(families: List[str]) -> Dict[str, Any]:
    """Everything the web editor needs to build its forms (same labels as the app)."""
    from .app import _BOOLS, _COLOR_FIELDS, _FIELD_LABELS, _GEOM
    from .app import LayoutEditor as LayoutProps
    choices = _choices()
    fields: Dict[str, Dict[str, Any]] = {}
    for f in set(COMMON_FIELDS) | {f for fs in TYPE_FIELDS.values() for f in fs}:
        d: Dict[str, Any] = {"label": _FIELD_LABELS.get(f, f)}
        if f in _BOOLS:
            d["kind"] = "bool"
        elif f in _GEOM:
            d["kind"] = "pct"
        elif f in _COLOR_FIELDS:
            d["kind"] = "color"
            d["alpha"] = True
        elif f in choices:
            d["kind"] = "choice"
            d["choices"] = choices[f]
        elif f == "font":
            d["kind"] = "font"
        elif f == "frame_px":
            d.update(kind="int", min=1, max=400, step=1, suffix="px")
        elif f == "radius":
            d.update(kind="float", min=0, max=0.5, step=0.01)
        elif f == "hold":
            d.update(kind="float", min=0.2, max=10, step=0.1, suffix="s")
        else:
            d["kind"] = "text"
        fields[f] = d
    return {
        "types": [{"value": k, "label": v} for k, v in ELEMENT_TYPES.items()],
        "common": COMMON_FIELDS, "type_fields": TYPE_FIELDS, "fields": fields,
        "fonts": families,
        "layout_colors": [{"key": k, "label": l} for k, l in LayoutProps._COLOR_KEYS],
        "no_signal": [{"value": "dim", "label": "Dim digits + NO SIGNAL"}, {"value": "text", "label": "Show NO SIGNAL"},
                      {"value": "hide", "label": "Hide timers"}, {"value": "ignore", "label": "Keep showing last time"}],
        "label_tokens": "Limitimer: {program} {n} {mode} · Millumin: {clip} {layer} {column} {trt} {remaining} "
                        "{elapsed} {state} {playmode} · TruCue: {tc_clip} {tc_index} {tc_next} {tc_prev} "
                        "{tc_remaining} {tc_elapsed} {tc_trt} {tc_state} {tc_playmode} {tc_bookmark} "
                        "{tc_bookmark_remaining} {tc_volume}",
    }


def clean_value(e: Element, f: str, v: Any):
    """Validate one field value for an element; returns the value to store."""
    from .app import _BOOLS, _COLOR_FIELDS, _GEOM
    allowed = set(COMMON_FIELDS) | set(TYPE_FIELDS[e.type])
    if f not in allowed:
        raise LayoutError(f"{ELEMENT_TYPES[e.type]} has no {f!r}")
    if f in _BOOLS:
        return bool(v)
    if f in _GEOM:
        try:
            x = float(v)
        except (TypeError, ValueError):
            raise LayoutError(f"{f} must be a number") from None
        lo = 0.0 if f in ("x", "y") else 0.005
        return round(max(lo, min(1.0, x)), 4)
    if f in _COLOR_FIELDS:
        try:
            parse_color(str(v))
        except ValueError:
            raise LayoutError(f"not a color: {v!r} (use #RRGGBB or #RRGGBBAA)") from None
        s = str(v).strip().upper()
        return s if s.startswith("#") else "#" + s
    choices = _choices()
    if f in choices:
        if v not in {c["value"] for c in choices[f]}:
            raise LayoutError(f"{f} can't be {v!r}")
        return v
    if f == "frame_px":
        try:
            return max(1, min(400, int(v)))
        except (TypeError, ValueError):
            raise LayoutError("frame width must be a whole number") from None
    if f in ("radius", "hold"):
        try:
            x = float(v)
        except (TypeError, ValueError):
            raise LayoutError(f"{f} must be a number") from None
        return round(max(0.0, min(0.5, x)), 3) if f == "radius" else round(max(0.2, min(10.0, x)), 2)
    if f in ("name", "text", "font"):
        return str(v)[:500]
    raise LayoutError(f"can't set {f!r}")


def safe_filename(name: str) -> str:
    base = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "-", (name or "").strip()).strip(" .-")[:120] or "layout"
    return base if base.lower().endswith(".json") else base + ".json"


class LayoutEditor:
    def __init__(self, window):
        self.w = window

    # -- reading -----------------------------------------------------------------------

    def revision(self) -> str:
        return hashlib.sha1(self.w.layout_obj.to_json().encode("utf-8")).hexdigest()[:12]

    def state(self) -> Dict[str, Any]:
        w = self.w
        px = w._output_pixels()
        return {
            "rev": self.revision(),
            "layout": w.layout_obj.to_dict(),
            "file": os.path.basename(w.layout_path) if w.layout_path else None,
            "in_layouts_folder": bool(w.layout_path) and os.path.dirname(os.path.abspath(w.layout_path))
            == os.path.abspath(files.last_dir("layouts")),
            "pixels": list(px), "aspect": round(px[0] / max(1, px[1]), 4),
            "can_undo": bool(w.history.can_undo or w._pending_change()), "can_redo": bool(w.history.can_redo),
        }

    def json_text(self) -> str:
        return self.w.layout_obj.to_json()

    # -- editing -------------------------------------------------------------------------

    def _find(self, eid) -> Element:
        e = self.w.layout_obj.find(str(eid))
        if e is None:
            raise LayoutError(f"no element {eid!r} (it may have been deleted in the app)")
        return e

    def _changed(self, select: Optional[List[str]] = None):
        """Bring the app's editor up to date, keep its selection where it makes sense."""
        w = self.w
        keep = [i for i in (select if select is not None else w.preview.selected) if w.layout_obj.find(i)]
        w._refresh_list()
        w.preview.set_selection(keep)
        w._show_selection()
        w._update_layout_label()
        w.preview.update()
        w._dirty()

    def op(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """One edit. Returns {"state": …, plus "id"/"ids" for new elements, "file" for saves}."""
        name = data.get("op")
        if name not in OPS:
            raise LayoutError(f"unknown layout op {name!r}")
        out = getattr(self, "_" + name)(data) or {}
        out["state"] = self.state()
        return out

    def _set(self, d):
        e = self._find(d.get("id"))
        fields = d.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise LayoutError("nothing to set")
        clean = {f: clean_value(e, f, v) for f, v in fields.items()}     # all or nothing
        for f, v in clean.items():
            setattr(e, f, v)
        for pos, size in (("x", "w"), ("y", "h")):                      # keep it on the canvas
            if getattr(e, pos) + getattr(e, size) > 1.0:
                if size in clean and pos not in clean:
                    setattr(e, size, round(1.0 - getattr(e, pos), 4))      # resized past the edge
                else:
                    setattr(e, pos, round(1.0 - getattr(e, size), 4))      # moved past the edge
        self._changed()

    def _set_layout(self, d):
        lay = self.w.layout_obj
        if "name" in d:
            lay.name = str(d["name"])[:200] or "Untitled"
        if "background" in d:
            try:
                parse_color(str(d["background"]))
            except ValueError:
                raise LayoutError(f"not a color: {d['background']!r}") from None
            lay.background = str(d["background"]).strip().upper()
        if "no_signal" in d:
            if d["no_signal"] not in NO_SIGNAL_MODES:
                raise LayoutError(f"no_signal can't be {d['no_signal']!r}")
            lay.no_signal = d["no_signal"]
        colors = d.get("colors") or {}
        if not isinstance(colors, dict):
            raise LayoutError("colors must be an object")
        for k, v in colors.items():
            if k not in DEFAULT_COLORS:
                raise LayoutError(f"no layout color {k!r}")
            try:
                parse_color(str(v))
            except ValueError:
                raise LayoutError(f"not a color: {v!r}") from None
            lay.colors[k] = str(v).strip().upper()
        self._changed()

    def _add(self, d):
        t = d.get("type")
        if t not in ELEMENT_TYPES:
            raise LayoutError(f"no element type {t!r}")
        if len(self.w.layout_obj.elements) >= MAX_ELEMENTS:
            raise LayoutError("that's a lot of elements — delete some first")
        e = default_element(t)
        self.w.layout_obj.elements.append(e)
        self._changed([e.id])
        return {"id": e.id}

    def _ids(self, d) -> List[Element]:
        ids = d.get("ids") or ([d["id"]] if d.get("id") else [])
        if not ids:
            raise LayoutError("no element given")
        return [self._find(i) for i in ids]

    def _duplicate(self, d):
        els = self._ids(d)
        if len(self.w.layout_obj.elements) + len(els) > MAX_ELEMENTS:
            raise LayoutError("that's a lot of elements — delete some first")
        new = []
        for e in els:
            c = e.copy()
            c.name = f"{e.name} copy"
            c.x = min(c.x + 0.02, 1 - c.w)
            c.y = min(c.y + 0.02, 1 - c.h)
            self.w.layout_obj.elements.append(c)
            new.append(c.id)
        self._changed(new)
        return {"ids": new}

    def _delete(self, d):
        for e in self._ids(d):
            self.w.layout_obj.elements.remove(e)
        self._changed([])

    def _move(self, d):
        """direction +1 = up the list = drawn later = in front."""
        e = self._find(d.get("id"))
        els = self.w.layout_obj.elements
        step = 1 if int(d.get("direction", 1)) > 0 else -1
        if d.get("to") in ("front", "back"):
            els.remove(e)
            els.append(e) if d["to"] == "front" else els.insert(0, e)
        else:
            i = els.index(e)
            j = max(0, min(len(els) - 1, i + step))
            els[i], els[j] = els[j], els[i]
        self._changed()

    def _undo(self, d):
        self.w._undo()
        self.w.preview.update()

    def _redo(self, d):
        self.w._redo()
        self.w.preview.update()

    def _new(self, d):
        lay = Layout(name=str(d.get("name") or "Untitled")[:200], background="#000000", elements=[])
        self.w._set_layout(lay)

    def _save(self, d):
        w = self.w
        if not w.layout_path:
            return self._save_as({"name": w.layout_obj.name})
        path = files.absolute(w.layout_path, "layouts")
        try:
            w.layout_obj.save(path)
        except OSError as e:
            raise LayoutError(f"couldn't save: {e}") from None
        w.layout_path = path
        w._update_layout_label()
        w._autosave_layout()
        return {"file": os.path.basename(path)}

    def _save_as(self, d):
        """Save into the layouts folder as NAME.json (overwrite only if asked)."""
        w = self.w
        fname = safe_filename(str(d.get("name") or w.layout_obj.name))
        folder = files.last_dir("layouts")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, fname)
        if os.path.exists(path) and not d.get("overwrite") and os.path.abspath(path) != os.path.abspath(
                w.layout_path or ""):
            raise LayoutError(f"{fname} already exists — pick another name, or overwrite it")
        try:
            w.layout_obj.save(path)
        except OSError as e:
            raise LayoutError(f"couldn't save: {e}") from None
        w.layout_path = path
        w._update_layout_label()
        w._autosave_layout()
        return {"file": fname}

    # -- import ------------------------------------------------------------------------------

    def import_layout(self, d: Dict[str, Any]) -> Dict[str, Any]:
        """{filename, content (the .json text), open: bool, overwrite: bool} → saved in the layouts
        folder (and opened). Returns {"file": name, "state": …}."""
        content = d.get("content")
        if not isinstance(content, str) or not content.strip():
            raise LayoutError("the file is empty")
        try:
            lay = Layout.from_json(content)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            raise LayoutError(f"that isn't a Confidence layout ({e})") from None
        if len(lay.elements) > MAX_ELEMENTS:
            raise LayoutError("that layout has too many elements")
        fname = safe_filename(str(d.get("filename") or lay.name))
        folder = files.last_dir("layouts")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, fname)
        if os.path.exists(path) and not d.get("overwrite"):
            stem = fname[:-5]
            n = 2
            while os.path.exists(os.path.join(folder, f"{stem} {n}.json")):
                n += 1
            fname = f"{stem} {n}.json"
            path = os.path.join(folder, fname)
        try:
            lay.save(path)
        except OSError as e:
            raise LayoutError(f"couldn't save: {e}") from None
        if d.get("open", True):
            self.w._set_layout(Layout.load(path), path)
        return {"file": fname, "state": self.state()}


__all__ = ["LayoutEditor", "LayoutError", "OPS", "clean_value", "safe_filename", "schema"]
