from __future__ import annotations

import unittest

from query_builders import (
    average_completion_query,
    leaderboard_query,
    matchup_query,
    quicklook_query,
    quicksplits_query,
)


class QueryBuilderTest(unittest.TestCase):
    def test_average_completion_preserves_legacy_surface(self) -> None:
        self.assertEqual(
            average_completion_query("DesktopFolder"),
            "players | filter uuid(DesktopFolder) | extract nick average_completion",
        )

    def test_quicklook_and_quicksplits_are_explicitly_seasoned(self) -> None:
        quicklook = quicklook_query("DesktopFolder", 2)
        quicksplits = quicksplits_query("DesktopFolder", 2)
        self.assertIn("index s2 | filter uuid(DesktopFolder)", quicklook)
        self.assertIn("splits.get_if story.follow_ender_eye", quicklook)
        self.assertIn("index s2 | filter uuid(DesktopFolder)", quicksplits)
        self.assertIn("splits.diff end.root projectelo.timeline.dragon_death", quicksplits)

    def test_player_split_average_uses_supported_tuple_filtering(self) -> None:
        query = leaderboard_query(
            "average_stronghold", 11, player="DesktopFolder"
        )
        self.assertIn("filter uuid(DesktopFolder) | players | extract uuid | assign target", query)
        self.assertIn("keepifattrcontained 0 target", query)
        self.assertNotIn("enumerate | filter 0(", query)
        self.assertNotIn("@player!!", query)

    def test_every_leaderboard_variant_builds_with_and_without_player(self) -> None:
        values = [
            "pb",
            "elo",
            "average_completion",
            "average_stronghold",
            "average_end",
        ]

        for value in values:
            with self.subTest(value=value, player=False):
                self.assertTrue(leaderboard_query(value, 11).startswith("index s11"))
            with self.subTest(value=value, player=True):
                self.assertTrue(
                    leaderboard_query(value, 11, player="DesktopFolder").startswith(
                        "index s11"
                    )
                )

    def test_matchup_keeps_optional_season_behavior(self) -> None:
        self.assertEqual(
            matchup_query("Alice", "Bob", 2, "| extract winrate"),
            "index s2 | filter uuid(Alice) uuid(Bob) | players | filter uuid(Alice) | extract winrate",
        )


if __name__ == "__main__":
    unittest.main()
