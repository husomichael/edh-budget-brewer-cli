"""Tests for mana base construction."""

import uuid

from django.test import TestCase

from cards.classification import LAND, SYNERGY
from cards.manabase import (
    MAX_NONBASIC_FRACTION,
    build_mana_base,
    color_weights_from_cards,
)
from cards.models import Card
from cards.pool import Candidate, build_pool


def make_card(name, **kwargs):
    defaults = {
        "oracle_id": uuid.uuid4(),
        "scryfall_id": uuid.uuid4(),
        "name": name,
        "cmc": 2,
        "mana_cost": "",
        "type_line": "Creature",
        "color_identity": [],
        "colors": [],
        "price_cents": 100,
        "legal_commander": True,
        "edhrec_rank": 500,
        "primary_role": SYNERGY,
    }
    defaults.update(kwargs)
    return Card.objects.create(**defaults)


def make_basics(*colors):
    names = {"W": "Plains", "U": "Island", "B": "Swamp", "R": "Mountain", "G": "Forest"}
    for color in colors:
        make_card(
            names[color],
            type_line=f"Basic Land — {names[color]}",
            color_identity=[color],
            is_land=True,
            is_basic=True,
            primary_role=LAND,
            price_cents=None,  # Scryfall prices basics as null
        )


def candidate(mana_cost):
    return Candidate(
        oracle_id=str(uuid.uuid4()),
        name="x",
        price_cents=0,
        retail_cents=0,
        is_owned=False,
        locked_in="",
        cmc=2,
        mana_cost=mana_cost,
        type_line="",
        image_uri="",
        primary_role=SYNERGY,
        secondary_role="",
        is_land=False,
        is_basic=False,
        score=0.5,
    )


class ColorWeightTests(TestCase):
    def test_even_split_when_no_spells_chosen(self):
        """The first pass has no spells yet, so weights must still be usable."""
        weights = color_weights_from_cards([], ["U", "B", "G"])
        self.assertEqual(set(weights), {"U", "B", "G"})
        for value in weights.values():
            self.assertAlmostEqual(value, 1 / 3)

    def test_weights_follow_actual_pips(self):
        spells = [candidate("{R}{R}{R}"), candidate("{G}")]
        weights = color_weights_from_cards(spells, ["R", "G"])
        self.assertAlmostEqual(weights["R"], 0.75)
        self.assertAlmostEqual(weights["G"], 0.25)

    def test_hybrid_pips_count_for_both_colors(self):
        weights = color_weights_from_cards([candidate("{W/U}")], ["W", "U"])
        self.assertAlmostEqual(weights["W"], 0.5)
        self.assertAlmostEqual(weights["U"], 0.5)

    def test_generic_costs_contribute_nothing(self):
        weights = color_weights_from_cards([candidate("{3}")], ["R", "G"])
        self.assertAlmostEqual(weights["R"], 0.5)

    def test_colorless_commander_has_no_weights(self):
        self.assertEqual(color_weights_from_cards([], []), {})


class BuildManaBaseTests(TestCase):
    def setUp(self):
        self.commander = make_card(
            "Mono Red Commander",
            type_line="Legendary Creature — Goblin",
            color_identity=["R"],
            can_be_commander=True,
        )

    def test_hits_the_land_target_using_free_basics(self):
        make_basics("R")
        mana = build_mana_base(build_pool(self.commander), budget_cents=0)
        self.assertEqual(mana.land_count, 36)
        self.assertEqual(mana.total_cents, 0)

    def test_basics_are_exempt_from_singleton(self):
        """Any number of basics is legal; this is the one such exception."""
        make_basics("R")
        mana = build_mana_base(build_pool(self.commander), budget_cents=0)
        quantities = [qty for cand, qty in mana.lands if cand.is_basic]
        self.assertEqual(len(quantities), 1)
        self.assertGreater(quantities[0], 1)

    def test_respects_the_land_budget(self):
        make_basics("R")
        make_card(
            "Pricey Land",
            type_line="Land",
            is_land=True,
            primary_role=LAND,
            price_cents=5000,
            edhrec_rank=1,
        )
        mana = build_mana_base(build_pool(self.commander), budget_cents=500)
        self.assertLessEqual(mana.total_cents, 500)
        self.assertNotIn("Pricey Land", [c.name for c, _ in mana.lands])

    def test_single_land_cannot_dominate_the_budget(self):
        """One expensive land must not eat the whole mana base budget."""
        make_basics("R")
        make_card(
            "Budget Eater",
            type_line="Land",
            is_land=True,
            primary_role=LAND,
            price_cents=900,
            edhrec_rank=1,
        )
        mana = build_mana_base(build_pool(self.commander), budget_cents=1000)
        self.assertNotIn("Budget Eater", [c.name for c, _ in mana.lands])

    def test_snow_basics_are_never_bought(self):
        make_basics("R")
        make_card(
            "Snow-Covered Mountain",
            type_line="Basic Snow Land — Mountain",
            color_identity=["R"],
            is_land=True,
            is_basic=False,
            primary_role=LAND,
            price_cents=50,
            edhrec_rank=1,
        )
        mana = build_mana_base(build_pool(self.commander), budget_cents=5000)
        self.assertNotIn("Snow-Covered Mountain", [c.name for c, _ in mana.lands])

    def test_nonbasic_count_is_capped(self):
        make_basics("R")
        for i in range(40):
            make_card(
                f"Utility Land {i}",
                type_line="Land",
                is_land=True,
                primary_role=LAND,
                price_cents=1,
                edhrec_rank=100 + i,
            )
        mana = build_mana_base(build_pool(self.commander), budget_cents=100_000)
        self.assertLessEqual(mana.nonbasic_count, int(36 * MAX_NONBASIC_FRACTION))

    def test_basics_distribution_sums_exactly(self):
        """Largest-remainder, so the deck never lands on 98 or 100 lands."""
        three_color = make_card(
            "Three Color",
            type_line="Legendary Creature — Elf",
            color_identity=["B", "G", "U"],
            can_be_commander=True,
        )
        make_basics("B", "G", "U")
        mana = build_mana_base(build_pool(three_color), budget_cents=0, land_count=35)
        self.assertEqual(mana.land_count, 35)

    def test_warns_when_no_basics_available(self):
        mana = build_mana_base(build_pool(self.commander), budget_cents=0)
        self.assertTrue(mana.warnings)
        self.assertLess(mana.land_count, 36)
