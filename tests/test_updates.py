"""Update checks, verified installs and reload, without network or editor."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


os.environ["UEFN_BRIDGE_NO_AUTOSTART"] = "1"
fake_unreal = sys.modules.get("unreal") or types.ModuleType("unreal")
fake_unreal.log = getattr(fake_unreal, "log", lambda *_: None)
fake_unreal.log_error = getattr(fake_unreal, "log_error", lambda *_: None)
fake_unreal.log_warning = getattr(fake_unreal, "log_warning", lambda *_: None)
sys.modules["unreal"] = fake_unreal

from bridge import server, updates  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import write_release_manifest  # noqa: E402


def manifest_for(version: str, files: dict) -> bytes:
    return json.dumps({
        "version": version,
        "files": {name: updates.content_digest(data) for name, data in files.items()},
    }).encode()


class FakeRepository:
    """Serves a manifest and files the way the raw content host would."""

    def __init__(self, version: str, files: dict, tamper: str = ""):
        self.manifest = manifest_for(version, files)
        self.files = dict(files)
        if tamper:
            self.files[tamper] = self.files[tamper] + b"# changed after the manifest\n"
        self.requested = []

    def __call__(self, url: str, timeout_s: float) -> bytes:
        self.requested.append(url)
        name = url.rsplit("/", 1)[-1]
        if name == updates.MANIFEST_NAME:
            return self.manifest
        return self.files[name]


class ReleaseManifestTests(unittest.TestCase):
    def test_checked_in_manifest_matches_the_bridge_files(self):
        on_disk = json.loads((REPO / "bridge" / updates.MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(
            on_disk, write_release_manifest.build_manifest(),
            "run: python tools/write_release_manifest.py",
        )

    def test_manifest_version_is_the_running_version(self):
        self.assertEqual(write_release_manifest.build_manifest()["version"], server.VERSION)

    def test_digest_ignores_windows_line_endings(self):
        self.assertEqual(updates.content_digest(b"a\r\nb\r\n"), updates.content_digest(b"a\nb\n"))


class CheckTests(unittest.TestCase):
    def test_newer_published_version_is_available(self):
        state = updates.check("0.5.0", FakeRepository("0.10.0", {"server.py": b"x"}))
        self.assertEqual((state["latest"], state["available"], state["error"]), ("0.10.0", True, None))

    def test_same_or_older_version_is_not_available(self):
        for published in ("0.5.0", "0.4.9"):
            with self.subTest(published=published):
                state = updates.check("0.5.0", FakeRepository(published, {"server.py": b"x"}))
                self.assertFalse(state["available"])

    def test_failures_are_reported_not_raised(self):
        def offline(_url, _timeout):
            raise OSError("network unreachable")

        for fetch in (offline, lambda *_: b"not json", lambda *_: b'{"version": "1.0", "files": {}}'):
            with self.subTest(fetch=fetch):
                state = updates.check("0.5.0", fetch)
                self.assertFalse(state["available"])
                self.assertIsNotNone(state["error"])

    def test_manifest_rejects_unsafe_file_names_and_digests(self):
        good = "a" * 64
        for files in ({"../server.py": good}, {"sub/server.py": good}, {"server.py": "abc"}, {"server.txt": good}):
            with self.subTest(files=files), self.assertRaises(updates.UpdateError):
                updates.parse_manifest(json.dumps({"version": "1.0.0", "files": files}).encode())


class ApplyUpdateTests(unittest.TestCase):
    OLD = {"server.py": b'VERSION = "0.5.0"\n', "routes.py": b"old routes\n"}
    NEW = {"server.py": b'VERSION = "0.6.0"\n', "routes.py": b"new routes\n", "extra.py": b"added\n"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "Content" / "Python" / "bridge"
        self.dir.mkdir(parents=True)
        for name, data in self.OLD.items():
            (self.dir / name).write_bytes(data)
        patcher = mock.patch.object(updates, "find_git_checkout", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, name):
        return (self.dir / name).read_bytes()

    def test_verified_files_replace_the_old_ones_with_a_backup(self):
        outcome = updates.apply_update(str(self.dir), "0.5.0", FakeRepository("0.6.0", self.NEW))
        self.assertTrue(outcome["updated"])
        self.assertEqual((outcome["version"], outcome["previous"]), ("0.6.0", "0.5.0"))
        for name, data in self.NEW.items():
            self.assertEqual(self.read(name), data)
        backup = self.dir / updates.BACKUP_DIR_NAME / "0.5.0"
        self.assertEqual((backup / "server.py").read_bytes(), self.OLD["server.py"])
        self.assertEqual((backup / "routes.py").read_bytes(), self.OLD["routes.py"])
        self.assertEqual(json.loads(self.read(updates.MANIFEST_NAME))["version"], "0.6.0")
        self.assertEqual(list(self.dir.glob("*.new")), [])

    def test_a_file_that_does_not_match_the_manifest_changes_nothing(self):
        with self.assertRaises(updates.UpdateError):
            updates.apply_update(str(self.dir), "0.5.0", FakeRepository("0.6.0", self.NEW, tamper="routes.py"))
        for name, data in self.OLD.items():
            self.assertEqual(self.read(name), data)
        self.assertFalse((self.dir / "extra.py").exists())
        self.assertFalse((self.dir / updates.BACKUP_DIR_NAME).exists())

    def test_a_failed_replacement_is_rolled_back(self):
        real_replace = os.replace
        calls = {"n": 0}

        def failing_replace(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("file in use")
            return real_replace(src, dst)

        with mock.patch.object(updates.os, "replace", side_effect=failing_replace), \
                self.assertRaises(updates.UpdateError):
            updates.apply_update(str(self.dir), "0.5.0", FakeRepository("0.6.0", self.NEW))
        for name, data in self.OLD.items():
            self.assertEqual(self.read(name), data)
        self.assertFalse((self.dir / "extra.py").exists())
        self.assertEqual(list(self.dir.glob("*.new")), [])

    def test_up_to_date_is_a_no_op_and_older_needs_allow_downgrade(self):
        same = updates.apply_update(str(self.dir), "0.5.0", FakeRepository("0.5.0", self.OLD))
        self.assertFalse(same["updated"])
        with self.assertRaises(updates.UpdateError):
            updates.apply_update(str(self.dir), "0.7.0", FakeRepository("0.6.0", self.NEW))
        down = updates.apply_update(str(self.dir), "0.7.0", FakeRepository("0.6.0", self.NEW), allow_downgrade=True)
        self.assertTrue(down["updated"])

    def test_a_second_concurrent_install_is_refused(self):
        self.assertTrue(updates._INSTALL_LOCK.acquire(blocking=False))
        try:
            with self.assertRaises(updates.UpdateError) as caught:
                updates.apply_update(str(self.dir), "0.5.0", FakeRepository("0.6.0", self.NEW))
        finally:
            updates._INSTALL_LOCK.release()
        self.assertIn("another bridge update", str(caught.exception))
        self.assertEqual(self.read("server.py"), self.OLD["server.py"])
        self.assertFalse((self.dir / updates.BACKUP_DIR_NAME).exists())

    def test_a_git_checkout_is_left_to_git(self):
        with mock.patch.object(updates, "find_git_checkout", return_value="/src/bridge-checkout"), \
                self.assertRaises(updates.UpdateError) as caught:
            updates.apply_update(str(self.dir), "0.5.0", FakeRepository("0.6.0", self.NEW))
        self.assertIn("git pull", str(caught.exception))
        self.assertEqual(self.read("server.py"), self.OLD["server.py"])


class RealFetchTests(unittest.TestCase):
    """The real fetch_url and apply_update against a local server laid out like the raw host."""

    def test_install_from_a_served_release(self):
        import functools
        import http.server
        import threading

        with tempfile.TemporaryDirectory() as served, tempfile.TemporaryDirectory() as installed:
            published = Path(served, "bridge")
            published.mkdir()
            files = {path.name: path.read_bytes() for path in (REPO / "bridge").glob("*.py")}
            for name, data in files.items():
                (published / name).write_bytes(data)
            (published / updates.MANIFEST_NAME).write_bytes(manifest_for("99.0.0", files))
            target = Path(installed, "bridge")
            target.mkdir()
            (target / "server.py").write_bytes(b'VERSION = "0.1.0"\n')

            class QuietHandler(http.server.SimpleHTTPRequestHandler):
                def log_message(self, *_):
                    pass

            handler = functools.partial(QuietHandler, directory=served)
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{httpd.server_port}/bridge/"
                with mock.patch.object(updates, "RAW_BASE", base), \
                        mock.patch.object(updates, "find_git_checkout", return_value=None):
                    state = updates.check("0.1.0")
                    outcome = updates.apply_update(str(target), "0.1.0")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
            self.assertEqual((state["latest"], state["available"]), ("99.0.0", True))
            self.assertTrue(outcome["updated"])
            for name, data in files.items():
                self.assertEqual((target / name).read_bytes(), data)


class SettingsTests(unittest.TestCase):
    def test_defaults_file_and_environment(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UEFN_BRIDGE_UPDATE_CHECK", None)
            os.environ.pop("UEFN_BRIDGE_AUTO_UPDATE", None)
            self.assertEqual(updates.load_settings(tmp), {"check_for_updates": True, "auto_update": False})
            Path(tmp, updates.SETTINGS_NAME).write_text('{"auto_update": true, "check_for_updates": "yes"}')
            self.assertEqual(updates.load_settings(tmp), {"check_for_updates": True, "auto_update": True})
            os.environ["UEFN_BRIDGE_AUTO_UPDATE"] = "0"
            os.environ["UEFN_BRIDGE_UPDATE_CHECK"] = "0"
            self.assertEqual(updates.load_settings(tmp), {"check_for_updates": False, "auto_update": False})

    def test_git_checkout_is_found_from_a_nested_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".git").mkdir()
            nested = Path(tmp, "bridge")
            nested.mkdir()
            self.assertEqual(updates.find_git_checkout(str(nested)), os.path.abspath(tmp))


class ServerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, server, "_update_state", None)
        self.addCleanup(setattr, server, "_reload_requested", False)

    def test_status_reports_the_last_check(self):
        state = {"current": server.VERSION, "latest": "9.0.0", "available": True,
                 "checked_at": "2026-10-13T00:00:00Z", "error": None}
        server._record_update_check(state)
        update = server._cmd_status()["update"]
        self.assertEqual((update["latest"], update["available"]), ("9.0.0", True))
        self.assertIn(update["install"], ("git", "copy", "unknown"))

    def test_update_requests_a_reload_that_the_tick_schedules(self):
        outcome = {"updated": True, "version": "9.0.0", "previous": server.VERSION, "files": ["server.py"], "backup": "b"}
        with mock.patch.object(server, "_bridge_dir", return_value=str(REPO / "bridge")), \
                mock.patch.object(server.updates, "apply_update", return_value=dict(outcome)), \
                mock.patch.object(server.updates, "check", return_value={"available": False}):
            result = server._cmd_bridge_update()
        self.assertTrue(result["reloading"])
        scheduled = []
        with mock.patch.object(server, "_schedule_editor_callback", side_effect=scheduled.append):
            server._tick(0.0)
            server._tick(0.0)
        self.assertEqual(scheduled, [server._reload_from_disk])

    def test_update_without_reload_leaves_the_bridge_running(self):
        outcome = {"updated": True, "version": "9.0.0", "previous": server.VERSION, "files": [], "backup": "b"}
        with mock.patch.object(server, "_bridge_dir", return_value=str(REPO / "bridge")), \
                mock.patch.object(server.updates, "apply_update", return_value=dict(outcome)), \
                mock.patch.object(server.updates, "check", return_value={"available": False}):
            result = server._cmd_bridge_update(reload=False)
        self.assertFalse(result["reloading"])
        self.assertFalse(server._reload_requested)

    def test_batch_refuses_network_commands_instead_of_running_them_on_the_editor_thread(self):
        import contextlib

        with mock.patch.object(server, "_cmd_bridge_update") as update, \
                mock.patch.object(server, "_cmd_bridge_check_update") as check, \
                mock.patch.object(server.unreal, "ScopedEditorTransaction",
                                  lambda _name: contextlib.nullcontext(), create=True):
            batch = server._cmd_batch_exec([{"command": "bridge.update"}, {"command": "bridge.check_update"}])
        self.assertEqual([entry["success"] for entry in batch["results"]], [False, False])
        self.assertTrue(all("network" in entry["error"] for entry in batch["results"]))
        update.assert_not_called()
        check.assert_not_called()

    def test_disabled_check_never_touches_the_network(self):
        with mock.patch.object(server.updates, "load_settings", return_value={"check_for_updates": False, "auto_update": False}), \
                mock.patch.object(server.updates, "check") as check:
            server._background_update_check(delay_s=0)
        check.assert_not_called()

    def test_reload_loads_fresh_sibling_modules(self):
        import routes as loose_routes  # the loose-module copy a reload re-imports

        before = sys.modules.get("routes")
        self.assertIs(before, loose_routes)
        with mock.patch.object(server, "stop"):
            server._reload_from_disk()
        self.assertIsNot(sys.modules.get("routes"), before)


if __name__ == "__main__":
    unittest.main()
