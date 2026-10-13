"""Find out whether a newer bridge is published, and install it on request.

The repository's ``bridge/release.json`` lists the current version and the
SHA-256 of every file that ships in ``bridge/``. A check reads that manifest
from the default branch. An update downloads each listed file, verifies its
digest, keeps a backup of the files it replaces, and swaps the new files in.
Nothing here imports ``unreal``, so it is testable outside the editor.

Hashes are taken over the file's text with CRLF line endings normalized to
LF, so a Windows checkout and the bytes served by the repository agree.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.request
from typing import Callable, Dict, List, Optional

REPOSITORY = "Valid/uefn-python-bridge"
BRANCH = "main"
RAW_BASE = f"https://raw.githubusercontent.com/{REPOSITORY}/{BRANCH}/bridge/"
MANIFEST_NAME = "release.json"
SETTINGS_NAME = "settings.json"
BACKUP_DIR_NAME = ".bridge-backup"
FETCH_TIMEOUT_S = 10.0
MAX_FILE_BYTES = 4 * 1024 * 1024

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_FILE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*\.py$")

Fetch = Callable[[str, float], bytes]

# One installation at a time: a manual update and a startup auto-update share
# the staging names and the backup folder. The lock is kept on the sys module,
# so a reload, which imports a fresh copy of this module, still uses the same
# lock as an install that an older copy started.
_INSTALL_LOCK = sys.__dict__.setdefault("_uefn_bridge_install_lock", threading.Lock())


def install_in_progress() -> bool:
    """True while any copy of this module is installing bridge files."""
    return _INSTALL_LOCK.locked()


class UpdateError(RuntimeError):
    """An update could not be checked or applied; nothing was replaced."""


def parse_version(text: str) -> tuple:
    match = _VERSION.match(text.strip()) if isinstance(text, str) else None
    if not match:
        raise UpdateError(f"not a version: {text!r}")
    return tuple(int(part) for part in match.groups())


def content_digest(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def fetch_url(url: str, timeout_s: float = FETCH_TIMEOUT_S) -> bytes:
    """Read a URL over HTTPS with the default certificate checks."""
    request = urllib.request.Request(url, headers={"User-Agent": "uefn-python-bridge-updater"})
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        data = response.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise UpdateError(f"{url} is larger than {MAX_FILE_BYTES} bytes")
    return data


def parse_manifest(data: bytes) -> dict:
    """Validate a release manifest: a version and a digest per bridge file."""
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError(f"release manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise UpdateError("release manifest must be an object")
    parse_version(manifest.get("version", ""))
    files = manifest.get("files")
    if not isinstance(files, dict) or "server.py" not in files:
        raise UpdateError("release manifest must list its files, including server.py")
    for name, digest in files.items():
        if not isinstance(name, str) or not _FILE_NAME.match(name):
            raise UpdateError(f"release manifest names an unsafe file: {name!r}")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise UpdateError(f"release manifest has a malformed digest for {name}")
    return manifest


def check(current_version: str, fetch: Fetch = fetch_url) -> dict:
    """Compare the running version with the published one. Never raises."""
    state = {
        "current": current_version,
        "latest": None,
        "available": False,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "error": None,
    }
    try:
        manifest = parse_manifest(fetch(RAW_BASE + MANIFEST_NAME, FETCH_TIMEOUT_S))
        state["latest"] = manifest["version"]
        state["available"] = parse_version(manifest["version"]) > parse_version(current_version)
    except Exception as exc:  # network, TLS, HTTP and manifest errors alike
        state["error"] = f"{type(exc).__name__}: {exc}"
    return state


def find_git_checkout(bridge_dir: str) -> Optional[str]:
    """Return the git work tree that contains ``bridge_dir``, if any."""
    path = os.path.abspath(bridge_dir)
    while True:
        if os.path.exists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def load_settings(bridge_dir: str) -> dict:
    """Read optional ``settings.json`` next to the bridge files.

    ``check_for_updates`` (default true) and ``auto_update`` (default false).
    The environment variables UEFN_BRIDGE_UPDATE_CHECK and
    UEFN_BRIDGE_AUTO_UPDATE ("0" or "1") override the file.
    """
    settings = {"check_for_updates": True, "auto_update": False}
    path = os.path.join(bridge_dir, SETTINGS_NAME)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                for key in settings:
                    if isinstance(loaded.get(key), bool):
                        settings[key] = loaded[key]
        except (OSError, ValueError):
            pass
    for key, env in (("check_for_updates", "UEFN_BRIDGE_UPDATE_CHECK"), ("auto_update", "UEFN_BRIDGE_AUTO_UPDATE")):
        value = os.environ.get(env)
        if value in ("0", "1"):
            settings[key] = value == "1"
    return settings


def apply_update(
    bridge_dir: str,
    current_version: str,
    fetch: Fetch = fetch_url,
    allow_downgrade: bool = False,
) -> dict:
    """Install the published bridge files into ``bridge_dir``.

    Every file is downloaded and verified before anything on disk changes.
    The files being replaced are copied to a new
    ``.bridge-backup/<installed version>-<UTC time>/`` folder first, and
    restored if a replacement fails part way. A bridge running
    from a git checkout is left alone: update it with ``git pull``.
    """
    checkout = find_git_checkout(bridge_dir)
    if checkout:
        raise UpdateError(f"this bridge runs from the git checkout {checkout}; update it with git pull")
    if not _INSTALL_LOCK.acquire(blocking=False):
        raise UpdateError("another bridge update is being installed; check status when it finishes")
    try:
        return _install(bridge_dir, current_version, fetch, allow_downgrade)
    finally:
        _INSTALL_LOCK.release()


def installed_version(bridge_dir: str) -> Optional[str]:
    """The version of the files on disk, from their release.json, if readable."""
    try:
        with open(os.path.join(bridge_dir, MANIFEST_NAME), "rb") as handle:
            return parse_manifest(handle.read())["version"]
    except (OSError, UpdateError):
        return None


def _install(bridge_dir: str, current_version: str, fetch: Fetch, allow_downgrade: bool) -> dict:
    manifest = parse_manifest(fetch(RAW_BASE + MANIFEST_NAME, FETCH_TIMEOUT_S))
    latest = manifest["version"]
    # The files on disk can be newer than the running code (installed, not yet
    # reloaded); compare and label backups by what is on disk.
    installed = installed_version(bridge_dir) or current_version
    if parse_version(latest) == parse_version(installed):
        return {"updated": False, "version": latest, "installed": installed, "files": [],
                "reload_needed": parse_version(installed) != parse_version(current_version)}
    if parse_version(latest) < parse_version(installed) and not allow_downgrade:
        raise UpdateError(f"published version {latest} is older than the installed {installed}")

    downloads: Dict[str, bytes] = {}
    for name, digest in manifest["files"].items():
        data = fetch(RAW_BASE + name, FETCH_TIMEOUT_S)
        if content_digest(data) != digest:
            raise UpdateError(
                f"{name} does not match the release manifest; the repository may have "
                "changed during the download, so try again"
            )
        downloads[name] = data

    # A new folder for every install, so a later install never overwrites an earlier backup.
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_dir = os.path.join(bridge_dir, BACKUP_DIR_NAME, f"{installed}-{stamp}")
    suffix = 1
    while os.path.exists(backup_dir):
        suffix += 1
        backup_dir = os.path.join(bridge_dir, BACKUP_DIR_NAME, f"{installed}-{stamp}-{suffix}")
    staged: List[str] = []
    try:
        os.makedirs(backup_dir)
        for name, data in downloads.items():
            staging = os.path.join(bridge_dir, name + ".new")
            with open(staging, "wb") as handle:
                handle.write(data)
            staged.append(staging)
        manifest_staging = os.path.join(bridge_dir, MANIFEST_NAME + ".new")
        with open(manifest_staging, "wb") as handle:
            handle.write(json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n")
        staged.append(manifest_staging)
        for name in list(downloads) + [MANIFEST_NAME]:
            target = os.path.join(bridge_dir, name)
            if os.path.isfile(target):
                shutil.copy2(target, os.path.join(backup_dir, name))
    except OSError as exc:
        for path in staged:
            _remove_quietly(path)
        shutil.rmtree(backup_dir, ignore_errors=True)
        raise UpdateError(f"could not stage the update: {exc}") from exc

    replaced: List[str] = []
    try:
        for name in list(downloads) + [MANIFEST_NAME]:
            os.replace(os.path.join(bridge_dir, name + ".new"), os.path.join(bridge_dir, name))
            replaced.append(name)
    except OSError as exc:
        for name in replaced:
            saved = os.path.join(backup_dir, name)
            if os.path.isfile(saved):
                shutil.copy2(saved, os.path.join(bridge_dir, name))
            else:
                _remove_quietly(os.path.join(bridge_dir, name))
        for path in staged:
            _remove_quietly(path)
        raise UpdateError(f"replacing the bridge files failed and was rolled back: {exc}") from exc
    return {"updated": True, "version": latest, "previous": installed,
            "files": sorted(downloads), "backup": backup_dir, "reload_needed": True}


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
