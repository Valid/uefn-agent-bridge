"""Tests for deterministic direct-Python and native-tool routing metadata."""

from __future__ import annotations

import unittest

from bridge.routes import (
    PYTHON_COMMANDS,
    READ_ONLY_TOOL_ROUTES,
    TOOLSET_ROUTES,
    UEFN_EXPOSED_TOOLSETS,
    is_tool_allowed,
    short_tool_name,
)
from bridge.routes import default_deadline_s
from bridge.uefn_tool_policy import (
    CREATOR_EXPOSED_TOOLSET_COUNT,
    CREATOR_EXPOSED_TOOL_COUNT,
    CREATOR_EXPOSED_TOOLS,
    check_unscoped_save,
)

_ASSET_TOOLS = "editor_toolset.toolsets.asset.AssetTools"


class RouteTests(unittest.TestCase):
    def test_pinned_policy_counts_match_manifest(self):
        self.assertEqual(len(CREATOR_EXPOSED_TOOLS), CREATOR_EXPOSED_TOOLSET_COUNT)
        self.assertEqual(
            sum(len(tools) for tools in CREATOR_EXPOSED_TOOLS.values()),
            CREATOR_EXPOSED_TOOL_COUNT,
        )

    def test_direct_and_toolset_commands_do_not_collide(self):
        self.assertFalse(PYTHON_COMMANDS.intersection(TOOLSET_ROUTES))

    def test_every_alias_targets_creator_exposed_toolset(self):
        for alias, (toolset_name, tool_name) in TOOLSET_ROUTES.items():
            with self.subTest(alias=alias):
                self.assertIn(toolset_name, UEFN_EXPOSED_TOOLSETS)
                self.assertTrue(is_tool_allowed(toolset_name, tool_name))

    def test_read_only_classification_is_known_alias(self):
        self.assertTrue(READ_ONLY_TOOL_ROUTES.issubset(TOOLSET_ROUTES))
        self.assertNotIn("entities.create", READ_ONLY_TOOL_ROUTES)
        self.assertIn("entities.find", READ_ONLY_TOOL_ROUTES)

    def test_fully_qualified_tool_name_is_shortened(self):
        self.assertEqual(
            short_tool_name(
                "ValkyrieToolset.SessionToolset",
                "ValkyrieToolset.SessionToolset.GetSessionStatus",
            ),
            "GetSessionStatus",
        )

    def test_mismatched_qualified_tool_name_is_rejected(self):
        self.assertIsNone(
            short_tool_name(
                "ValkyrieToolset.SessionToolset",
                "ValkyrieToolset.EntityToolset.FindEntities",
            )
        )

    def test_widget_aliases_route_to_umg_verse_field_and_mvvm_tools(self):
        self.assertEqual(
            TOOLSET_ROUTES["widgets.add_verse_field"],
            ("VerseFieldsToolset.VerseFieldsToolset", "AddVerseField"),
        )
        self.assertEqual(
            TOOLSET_ROUTES["widgets.verse_field_types"],
            ("VerseFieldsToolset.VerseFieldsToolset", "GetSupportedVerseFieldTypes"),
        )
        self.assertEqual(
            TOOLSET_ROUTES["widgets.tree"], ("UMGToolSet.UMGToolSet", "GetWidgets")
        )
        self.assertEqual(
            TOOLSET_ROUTES["widgets.view_bindings"],
            ("MVVMToolset.MVVMToolset", "ListWidgetViewBindings"),
        )
        for alias in ("widgets.tree", "widgets.verse_fields", "widgets.verse_field_types"):
            self.assertIn(alias, READ_ONLY_TOOL_ROUTES)
        for alias in ("widgets.add", "widgets.add_verse_field", "widgets.compile"):
            self.assertNotIn(alias, READ_ONLY_TOOL_ROUTES)

    def test_widget_compile_gets_a_longer_deadline(self):
        self.assertEqual(default_deadline_s("widgets.compile"), 120.0)

    def test_programmatic_script_toolset_stays_out_of_policy(self):
        self.assertFalse(
            is_tool_allowed(
                "editor_toolset.toolsets.programmatic.ProgrammaticToolset",
                "execute_tool_script",
            )
        )


class UnscopedSaveGuardTests(unittest.TestCase):
    def test_empty_missing_or_blank_paths_are_refused(self):
        for arguments in ({"asset_paths": []}, {}, None, {"asset_paths": [""]},
                          {"asset_paths": "/Game/A"}):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    check_unscoped_save(_ASSET_TOOLS, "save_assets", arguments)

    def test_qualified_tool_name_is_guarded_too(self):
        with self.assertRaises(ValueError):
            check_unscoped_save(
                _ASSET_TOOLS, _ASSET_TOOLS + ".save_assets", {"asset_paths": []}
            )

    def test_named_paths_or_explicit_opt_in_pass(self):
        check_unscoped_save(_ASSET_TOOLS, "save_assets", {"asset_paths": ["/Game/UI/UW_Menu"]})
        check_unscoped_save(_ASSET_TOOLS, "save_assets", {"asset_paths": []}, allow_save_all=True)

    def test_other_tools_are_untouched(self):
        check_unscoped_save(_ASSET_TOOLS, "is_dirty", {})
        check_unscoped_save("UMGToolSet.UMGToolSet", "CompileWidgetBlueprint", {})


if __name__ == "__main__":
    unittest.main()
