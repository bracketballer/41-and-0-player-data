import unittest

from bracketballer_data.matchup_assignment import (
    DefensiveMatchupPlayer,
    OffensiveMatchupPlayer,
    PERIMETER_SCORE_THRESHOLD,
    assign_matchups,
)


def offense(*, reverse=False, missing_height=False):
    rows = [
        OffensiveMatchupPlayer(101, 80, None if missing_height else 72),
        OffensiveMatchupPlayer(102, 50, None if missing_height else 76),
        OffensiveMatchupPlayer(103, 45, None if missing_height else 78),
        OffensiveMatchupPlayer(104, 30, None if missing_height else 80),
        OffensiveMatchupPlayer(105, 20, None if missing_height else 84),
    ]
    return list(reversed(rows)) if reverse else rows


def defense(*, reverse=False, weak=False, missing_height=False):
    rows = [
        DefensiveMatchupPlayer(
            201,
            90 if not weak else 40,
            20,
            0.10,
            None if missing_height else 72,
        ),
        DefensiveMatchupPlayer(202, 80, 35, 0.20, None if missing_height else 76),
        DefensiveMatchupPlayer(203, 60, 40, 0.30, None if missing_height else 78),
        DefensiveMatchupPlayer(204, 50, 45, 0.40, None if missing_height else 80),
        DefensiveMatchupPlayer(205, 40, 50, 0.50, None if missing_height else 84),
    ]
    return list(reversed(rows)) if reverse else rows


class MatchupAssignmentTests(unittest.TestCase):
    def test_returns_one_deterministic_assignment_and_never_scores_candidates(self):
        first = assign_matchups(offense(), defense())
        second = assign_matchups(offense(reverse=True), defense(reverse=True))

        self.assertEqual(first, second)
        self.assertEqual(len(first.pairs), 5)
        self.assertEqual(
            {(pair.offensive_player_id, pair.defensive_player_id) for pair in first.pairs},
            {(101, 201), (102, 202), (103, 203), (104, 204), (105, 205)},
        )
        self.assertEqual(sum(pair.is_primary_handler for pair in first.pairs), 1)
        self.assertEqual(sum(pair.is_best_perimeter_defender for pair in first.pairs), 1)
        self.assertEqual(len(first.as_json()), 5)

    def test_perimeter_threshold_and_deterministic_fallback(self):
        strong = assign_matchups(offense(), defense())
        self.assertGreaterEqual(strong.perimeter_score, PERIMETER_SCORE_THRESHOLD)
        self.assertTrue(strong.perimeter_threshold_met)
        self.assertEqual(strong.perimeter_defender_id, 201)

        weak_defense = [
            DefensiveMatchupPlayer(row.player_id, 40, 80, row.center_role_share, row.height)
            for row in defense(weak=True)
        ]
        fallback = assign_matchups(offense(), weak_defense)
        self.assertFalse(fallback.perimeter_threshold_met)
        self.assertEqual(fallback.perimeter_defender_id, 201)
        self.assertIn("PERIMETER_THRESHOLD_FALLBACK", fallback.limitations)

    def test_soft_three_inch_grace_minimizes_excess_size(self):
        defensive = [
            DefensiveMatchupPlayer(201, 90, 20, 0.1, 72),
            DefensiveMatchupPlayer(202, 80, 35, 0.2, 90),
            DefensiveMatchupPlayer(203, 60, 40, 0.3, 76),
            DefensiveMatchupPlayer(204, 50, 45, 0.4, 80),
            DefensiveMatchupPlayer(205, 40, 50, 0.5, 60),
        ]
        assignment = assign_matchups(offense(), defensive)
        by_offense = {pair.offensive_player_id: pair for pair in assignment.pairs}
        self.assertEqual(by_offense[102].defensive_player_id, 205)
        self.assertEqual(by_offense[103].defensive_player_id, 203)
        self.assertEqual(by_offense[104].defensive_player_id, 204)
        self.assertEqual(by_offense[105].defensive_player_id, 202)
        self.assertGreater(assignment.total_size_penalty or 0, 0)

    def test_missing_height_keeps_assignment_and_surfaces_unavailable_evidence(self):
        assignment = assign_matchups(
            offense(missing_height=True), defense(missing_height=True)
        )
        self.assertEqual(len(assignment.pairs), 5)
        self.assertIsNone(assignment.total_size_penalty)
        self.assertIn("MISSING_HEIGHT", assignment.limitations)
        self.assertTrue(
            all(pair.height_difference_inches is None for pair in assignment.pairs)
        )

    def test_missing_scores_use_neutral_evidence_not_zero(self):
        defensive = [
            DefensiveMatchupPlayer(201, None, None, None, 72),
            DefensiveMatchupPlayer(202, 40, 80, 0.2, 76),
            DefensiveMatchupPlayer(203, 40, 80, 0.3, 78),
            DefensiveMatchupPlayer(204, 40, 80, 0.4, 80),
            DefensiveMatchupPlayer(205, 40, 80, 0.5, 84),
        ]
        assignment = assign_matchups(offense(), defensive)
        self.assertEqual(assignment.perimeter_score, 50.0)
        self.assertIn("MISSING_DEFENSIVE_DISRUPTOR", assignment.limitations)
        self.assertIn("MISSING_DROP_COMPATIBLE_BIG", assignment.limitations)

    def test_invalid_lineups_and_values_are_rejected(self):
        with self.assertRaises(ValueError):
            assign_matchups(offense()[:4], defense())
        with self.assertRaises(ValueError):
            assign_matchups(offense(), defense() + [defense()[0]])
        with self.assertRaises(ValueError):
            assign_matchups(
                [OffensiveMatchupPlayer(101, 101, 72), *offense()[1:]],
                defense(),
            )
        with self.assertRaises(ValueError):
            assign_matchups(
                offense(),
                [
                    DefensiveMatchupPlayer(201, 90, 20, 1.1, 72),
                    *defense()[1:],
                ],
            )


if __name__ == "__main__":
    unittest.main()
