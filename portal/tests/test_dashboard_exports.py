import re
from pathlib import Path

DASH = Path(__file__).parent.parent / "app" / "templates" / "dashboard.html"


def test_every_hlt_name_the_3d_module_calls_is_exported():
    """The 3D module reads helpers off window.HLT (set up by the 2D script). A name it calls that was never exported only fails
    when that code path runs (a station with an active alert), and then the whole 3D scene silently stops drawing."""
    s = DASH.read_text(encoding="utf-8")
    i = s.index("window.HLT = {")
    j = s.index("};", i)
    exported = set(re.findall(r"[A-Za-z_]\w*", re.sub(r"//.*", "", s[i + len("window.HLT = {"):j])))
    exported |= set(re.findall(r"window\.HLT\.(\w+)\s*=", s))   # assigned separately, e.g. tsDate
    module = s[s.index('<script type="module">'):]
    used = set(re.findall(r"\bH\.([A-Za-z_]\w*)", module))
    assert used - exported == set(), f"the 3D module uses HLT names that are not exported: {sorted(used - exported)}"
