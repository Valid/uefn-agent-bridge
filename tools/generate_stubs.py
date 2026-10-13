"""Generate .pyi type stubs from a live UEFN editor session.

Run inside UEFN:  Tools > Execute Python Script > select this file

Output:  <Project>/Saved/unreal.pyi

Point your IDE (VS Code, Cursor, etc.) at this file for autocomplete:
    "python.analysis.extraPaths": ["path/to/Saved"]

Docs: https://github.com/Valid/uefn-agent-bridge
"""

import os
import sys
import time
from typing import Any, List

import unreal


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _public(obj) -> List[str]:
    try:
        return sorted(n for n in dir(obj) if not n.startswith("_"))
    except Exception:
        return []


def _infer_type(parent, name: str) -> str:
    try:
        val = getattr(parent, name)
    except Exception:
        return "Any"
    if callable(val):
        return "Callable"
    if isinstance(val, bool):
        return "bool"
    if isinstance(val, int):
        return "int"
    if isinstance(val, float):
        return "float"
    if isinstance(val, str):
        return "str"
    return "Any"


def _extract_sig(obj, name: str) -> str:
    """Best-effort method signature from docstring."""
    try:
        doc = getattr(getattr(obj, name), "__doc__", "") or ""
        first = doc.strip().split("\n")[0]
        if "(" in first and ")" in first:
            return first
        return f"{name}(*args, **kwargs) -> Any"
    except Exception:
        return f"{name}(*args, **kwargs) -> Any"


def generate() -> str:
    lines = [
        '"""Auto-generated type stubs for the UEFN `unreal` module."""',
        f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"# Python: {sys.version.split()[0]}",
        "",
        "from typing import Any, Callable, List, Optional, overload",
        "",
    ]

    # Top-level functions
    for name in _public(unreal):
        obj = _safe(lambda n=name: getattr(unreal, n))
        if obj is None or isinstance(obj, type):
            continue
        if callable(obj):
            sig = _extract_sig(unreal, name)
            lines.append(f"def {sig}: ...")
            lines.append("")

    # Types (classes, enums, structs)
    for name in _public(unreal):
        obj = _safe(lambda n=name: getattr(unreal, n))
        if obj is None or not isinstance(obj, type):
            continue

        # Determine bases
        bases = []
        try:
            for b in obj.__mro__[1:]:
                if b.__name__ not in ("object", "type"):
                    bases.append(b.__name__)
        except Exception:
            pass
        base_str = f"({', '.join(bases)})" if bases else ""

        lines.append(f"class {name}{base_str}:")
        members = _public(obj)
        if not members:
            lines.append("    ...")
            lines.append("")
            continue

        for m in members:
            kind = "Callable"
            try:
                val = getattr(obj, m)
                if callable(val):
                    sig = _extract_sig(obj, m)
                    lines.append(f"    def {sig}: ...")
                else:
                    t = _infer_type(obj, m)
                    lines.append(f"    {m}: {t}")
            except Exception:
                lines.append(f"    {m}: Any")
        lines.append("")

    return "\n".join(lines)


def main():
    unreal.log("=== Generating .pyi stubs ===")
    content = generate()

    out = os.path.join(unreal.Paths.project_saved_dir(), "unreal.pyi")
    with open(out, "w", encoding="utf-8") as f:
        f.write(content)

    line_count = content.count("\n")
    unreal.log(f"=== Wrote {line_count:,} lines to {out} ===")


main()
