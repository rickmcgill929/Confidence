"""Editing and importing layouts from the browser remote."""
import json
import os

import pytest

from test_remote import Client, free_port, qapp, window  # noqa: F401  (fixtures)


def test_schema_matches_the_app(qapp, window):
    from dsan.clock.layout import COMMON_FIELDS, TYPE_FIELDS
    from dsan.clock.remote import RemoteAPI
    s = RemoteAPI(window).layout_schema()
    assert s["common"] == COMMON_FIELDS and s["type_fields"] == TYPE_FIELDS
    f = s["fields"]
    assert f["align"]["kind"] == "choice" and {"value": "center", "label": "Center"} in f["align"]["choices"]
    assert f["color"]["label"] == "Color" and f["color"]["kind"] == "color"
    assert f["x"]["kind"] == "pct" and f["visible"]["kind"] == "bool" and f["frame_px"]["kind"] == "int"
    assert f["font"]["kind"] == "font" and isinstance(s["fonts"], list)
    assert [c["key"] for c in s["layout_colors"]][:2] == ["running", "idle"]
    assert {t["value"] for t in s["types"]} >= {"timer", "label", "cue", "message"}


def test_edit_ops(qapp, window, tmp_path):
    from dsan.clock.remote import RemoteAPI, RemoteError
    w = window
    api = RemoteAPI(w)
    ed = api.layouts
    st = ed.state()
    assert st["rev"] and st["aspect"] > 0 and st["layout"]["elements"]
    n = len(w.layout_obj.elements)

    r = api.act({"action": "layout_edit", "op": "add", "type": "label"})
    lid = r["id"]
    assert len(r["state"]["layout"]["elements"]) == n + 1 and w.preview.selected == [lid]
    assert w.element_list.count() == n + 1                               # the app's list follows
    api.act({"action": "layout_edit", "op": "set", "id": lid,
             "fields": {"text": "Hello {program}", "align": "left", "color": "#ff0000", "x": 0.9, "w": 0.3}})
    e = w.layout_obj.find(lid)
    assert (e.text, e.align, e.color) == ("Hello {program}", "left", "#FF0000")
    assert abs(e.x + e.w - 1.0) < 1e-6 and e.w == 0.3                     # moved past the edge → kept on canvas
    api.act({"action": "layout_edit", "op": "set", "id": lid, "fields": {"w": 0.5}})
    assert abs(e.x + e.w - 1.0) < 1e-6                                    # resized past the edge → trimmed
    assert w.props.element is e and w.props._form.rowCount() > 5          # the app's properties panel shows it

    for bad in ({"fields": {"align": "middle"}}, {"fields": {"color": "red"}}, {"fields": {"cue_style": "frame"}},
                {"fields": {"x": "left"}}, {"fields": {}}, {"id": "nope", "fields": {"text": "x"}}):
        with pytest.raises(RemoteError):
            api.act({"action": "layout_edit", "op": "set", "id": lid, **bad})
    assert e.align == "left"                                              # nothing half-applied

    r = api.act({"action": "layout_edit", "op": "duplicate", "id": lid})
    dup = w.layout_obj.find(r["ids"][0])
    assert dup.name.endswith("copy") and dup.text == e.text
    els = w.layout_obj.elements
    api.act({"action": "layout_edit", "op": "move", "id": dup.id, "to": "back"})
    assert els[0] is dup
    api.act({"action": "layout_edit", "op": "move", "id": dup.id, "direction": 1})
    assert els[1] is dup
    api.act({"action": "layout_edit", "op": "delete", "ids": [dup.id]})
    assert w.layout_obj.find(dup.id) is None

    api.act({"action": "layout_edit", "op": "set_layout", "name": "Web look", "background": "#101010",
             "no_signal": "hide", "colors": {"running": "#00ff00"}})
    lay = w.layout_obj
    assert (lay.name, lay.background, lay.no_signal, lay.colors["running"]) == ("Web look", "#101010", "hide", "#00FF00")
    for bad in ({"no_signal": "panic"}, {"colors": {"nope": "#fff"}}, {"background": "blue"}):
        with pytest.raises(RemoteError):
            api.act({"action": "layout_edit", "op": "set_layout", **bad})

    # undo walks back over web edits, like the app's own
    w._commit_history()
    before = w.layout_obj.name
    api.act({"action": "layout_edit", "op": "set_layout", "name": "Changed"})
    w._commit_history()
    r = api.act({"action": "layout_edit", "op": "undo"})
    assert w.layout_obj.name == before and r["state"]["can_redo"]
    api.act({"action": "layout_edit", "op": "redo"})
    assert w.layout_obj.name == "Changed"

    # save / save as into the layouts folder
    r = api.act({"action": "layout_edit", "op": "save_as", "name": "Stage: Left?"})
    assert r["file"] == "Stage- Left.json" and os.path.isfile(tmp_path / "Stage- Left.json")
    assert r["state"]["in_layouts_folder"] and r["state"]["file"] == "Stage- Left.json"
    w.layout_path = None
    with pytest.raises(RemoteError, match="already exists"):
        api.act({"action": "layout_edit", "op": "save_as", "name": "Stage- Left"})
    api.act({"action": "layout_edit", "op": "save_as", "name": "Stage- Left", "overwrite": True})
    api.act({"action": "layout_edit", "op": "set_layout", "name": "Saved again"})
    api.act({"action": "layout_edit", "op": "save"})
    assert json.loads((tmp_path / "Stage- Left.json").read_text())["name"] == "Saved again"
    api.act({"action": "layout_edit", "op": "new", "name": "Blank"})
    assert w.layout_obj.elements == [] and w.layout_path is None
    with pytest.raises(RemoteError, match="unknown layout op"):
        api.act({"action": "layout_edit", "op": "explode"})


