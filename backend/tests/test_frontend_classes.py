"""Guard against layout drift: every static CSS class used in index.html must
be defined by the stylesheets.

This caught real bugs (Tailwind-style `flex`/`items-center` that never
existed, so rows silently stopped being flex containers; `callout-warn` vs
`callout-warning` losing their colours). Purely static, so it needs no
browser and runs with the rest of the suite."""

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"

# Classes that are deliberate hooks or state markers with no styling of their own.
NO_STYLE_NEEDED = {
    "btn-label",  # text hook inside buttons
}


def _defined_classes() -> set[str]:
    css = "".join(p.read_text() for p in (FRONTEND / "css").glob("*.css"))
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return set(re.findall(r"\.([A-Za-z_][\w-]*)", css))


def _used_classes() -> dict[str, int]:
    html = (FRONTEND / "index.html").read_text()
    used: dict[str, int] = {}
    # Static class attributes only: skip Alpine's :class / x-bind:class, whose
    # values are JavaScript expressions.
    for m in re.finditer(r'(?<![:\w.@-])class="([^"]*)"', html):
        for token in m.group(1).split():
            if re.fullmatch(r"[A-Za-z_][\w-]*", token):
                used[token] = used.get(token, 0) + 1
    return used


def test_index_html_uses_only_defined_classes():
    defined = _defined_classes()
    undefined = {
        name: n for name, n in _used_classes().items()
        if name not in defined and name not in NO_STYLE_NEEDED
    }
    assert not undefined, (
        "classes used in frontend/index.html but defined in no stylesheet "
        f"(typo, or a utility that does not exist here): {sorted(undefined)}"
    )
