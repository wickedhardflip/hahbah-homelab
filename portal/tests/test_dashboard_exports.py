import re
from pathlib import Path

DASH = Path(__file__).parent.parent / "app" / "templates" / "dashboard.html"
MAP3D = Path(__file__).parent.parent / "app" / "static" / "map3d.js"


def test_every_hlt_name_the_3d_module_calls_is_exported():
    """The 3D module reads helpers off window.HLT (set up by the 2D script). A name it calls that was never exported only fails
    when that code path runs (a station with an active alert), and then the whole 3D scene silently stops drawing."""
    s = DASH.read_text(encoding="utf-8")
    i = s.index("window.HLT = {")
    j = s.index("};", i)
    exported = set(re.findall(r"[A-Za-z_]\w*", re.sub(r"//.*", "", s[i + len("window.HLT = {"):j])))
    exported |= set(re.findall(r"window\.HLT\.(\w+)\s*=", s))   # assigned separately, e.g. tsDate
    module = MAP3D.read_text(encoding="utf-8")
    used = set(re.findall(r"\bH\.([A-Za-z_]\w*)", module))
    assert used - exported == set(), f"the 3D module uses HLT names that are not exported: {sorted(used - exported)}"


def test_map3d_fx_module_is_imported_and_present():
    """The visuals module is loaded by relative import; a rename would silently leave 3D blank."""
    assert 'from "./map3d_fx.js"' in MAP3D.read_text(encoding="utf-8")
    assert (MAP3D.parent / "map3d_fx.js").is_file()
