"""Rotation wire format, struct construction guard, and delete reporting."""

from __future__ import annotations

import ast
import contextlib
import os
import re
import sys
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

from bridge import server  # noqa: E402


REPO = Path(__file__).resolve().parents[1]


class EngineRotator:
    """Mirrors the engine constructor: positional parameters are (roll, pitch, yaw)."""

    def __init__(self, roll=0.0, pitch=0.0, yaw=0.0):
        self.roll, self.pitch, self.yaw = roll, pitch, yaw


class RotationWireFormatTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(server.unreal, "Rotator", EngineRotator, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assertAngles(self, rotator, pitch, yaw, roll):
        self.assertEqual((rotator.pitch, rotator.yaw, rotator.roll), (pitch, yaw, roll))

    def test_list_is_pitch_yaw_roll(self):
        self.assertAngles(server._rotator([10, 20, 30]), 10.0, 20.0, 30.0)

    def test_a_pure_turn_lands_on_yaw(self):
        self.assertAngles(server._rotator([0, 90, 0]), 0.0, 90.0, 0.0)

    def test_tuple_is_accepted(self):
        self.assertAngles(server._rotator((-30, 0, 5)), -30.0, 0.0, 5.0)

    def test_response_shaped_object_round_trips(self):
        self.assertAngles(server._rotator({"pitch": 1, "yaw": 2, "roll": 3}), 1.0, 2.0, 3.0)

    def test_missing_object_keys_default_to_zero(self):
        self.assertAngles(server._rotator({"yaw": 45}), 0.0, 45.0, 0.0)

    def test_malformed_rotations_are_rejected(self):
        for bad in ([1, 2], [1, 2, 3, 4], "0,90,0", 90, None, {"heading": 90}, ["a", 0, 0]):
            with self.subTest(rotation=bad), self.assertRaises(ValueError):
                server._rotator(bad)


# A struct whose positional parameter order differs from how it is usually described.
_ORDER_SENSITIVE_STRUCTS = {"Rotator", "Color"}


def _positional_struct_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _ORDER_SENSITIVE_STRUCTS
            and isinstance(func.value, ast.Name)
            and func.value.id == "unreal"
        ):
            yield node


_MARKDOWN_CALL = re.compile(r"unreal\.(Rotator|Color)\(([^()]*)\)")


class StructConstructionGuardTests(unittest.TestCase):
    """unreal.Rotator(0, 90, 0) pitches instead of turning; keep keywords everywhere."""

    def test_python_sources_build_order_sensitive_structs_by_keyword(self):
        offenders = []
        for path in sorted(REPO.glob("**/*.py")):
            if ".git" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for call in _positional_struct_calls(tree):
                offenders.append(f"{path.relative_to(REPO)}:{call.lineno}")
        self.assertEqual(offenders, [], "construct unreal.Rotator/unreal.Color by keyword")

    def test_documentation_examples_build_order_sensitive_structs_by_keyword(self):
        offenders = []
        for path in sorted(REPO.glob("*.md")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for match in _MARKDOWN_CALL.finditer(line):
                    args = [a.strip() for a in match.group(2).split(",") if a.strip()]
                    if any("=" not in a for a in args):
                        offenders.append(f"{path.name}:{lineno}: {match.group(0)}")
        self.assertEqual(offenders, [], "documented examples must use keyword arguments")

    def test_guard_detects_the_bug_it_exists_for(self):
        bad = ast.parse("import unreal\nr = unreal.Rotator(0, 90, 0)\nc = unreal.Color(255, 0, 0, 255)\n")
        good = ast.parse("import unreal\nr = unreal.Rotator(roll=0, pitch=0, yaw=90)\nz = unreal.Rotator()\n")
        self.assertEqual(len(list(_positional_struct_calls(bad))), 2)
        self.assertEqual(list(_positional_struct_calls(good)), [])
        self.assertIsNotNone(_MARKDOWN_CALL.search("unreal.Rotator(-30, 0, 0)"))


class FakeActor:
    def __init__(self, label, path):
        self.label, self.path = label, path

    def get_actor_label(self):
        return self.label

    def get_path_name(self):
        return self.path


class FakeActorSubsystem:
    def __init__(self, actors, refuse):
        self.actors, self.refuse, self.destroyed = actors, set(refuse), []

    def get_all_level_actors(self):
        return list(self.actors)

    def destroy_actor(self, actor):
        if actor.label in self.refuse:
            return False
        self.destroyed.append(actor.label)
        return True


class DeleteReportingTests(unittest.TestCase):
    def test_refused_and_missing_targets_are_reported_not_counted(self):
        subsystem = FakeActorSubsystem(
            [FakeActor("Keep", "/Game/L.L:Keep"), FakeActor("Gone", "/Game/L.L:Gone"), FakeActor("Locked", "/Game/L.L:Locked")],
            refuse={"Locked"},
        )
        with mock.patch.object(server, "_get_actor_sub", return_value=subsystem), \
                mock.patch.object(server.unreal, "ScopedEditorTransaction", lambda _name: contextlib.nullcontext(), create=True):
            result = server._cmd_actors_delete(["Gone", "Locked", "/Game/L.L:Nowhere"])
        self.assertEqual(result["deleted"], ["Gone"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["failed"], [
            {"target": "Locked", "reason": "destroy_actor returned False"},
            {"target": "/Game/L.L:Nowhere", "reason": "not found"},
        ])
        self.assertEqual(subsystem.destroyed, ["Gone"])

    def test_targets_match_by_path_as_well_as_label(self):
        subsystem = FakeActorSubsystem([FakeActor("Cube", "/Game/L.L:StaticMeshActor_3")], refuse=())
        with mock.patch.object(server, "_get_actor_sub", return_value=subsystem), \
                mock.patch.object(server.unreal, "ScopedEditorTransaction", lambda _name: contextlib.nullcontext(), create=True):
            result = server._cmd_actors_delete(["/Game/L.L:StaticMeshActor_3"])
        self.assertEqual((result["count"], result["failed"]), (1, []))


if __name__ == "__main__":
    unittest.main()