def test_import(qapp, window, tmp_path):
    from dsan.clock.layout import PRESETS
    from dsan.clock.remote import RemoteAPI, RemoteError
    w = window
    api = RemoteAPI(w)
    text = PRESETS["Limitimer + Millumin"]().to_json()
    r = api.act({"action": "layout_import", "filename": "Show A.json", "content": text})
    assert r["file"] == "Show A.json" and w.layout_obj.name == "Limitimer + Millumin"
    assert os.path.basename(w.layout_path) == "Show A.json"
    r = api.act({"action": "layout_import", "filename": "Show A.json", "content": text, "open": False})
    assert r["file"] == "Show A 2.json" and os.path.basename(w.layout_path) == "Show A.json"   # not overwritten
    r = api.act({"action": "layout_import", "filename": "Show A.json", "content": text, "overwrite": True,
                 "open": False})
    assert r["file"] == "Show A.json"
    assert "Show A 2.json" in api.saved_layouts()
    for bad in ("", "not json", json.dumps({"elements": [{"type": "rocket"}]}), json.dumps([1, 2])):
        with pytest.raises(RemoteError):
            api.act({"action": "layout_import", "filename": "x.json", "content": bad})
    r = api.act({"action": "layout_import", "filename": "../../etc/evil", "content": text, "open": False})
    assert r["file"] == "etc-evil.json" and (tmp_path / "etc-evil.json").is_file()


def test_http_routes(qapp, window):
    w = window
    port = free_port()
    w.remote_port.setValue(port)
    w.remote_enable.setChecked(True)
    c = Client(qapp, port)
    code, ctype, body = c.request("/editor", raw=True)
    assert code == 200 and "text/html" in ctype and b"Layout editor" in body
    code, ctype, body = c.request("/", raw=True)
    assert b'href="/editor"' in body and b'id="importlayout"' in body and b'id="downloadlayout"' in body
    code, st = c.request("/api/layout")
    assert code == 200 and st["layout"]["elements"]
    code, same = c.request(f"/api/layout?rev={st['rev']}")
    assert same == {"rev": st["rev"], "same": True}
    code, schema = c.request("/api/layout/schema")
    assert code == 200 and "fields" in schema
    w.layout_obj.name = "Ünïcode \"look\""
    code, ctype, body = c.request("/api/layout.json", raw=True)
    assert code == 200 and json.loads(body)["name"] == "Ünïcode \"look\""
    code, r = c.act(action="layout_edit", op="add", type="clock")
    assert code == 200 and r["id"] and r["state"]["rev"] != st["rev"]
    code, r = c.act(action="layout_edit", op="set", id="missing", fields={"x": 0.1})
    assert code == 400 and "no element" in r["error"]
    big = {"action": "layout_import", "content": "x" * (3 * 1024 * 1024)}
    code, r = c.request("/api/action", big)
    assert code == 413
    w.remote_pin.setText("4321")
    w._remote_pin_changed()
    assert c.request("/api/layout")[0] == 401 and c.request("/api/layout.json", raw=True)[0] == 401
