"""Write bridge/release.json: the bridge version and a digest of each file in bridge/.

Run it after changing any file in bridge/ or the bridge version:

    python tools/write_release_manifest.py

Installed bridges read this manifest from the default branch to learn that a
newer version exists, and to verify each file they download before
installing it (bridge/updates.py). tests/test_updates.py fails while the
manifest is out of date.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / "bridge"
sys.path.insert(0, str(BRIDGE))

from updates import MANIFEST_NAME, content_digest  # noqa: E402


def bridge_version() -> str:
    match = re.search(r'^VERSION = "([^"]+)"$', (BRIDGE / "server.py").read_text(encoding="utf-8"), re.M)
    if not match:
        raise SystemExit("bridge/server.py has no VERSION line")
    return match.group(1)


def build_manifest() -> dict:
    files = {path.name: content_digest(path.read_bytes()) for path in sorted(BRIDGE.glob("*.py"))}
    return {"version": bridge_version(), "files": files}


def render(manifest: dict) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def main() -> None:
    target = BRIDGE / MANIFEST_NAME
    target.write_text(render(build_manifest()), encoding="utf-8", newline="\n")
    print(f"wrote {target.relative_to(REPO)}")


if __name__ == "__main__":
    main()
