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
from bridge.uefn_tool_policy import (
    CREATOR_EXPOSED_TOOLSET_COUNT,
    CREATOR_EXPOSED_TOOL_COUNT,
    CREATOR_EXPOSED_TOOLS,
)


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


if __name__ == "__main__":
    unittest.main()
