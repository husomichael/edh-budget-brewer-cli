"""Tests for the marginal upgrade path."""

import uuid

from django.test import TestCase

from cards.classification import LAND, SYNERGY
from cards.models import Card, CollectionItem, Deck, DeckCard
from cards.pool import build_pool
from cards.upgrades import upgrade_path


def make_card(name, **kwargs):
    defaults = {
        "oracle_id": uuid.uuid4(),
        "scryfall_id": uuid.uuid4(),
        "name": name,
        "cmc": 2,
        "mana_cost": "{1}{R}",
        "type_line": "Creature",
        "color_identity": ["R"],
        "price_cents": 100,
        "legal_commander": True,
        "edhrec_rank": 500,
        "primary_role": SYNERGY,
    }
    defaults.update(kwargs)
    return Card.objects.create(**defaults)


class UpgradePathTests(TestCase):
    def setUp(self):
        self.commander = make_card(
            "Path Cmdr",
            type_line="Legendary Creature — Goblin",
            oracle_text="Create a 1/1 red Goblin creature token.",
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
        # Cheap filler to make a deck feasible, plus better cards that only
        # become affordable at higher budgets.
        for i in range(90):
            make_card(f"cheap-{i}", price_cents=5, edhrec_rank=20_000 + i)
        for i in range(40):
            make_card(f"premium-{i}", price_cents=800 + i * 50, edhrec_rank=100 + i)

    def _path(self, base=2_000, **kwargs):
        return upgrade_path(build_pool(self.commander), base, **kwargs)

    def test_produces_a_step_per_budget_tier(self):
        path = self._path(steps_cents=[1_000, 2_000], include_no_limit=False)
        self.assertEqual(len(path.steps), 2)
        self.assertEqual([s.label for s in path.steps], ["+$10", "+$20"])

    def test_more_budget_never_scores_worse(self):
        path = self._path(steps_cents=[1_000, 5_000, 20_000])
        scores = [path.base.total_score] + [s.score for s in path.steps]
        for earlier, later in zip(scores, scores[1:], strict=False):
            self.assertGreaterEqual(later, earlier - 1e-9)

    def test_steps_report_what_came_in_and_out(self):
        path = self._path(steps_cents=[20_000], include_no_limit=False)
        step = path.steps[0]
        self.assertTrue(step.added)
        # Deck size is fixed, so every addition displaces something.
        self.assertEqual(len(step.added), len(step.removed))

    def test_no_limit_tier_is_flagged(self):
        path = self._path(steps_cents=[1_000])
        self.assertTrue(path.steps[-1].is_no_limit)
        self.assertEqual(path.steps[-1].label, "no limit")

    def test_best_next_buys_are_ordered_cheapest_first(self):
        """So the user can stop partway and still have spent well."""
        path = self._path(steps_cents=[20_000], include_no_limit=False)
        prices = [c.price_cents for c in path.best_next_buys]
        self.assertEqual(prices, sorted(prices))

    def test_cost_per_point_is_none_without_improvement(self):
        path = self._path(steps_cents=[1], include_no_limit=False)
        step = path.steps[0]
        if step.score_delta <= 0:
            self.assertIsNone(step.cost_per_point)

    def test_is_deterministic(self):
        first = self._path(steps_cents=[5_000], include_no_limit=False)
        second = self._path(steps_cents=[5_000], include_no_limit=False)
        self.assertEqual(
            [c.name for c in first.steps[0].added],
            [c.name for c in second.steps[0].added],
        )

    def test_infeasible_tiers_are_skipped_not_fatal(self):
        path = self._path(base=2_000, steps_cents=[1_000], include_no_limit=True)
        self.assertTrue(path.steps)


class OwnedFreePricingTests(TestCase):
    def setUp(self):
        self.commander = make_card(
            "Owned Cmdr",
            type_line="Legendary Creature — Goblin",
            can_be_commander=True,
        )
        self.pricey = make_card("Pricey Owned", price_cents=5_000, edhrec_rank=10)

    def test_owned_cards_cost_nothing_but_keep_their_retail_price(self):
        CollectionItem.objects.create(card=self.pricey)
        pool = build_pool(self.commander, owned_free=True)
        card = next(c for c in pool.candidates if c.name == "Pricey Owned")
        self.assertEqual(card.price_cents, 0)
        self.assertEqual(card.retail_cents, 5_000)
        self.assertTrue(card.is_owned)

    def test_without_the_flag_owned_cards_cost_full_price(self):
        CollectionItem.objects.create(card=self.pricey)
        pool = build_pool(self.commander, owned_free=False)
        card = next(c for c in pool.candidates if c.name == "Pricey Owned")
        self.assertEqual(card.price_cents, 5_000)
        self.assertFalse(card.is_owned)

    def test_cards_in_assembled_decks_are_flagged_not_hidden(self):
        """Surfacing the conflict beats silently deciding for the user."""
        CollectionItem.objects.create(card=self.pricey)
        built = Deck.objects.create(
            name="Already Built", commander=self.commander, is_assembled=True
        )
        DeckCard.objects.create(deck=built, card=self.pricey)

        pool = build_pool(self.commander, owned_free=True)
        card = next(c for c in pool.candidates if c.name == "Pricey Owned")
        self.assertEqual(card.locked_in, "Already Built")
        self.assertEqual(card.price_cents, 0)

    def test_unassembled_decks_do_not_lock_cards(self):
        CollectionItem.objects.create(card=self.pricey)
        shelved = Deck.objects.create(
            name="Shelved", commander=self.commander, is_assembled=False
        )
        DeckCard.objects.create(deck=shelved, card=self.pricey)

        pool = build_pool(self.commander, owned_free=True)
        card = next(c for c in pool.candidates if c.name == "Pricey Owned")
        self.assertEqual(card.locked_in, "")
