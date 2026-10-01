"""Tests for candidate pool construction."""

import uuid

from django.test import TestCase

from cards.classification import LAND, RAMP, SPOT_REMOVAL, SYNERGY
from cards.models import Card
from cards.pool import build_pool


def make_card(name, **kwargs):
    defaults = {
        "oracle_id": uuid.uuid4(),
        "scryfall_id": uuid.uuid4(),
        "name": name,
        "cmc": 2,
        "mana_cost": "{1}{R}",
        "type_line": "Creature",
        "color_identity": [],
        "colors": [],
        "price_cents": 100,
        "legal_commander": True,
        "is_banned": False,
        "edhrec_rank": 500,
        "primary_role": SYNERGY,
    }
    defaults.update(kwargs)
    return Card.objects.create(**defaults)


class BuildPoolTests(TestCase):
    def setUp(self):
        self.commander = make_card(
            "Test Commander",
            type_line="Legendary Creature — Goblin",
            color_identity=["R"],
            can_be_commander=True,
        )

    def test_includes_on_color_and_colorless(self):
        make_card("Mono Red Spell", color_identity=["R"])
        make_card("Colorless Rock", color_identity=[], primary_role=RAMP)
        names = {c.name for c in build_pool(self.commander).candidates}
        self.assertIn("Mono Red Spell", names)
        self.assertIn("Colorless Rock", names)

    def test_excludes_off_color(self):
        make_card("Blue Spell", color_identity=["U"])
        make_card("Rakdos Spell", color_identity=["B", "R"])
        names = {c.name for c in build_pool(self.commander).candidates}
        self.assertNotIn("Blue Spell", names)
        self.assertNotIn("Rakdos Spell", names)

    def test_excludes_banned_and_illegal(self):
        make_card("Banned Card", color_identity=["R"], is_banned=True)
        make_card("Not Legal", color_identity=["R"], legal_commander=False)
        names = {c.name for c in build_pool(self.commander).candidates}
        self.assertNotIn("Banned Card", names)
        self.assertNotIn("Not Legal", names)

    def test_excludes_unpriceable_cards(self):
        """A card with no USD price is unbuyable and must not look free."""
        make_card("No Price", color_identity=["R"], price_cents=None)
        names = {c.name for c in build_pool(self.commander).candidates}
        self.assertNotIn("No Price", names)

    def test_excludes_the_commander_itself(self):
        names = {c.name for c in build_pool(self.commander).candidates}
        self.assertNotIn("Test Commander", names)

    def test_excludes_partner_when_given(self):
        partner = make_card(
            "Test Partner",
            type_line="Legendary Creature — Goblin",
            color_identity=["R"],
            can_be_commander=True,
        )
        pool = build_pool(self.commander, partner=partner)
        self.assertNotIn("Test Partner", {c.name for c in pool.candidates})

    def test_basic_lands_are_priced_free(self):
        make_card(
            "Mountain",
            type_line="Basic Land — Mountain",
            is_land=True,
            is_basic=True,
            primary_role=LAND,
            price_cents=42,
        )
        card = next(
            c for c in build_pool(self.commander).candidates if c.name == "Mountain"
        )
        self.assertEqual(card.price_cents, 0)
        self.assertEqual(card.score_per_dollar, float("inf"))

    def test_by_role_and_filling_role(self):
        make_card("Pure Ramp", color_identity=["R"], primary_role=RAMP)
        make_card(
            "Removal That Ramps",
            color_identity=["R"],
            primary_role=SPOT_REMOVAL,
            secondary_role=RAMP,
        )
        pool = build_pool(self.commander)
        self.assertEqual({c.name for c in pool.by_role(RAMP)}, {"Pure Ramp"})
        self.assertEqual(
            {c.name for c in pool.filling_role(RAMP)},
            {"Pure Ramp", "Removal That Ramps"},
        )

    def test_score_per_dollar_rewards_cheap_value(self):
        # Created for their effect on the pool; the returned rows are unused.
        make_card("Cheap Good", color_identity=["R"], price_cents=50, edhrec_rank=10)
        make_card("Pricey Good", color_identity=["R"], price_cents=5000, edhrec_rank=10)
        pool = build_pool(self.commander)
        by_name = {c.name: c for c in pool.candidates}
        self.assertGreater(
            by_name["Cheap Good"].score_per_dollar,
            by_name["Pricey Good"].score_per_dollar,
        )
        # Same rank means same raw score; only density differs.
        self.assertEqual(by_name["Cheap Good"].score, by_name["Pricey Good"].score)

    def test_pool_reports_its_tier(self):
        pool = build_pool(self.commander)
        self.assertEqual(pool.tier, 0)
        self.assertTrue(pool.description.startswith("Tier 0"))
