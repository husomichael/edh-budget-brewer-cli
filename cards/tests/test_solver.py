"""Tests for the budget deck solver."""

import uuid

from django.test import TestCase

from cards.classification import (
    DRAW,
    LAND,
    PROTECTION,
    RAMP,
    RECURSION,
    SPOT_REMOVAL,
    SWEEPER,
    SYNERGY,
    WINCON,
)
from cards.models import Card
from cards.pool import build_pool
from cards.solver import DECK_SIZE, InfeasibleBudget, brew

QUOTAS = {
    RAMP: (2, 4),
    DRAW: (2, 4),
    SPOT_REMOVAL: (2, 4),
    SWEEPER: (1, 2),
    PROTECTION: (1, 2),
    RECURSION: (0, 2),
    WINCON: (0, 2),
    SYNERGY: (0, 99),
}


def make_card(name, **kwargs):
    defaults = {
        "oracle_id": uuid.uuid4(),
        "scryfall_id": uuid.uuid4(),
        "name": name,
        "cmc": 2,
        "mana_cost": "{1}{R}",
        "type_line": "Creature",
        "color_identity": ["R"],
        "colors": ["R"],
        "price_cents": 10,
        "legal_commander": True,
        "edhrec_rank": 500,
        "primary_role": SYNERGY,
    }
    defaults.update(kwargs)
    return Card.objects.create(**defaults)


class SolverTests(TestCase):
    def setUp(self):
        self.commander = make_card(
            "Test Commander",
            type_line="Legendary Creature — Goblin",
            can_be_commander=True,
        )
        make_card(
            "Mountain",
            type_line="Basic Land — Mountain",
            is_land=True,
            is_basic=True,
            primary_role=LAND,
            price_cents=None,
        )
        # A deep enough pool to fill 63 nonland slots with room to upgrade.
        for role in (RAMP, DRAW, SPOT_REMOVAL, SWEEPER, PROTECTION, RECURSION, WINCON):
            for i in range(12):
                make_card(
                    f"{role}-{i}",
                    primary_role=role,
                    price_cents=5 + i * 25,
                    edhrec_rank=1000 - i * 50,
                )
        for i in range(120):
            make_card(
                f"synergy-{i}",
                primary_role=SYNERGY,
                price_cents=5 + i,
                edhrec_rank=2000 - i * 10,
            )

    def _brew(self, budget_cents):
        return brew(build_pool(self.commander), budget_cents, quotas=QUOTAS)

    def test_produces_exactly_one_hundred_cards(self):
        self.assertEqual(self._brew(10_000).card_count, DECK_SIZE)

    def test_stays_within_budget(self):
        for budget in (2_000, 5_000, 20_000):
            result = self._brew(budget)
            self.assertLessEqual(result.total_cents, budget, f"over budget at {budget}")

    def test_is_deterministic(self):
        """Identical inputs must produce byte-identical decks."""
        first, second = self._brew(8_000), self._brew(8_000)
        self.assertEqual(
            [c.name for c in first.spells], [c.name for c in second.spells]
        )
        self.assertEqual(
            [(c.name, q) for c, q in first.lands],
            [(c.name, q) for c, q in second.lands],
        )
        self.assertEqual(first.total_cents, second.total_cents)

    def test_respects_role_ceilings(self):
        result = self._brew(20_000)
        for role, (_floor, ceiling) in QUOTAS.items():
            self.assertLessEqual(
                result.role_counts.get(role, 0),
                ceiling,
                f"{role} exceeded its ceiling",
            )

    def test_meets_role_floors(self):
        result = self._brew(20_000)
        for role, (floor, _ceiling) in QUOTAS.items():
            self.assertGreaterEqual(
                result.role_coverage.get(role, 0), floor, f"{role} below floor"
            )

    def test_more_budget_buys_a_better_deck(self):
        cheap = self._brew(2_000)
        rich = self._brew(30_000)
        self.assertGreater(rich.total_score, cheap.total_score)

    def test_no_single_card_dominates_the_budget(self):
        """Greedy-by-best-affordable buys one premium card and 60 pieces of
        chaff. Ranking swaps by score-per-dollar must spread the budget."""
        make_card(
            "Expensive Bomb",
            primary_role=SYNERGY,
            price_cents=9_000,
            edhrec_rank=1,
        )
        result = self._brew(10_000)
        priciest = max(c.price_cents for c in result.spells)
        self.assertLess(priciest, result.spell_cents * 0.5)

    def test_infeasible_budget_raises_with_a_minimum(self):
        with self.assertRaises(InfeasibleBudget) as ctx:
            self._brew(1)
        self.assertGreater(ctx.exception.minimum_cents, 0)

    def test_singleton_is_enforced(self):
        result = self._brew(20_000)
        names = [c.name for c in result.spells]
        self.assertEqual(len(names), len(set(names)))

    def test_only_basics_appear_in_multiples(self):
        result = self._brew(20_000)
        for cand, qty in result.lands:
            if qty > 1:
                self.assertTrue(cand.is_basic, f"{cand.name} has {qty} copies")

    def test_commander_is_not_in_the_deck(self):
        result = self._brew(20_000)
        self.assertNotIn("Test Commander", [c.name for c in result.spells])

    def test_reports_scoring_tier(self):
        result = self._brew(10_000)
        self.assertEqual(result.tier, 0)
        self.assertTrue(result.tier_label)

    def test_curve_covers_every_nonland_card(self):
        result = self._brew(10_000)
        self.assertEqual(sum(result.curve.values()), len(result.spells))

    def test_role_counts_sum_to_nonland_count(self):
        result = self._brew(10_000)
        self.assertEqual(sum(result.role_counts.values()), len(result.spells))
