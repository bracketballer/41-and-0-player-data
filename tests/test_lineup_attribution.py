import json
import unittest
from pathlib import Path

from bracketballer_data.lineup_attribution import (
    CoverageStat,
    DisagreementStat,
    ResolvedLineup,
    compute_coverage_stats,
    compute_disagreement_stats,
    extract_on_floor_by_team,
    lineup_matches_any,
    resolve_event_lineups,
    resolve_event_side,
    resolve_team_lineup,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# Real player_shot_events.raw_payload row (source_play_id 41344836, Cornell @
# Samford, 2025-12-08). All ten onFloor ids were verified against the live
# `players` table: 2384 (Jacob Beccles), 2372 (Josh Baldwin), and 3685 (Zion
# Wilburn) have no `players` row (never ingested as shot-takers); the other
# seven do, and each `players.id` equals the onFloor `id` directly.
CORNELL_SAMFORD_EVENT = load_fixture("onfloor_regular_2026.json")
KNOWN_PLAYER_IDS = {2371, 2385, 2370, 4598, 31618, 549, 5536}
UNRESOLVABLE_IDS = {2384, 2372, 3685}


class ExtractOnFloorByTeamTests(unittest.TestCase):
    def test_splits_real_payload_into_two_teams_of_five(self):
        by_team = extract_on_floor_by_team(CORNELL_SAMFORD_EVENT)
        self.assertEqual(set(by_team), {"Cornell", "Samford"})
        self.assertEqual(len(by_team["Cornell"]), 5)
        self.assertEqual(len(by_team["Samford"]), 5)

    def test_missing_on_floor_key_returns_empty(self):
        self.assertEqual(extract_on_floor_by_team({"id": 1}), {})

    def test_non_list_on_floor_returns_empty(self):
        self.assertEqual(extract_on_floor_by_team({"onFloor": "not-a-list"}), {})

    def test_drops_malformed_entries_without_raising(self):
        payload = {
            "onFloor": [
                {"id": 1, "team": "A"},
                {"id": None, "team": "A"},  # non-integer id
                {"id": 2, "team": None},  # missing team name
                "not-a-mapping",
                {"id": 3, "team": "A"},
            ]
        }
        by_team = extract_on_floor_by_team(payload)
        self.assertEqual(by_team, {"A": [1, 3]})


class ResolveTeamLineupTests(unittest.TestCase):
    def test_full_resolution_is_valid_five(self):
        resolution = resolve_team_lineup("Cornell", [1, 2, 3, 4, 5], {1, 2, 3, 4, 5})
        self.assertTrue(resolution.is_valid_five)
        self.assertEqual(resolution.resolved_count, 5)

    def test_unresolvable_teammate_breaks_valid_five(self):
        # Mirrors the real Cornell side: 5 on-floor entries, one (2384) absent
        # from `players`.
        resolution = resolve_team_lineup(
            "Cornell", [2384, 2371, 2385, 2372, 2370], KNOWN_PLAYER_IDS
        )
        self.assertEqual(resolution.on_floor_count, 5)
        self.assertEqual(resolution.resolved_count, 3)
        self.assertFalse(resolution.is_valid_five)

    def test_six_on_floor_entries_is_never_valid_even_if_all_resolve(self):
        resolution = resolve_team_lineup("A", [1, 2, 3, 4, 5, 6], {1, 2, 3, 4, 5, 6})
        self.assertEqual(resolution.resolved_count, 6)
        self.assertFalse(resolution.is_valid_five)

    def test_three_on_floor_entries_is_never_valid(self):
        resolution = resolve_team_lineup("A", [1, 2, 3], {1, 2, 3})
        self.assertFalse(resolution.is_valid_five)

    def test_duplicate_ids_collapse_via_frozenset_and_fail_valid_five(self):
        # 5 on-floor slots but only 4 distinct ids is a data anomaly, not a
        # valid five-player set.
        resolution = resolve_team_lineup("A", [1, 1, 2, 3, 4], {1, 2, 3, 4})
        self.assertEqual(resolution.on_floor_count, 5)
        self.assertEqual(resolution.resolved_count, 4)
        self.assertFalse(resolution.is_valid_five)


class ResolveEventLineupsTests(unittest.TestCase):
    def test_real_event_neither_side_is_a_valid_five(self):
        # Ground-truthed against the live `players` table: Cornell is missing
        # Jacob Beccles (2384) and Josh Baldwin (2372); Samford is missing
        # Zion Wilburn (3685). This single real event demonstrates exactly
        # the coverage gap T1 is measuring.
        resolved = resolve_event_lineups(CORNELL_SAMFORD_EVENT, KNOWN_PLAYER_IDS)
        self.assertEqual(set(resolved), {"Cornell", "Samford"})
        self.assertFalse(resolved["Cornell"].is_valid_five)
        self.assertEqual(resolved["Cornell"].resolved_count, 3)
        self.assertFalse(resolved["Samford"].is_valid_five)
        self.assertEqual(resolved["Samford"].resolved_count, 4)
        self.assertEqual(
            resolved["Samford"].resolved_player_ids, frozenset({4598, 31618, 549, 5536})
        )


class ResolveEventSideTests(unittest.TestCase):
    """`side="defense"` backs the AC's "attributing shots to specific
    defensive lineups" deliverable; `side="offense"` is the shooter's own
    team, reported as supporting context."""

    def test_defense_side_is_the_opponent_not_the_shooting_team(self):
        # Cornell shot the ball (raw["team"]); Samford is on defense.
        resolution = resolve_event_side(CORNELL_SAMFORD_EVENT, "defense", KNOWN_PLAYER_IDS)
        self.assertEqual(resolution.team, "Samford")
        self.assertEqual(resolution.resolved_count, 4)

    def test_offense_side_is_the_shooting_team(self):
        resolution = resolve_event_side(CORNELL_SAMFORD_EVENT, "offense", KNOWN_PLAYER_IDS)
        self.assertEqual(resolution.team, "Cornell")
        self.assertEqual(resolution.resolved_count, 3)

    def test_empty_on_floor_event_counts_as_a_zero_sized_side_not_dropped(self):
        # A pre-2024-style event with onFloor=[] must still contribute to the
        # per-event coverage denominator as an invalid five, not be excluded.
        payload = {"team": "A", "opponent": "B", "onFloor": []}
        resolution = resolve_event_side(payload, "defense", KNOWN_PLAYER_IDS)
        self.assertEqual(resolution.on_floor_count, 0)
        self.assertFalse(resolution.is_valid_five)

    def test_missing_opponent_name_does_not_raise(self):
        payload = {"team": "A", "onFloor": [{"id": 1, "team": "A"}]}
        resolution = resolve_event_side(payload, "defense", {1})
        self.assertEqual(resolution.on_floor_count, 0)
        self.assertFalse(resolution.is_valid_five)

    def test_invalid_side_argument_raises(self):
        with self.assertRaises(ValueError):
            resolve_event_side(CORNELL_SAMFORD_EVENT, "special_teams", KNOWN_PLAYER_IDS)


class CoverageStatsTests(unittest.TestCase):
    def test_coverage_percentage_is_exact(self):
        valid = ResolvedLineup("A", 5, frozenset({1, 2, 3, 4, 5}))
        invalid = ResolvedLineup("B", 5, frozenset({1, 2, 3}))
        stats = compute_coverage_stats(
            [(2026, valid), (2026, valid), (2026, invalid), (2025, valid)]
        )
        self.assertEqual(stats[2026].team_sides_seen, 3)
        self.assertEqual(stats[2026].valid_five_count, 2)
        self.assertAlmostEqual(stats[2026].coverage_pct, 200 / 3)
        self.assertEqual(stats[2025].coverage_pct, 100.0)

    def test_empty_season_coverage_is_none_not_zero_division(self):
        stat = CoverageStat(season=2026)
        self.assertIsNone(stat.coverage_pct)


class DisagreementStatsTests(unittest.TestCase):
    def test_matching_lineup_is_not_a_disagreement(self):
        derived = frozenset({1, 2, 3, 4, 5})
        stored = [frozenset({5, 4, 3, 2, 1})]  # order independent
        self.assertTrue(lineup_matches_any(derived, stored))

    def test_full_pipeline_counts_disagreement_and_missing_reference_separately(self):
        comparisons = [
            (2026, frozenset({1, 2, 3, 4, 5}), [frozenset({1, 2, 3, 4, 5})]),  # agree
            (2026, frozenset({1, 2, 3, 4, 6}), [frozenset({1, 2, 3, 4, 5})]),  # disagree
            (2026, frozenset({1, 2, 3, 4, 5}), None),  # no team_game_lineups rows
            (2026, frozenset({1, 2, 3, 4, 5}), []),  # empty rows, same as None
        ]
        stats = compute_disagreement_stats(comparisons)
        stat = stats[2026]
        self.assertEqual(stat.comparable_count, 2)
        self.assertEqual(stat.disagreement_count, 1)
        self.assertEqual(stat.no_reference_count, 2)
        self.assertEqual(stat.disagreement_rate_pct, 50.0)

    def test_no_comparable_games_is_none_not_zero_division(self):
        stat = DisagreementStat(season=2026)
        self.assertIsNone(stat.disagreement_rate_pct)


class Pre2024FixtureTests(unittest.TestCase):
    """A real 2020 row has an `onFloor` key but it is always empty, which is
    why the analysis script scopes to 2024-2026: it matches
    SHOOTING_SIGNALS.md's note that on-floor data is available "beginning
    primarily in 2024," and confirms extract_on_floor_by_team degrades to
    zero resolvable team-sides instead of raising on older rows."""

    def test_pre_2024_payload_has_empty_on_floor(self):
        payload = load_fixture("onfloor_pre2024.json")
        self.assertEqual(payload["season"], 2020)
        self.assertEqual(payload["onFloor"], [])
        by_team = extract_on_floor_by_team(payload)
        self.assertEqual(by_team, {})


if __name__ == "__main__":
    unittest.main()
