"""Introspect the UEFN Python API and dump results to JSON.

Run inside UEFN:  Tools > Execute Python Script > select this file

Output:  <Project>/Saved/uefn_api_introspection.json

This captures every class, enum, struct, and function exposed by the
`unreal` module so you can generate stubs, documentation, or capability
reports for your specific UEFN version.

Docs: https://github.com/Valid/uefn-agent-bridge
"""

import json
import os
import sys
import time
from typing import Any, Dict, List

import unreal


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _public_members(obj) -> List[str]:
    try:
        return [n for n in dir(obj) if not n.startswith("_")]
    except Exception:
        return []


def _member_kind(parent, name: str) -> str:
    try:
        val = getattr(parent, name)
    except Exception:
        return "inaccessible"
    if callable(val):
        return "method"
    if isinstance(val, property):
        return "property"
    if isinstance(val, (int, float, str, bool)):
        return "constant"
    return "attribute"


def _sig_hint(obj, name: str) -> str:
    try:
        doc = getattr(getattr(obj, name), "__doc__", "") or ""
        return doc.strip().split("\n")[0][:300]
    except Exception:
        return ""


def introspect() -> Dict:
    data: Dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "python_version": sys.version.split()[0],
        "engine_version": str(_safe(lambda: unreal.SystemLibrary.get_engine_version(), "unknown")),
        "functions": {},
        "classes": {},
        "enums": {},
        "structs": {},
    }

    for name in dir(unreal):
        if name.startswith("_"):
            continue
        obj = _safe(lambda: getattr(unreal, name))
        if obj is None:
            continue

        # Top-level callable (not a type)
        if callable(obj) and not isinstance(obj, type):
            data["functions"][name] = _sig_hint(unreal, name)
            continue

        if not isinstance(obj, type):
            continue

        info: Dict[str, Any] = {"bases": [], "members": {}}
        try:
            for base in obj.__mro__[1:]:
                if base.__name__ not in ("object", "type"):
                    info["bases"].append(base.__name__)
        except Exception:
            pass

        for m in _public_members(obj):
            info["members"][m] = {
                "kind": _member_kind(obj, m),
                "hint": _sig_hint(obj, m) if _member_kind(obj, m) == "method" else "",
            }

        # Classify
        try:
            if issubclass(obj, unreal.EnumBase):
                data["enums"][name] = info
                continue
        except (TypeError, AttributeError):
            pass
        try:
            if issubclass(obj, unreal.StructBase):
                data["structs"][name] = info
                continue
        except (TypeError, AttributeError):
            pass
        data["classes"][name] = info

    return data


def main() -> None:
    unreal.log("=== UEFN API introspection starting ===")
    result = introspect()

    totals = {k: len(v) for k, v in result.items() if isinstance(v, dict)}
    for k, v in totals.items():
        unreal.log(f"  {k}: {v}")
    unreal.log(f"  total: {sum(totals.values())}")

    out = os.path.join(unreal.Paths.project_saved_dir(), "uefn_api_introspection.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)
    unreal.log(f"=== Saved to {out} ===")


main()
