"""Tests for score normalization."""

from django.test import SimpleTestCase

from cards.scoring import RANK_CEILING, Tier0PopularityStrategy, normalize_rank


class NormalizeRankTests(SimpleTestCase):
    def test_rank_one_scores_maximum(self):
        self.assertEqual(normalize_rank(1), 1.0)

    def test_missing_rank_scores_zero(self):
        self.assertEqual(normalize_rank(None), 0.0)
        self.assertEqual(normalize_rank(0), 0.0)
        self.assertEqual(normalize_rank(-5), 0.0)

    def test_rank_beyond_ceiling_scores_zero(self):
        self.assertEqual(normalize_rank(RANK_CEILING), 0.0)
        self.assertEqual(normalize_rank(RANK_CEILING + 5000), 0.0)

    def test_scores_decrease_monotonically_with_rank(self):
        ranks = [1, 10, 100, 1000, 10000, 31999]
        scores = [normalize_rank(r) for r in ranks]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_log_scale_separates_top_staples(self):
        """A linear scale cannot tell rank 1 from rank 100; a log scale can.

        EDH popularity is power-law distributed, so the gap between rank 1 and
        rank 100 must be larger than the gap between 10,000 and 10,100.
        """
        top_gap = normalize_rank(1) - normalize_rank(100)
        tail_gap = normalize_rank(10_000) - normalize_rank(10_100)
        self.assertGreater(top_gap, tail_gap * 10)

    def test_all_scores_within_unit_range(self):
        for rank in [1, 2, 50, 500, 5000, 20000, 31999]:
            score = normalize_rank(rank)
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 1.0)


class Tier0StrategyTests(SimpleTestCase):
    def test_reports_its_tier_and_label(self):
        pool = Tier0PopularityStrategy().score([])
        self.assertEqual(pool.tier, 0)
        self.assertIn("generic", pool.label)
        self.assertTrue(pool.description.startswith("Tier 0"))

    def test_scores_keyed_by_oracle_id_as_string(self):
        rows = [
            {"oracle_id": "abc", "edhrec_rank": 1},
            {"oracle_id": "def", "edhrec_rank": None},
        ]
        scores = Tier0PopularityStrategy().score(rows).scores
        self.assertEqual(scores["abc"], 1.0)
        self.assertEqual(scores["def"], 0.0)
