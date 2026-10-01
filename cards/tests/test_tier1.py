"""Tests for Tier 1 commander-text synergy inference."""

import uuid

from django.test import SimpleTestCase, TestCase

from cards.classification import SYNERGY
from cards.models import Card
from cards.tier1 import (
    MIN_THEME_WEIGHT,
    Tier1SynergyStrategy,
    creature_types,
    extract_themes,
    theme_score,
)


class Fake:
    """Minimal stand-in for a commander; extract_themes only reads two fields."""

    def __init__(self, type_line, oracle_text=""):
        self.type_line = type_line
        self.oracle_text = oracle_text


def row(type_line="", oracle_text="", rank=500, oid=None):
    return {
        "oracle_id": oid or str(uuid.uuid4()),
        "type_line": type_line,
        "oracle_text": oracle_text,
        "edhrec_rank": rank,
    }


class CreatureTypeCatalogTests(SimpleTestCase):
    def test_catalog_loads(self):
        self.assertGreater(len(creature_types()), 200)

    def test_very_short_types_are_excluded(self):
        """The catalog really contains 'Q' and 'Qu'; matching those is noise."""
        for name in creature_types():
            self.assertGreaterEqual(len(name), 3)

    def test_matching_respects_word_boundaries(self):
        """'Bat' must not match 'Battle', 'Elf' must not match 'Elfari'."""
        catalog = creature_types()
        self.assertIsNotNone(catalog["Bat"])
        self.assertFalse(catalog["Bat"].search("Battle Cry"))
        self.assertFalse(catalog["Elf"].search("Elfari of Kaldheim"))
        self.assertTrue(catalog["Bat"].search("Legendary Creature — Bat"))

    def test_plurals_match(self):
        self.assertTrue(creature_types()["Goblin"].search("Goblins you control"))


class ExtractThemesTests(SimpleTestCase):
    def test_tribal_commander_detects_its_type(self):
        krenko = Fake(
            "Legendary Creature — Goblin Warrior",
            "{T}: Create X 1/1 red Goblin creature tokens, where X is the "
            "number of Goblins you control.",
        )
        keys = [t.key for t in extract_themes(krenko)]
        self.assertIn("Goblin", keys)
        self.assertIn("tokens", keys)

    def test_bare_subtype_alone_is_not_a_tribal_theme(self):
        """Muldrotha is an 'Elemental Avatar' but is a graveyard deck.

        A subtype that appears only on the type line, never in rules text, is
        flavor. Treating it as a theme builds Elemental tribal by accident.
        """
        muldrotha = Fake(
            "Legendary Creature — Elemental Avatar",
            "During each of your turns, you may play a land and up to one "
            "permanent of each other type from your graveyard.",
        )
        themes = extract_themes(muldrotha)
        keys = [t.key for t in themes]
        self.assertIn("graveyard", keys)
        self.assertNotIn("Elemental", keys)
        self.assertNotIn("Avatar", keys)

    def test_text_mention_promotes_a_subtype_to_a_theme(self):
        with_text = Fake(
            "Legendary Creature — Elemental",
            "Whenever a land enters, create a 5/5 Elemental creature token.",
        )
        self.assertIn("Elemental", [t.key for t in extract_themes(with_text)])

    def test_having_a_keyword_is_not_a_deck_theme(self):
        """Atraxa has flying; Atraxa is not a flying deck."""
        atraxa = Fake(
            "Legendary Creature — Phyrexian Angel Horror",
            "Flying, vigilance, deathtouch, lifelink\n"
            "At the beginning of your end step, proliferate.",
        )
        keys = [t.key for t in extract_themes(atraxa)]
        self.assertIn("counters", keys)
        self.assertNotIn("flying", keys)
        self.assertNotIn("lifegain", keys)
        self.assertNotIn("Angel", keys)

    def test_weak_themes_are_dropped(self):
        for theme in extract_themes(Fake("Legendary Creature — Human Noble", "")):
            self.assertGreaterEqual(theme.weight, MIN_THEME_WEIGHT)

    def test_theme_count_is_capped(self):
        """Morophon-style commanders must not boost everything equally."""
        busy = Fake(
            "Legendary Creature — Shapeshifter",
            "Goblin Elf Dragon Vampire Zombie Angel Sliver Beast Knight "
            "Wizard Soldier graveyard token sacrifice treasure artifact",
        )
        self.assertLessEqual(len(extract_themes(busy)), 5)

    def test_vanilla_commander_yields_no_themes(self):
        self.assertEqual(extract_themes(Fake("Legendary Creature — Bear", "")), [])

    def test_is_deterministic(self):
        commander = Fake(
            "Legendary Creature — Goblin",
            "Goblins you control get +1/+0. Create a Goblin token. "
            "Sacrifice a Goblin: draw two cards.",
        )
        first = [(t.kind, t.key, t.weight) for t in extract_themes(commander)]
        second = [(t.kind, t.key, t.weight) for t in extract_themes(commander)]
        self.assertEqual(first, second)


class ThemeScoreTests(SimpleTestCase):
    def setUp(self):
        self.themes = extract_themes(
            Fake(
                "Legendary Creature — Goblin Warrior",
                "Create X 1/1 red Goblin creature tokens.",
            )
        )

    def test_on_theme_card_outscores_off_theme(self):
        goblin = row("Creature — Goblin Shaman", "Haste")
        bear = row("Creature — Bear", "")
        self.assertGreater(
            theme_score(goblin, self.themes), theme_score(bear, self.themes)
        )

    def test_type_line_hit_beats_a_passing_mention(self):
        is_goblin = row("Creature — Goblin", "")
        mentions_goblin = row("Sorcery", "Create a 1/1 red Goblin creature token.")
        self.assertGreater(
            theme_score(is_goblin, self.themes),
            theme_score(mentions_goblin, self.themes) - 1.0,
        )

    def test_scores_stay_in_unit_range(self):
        for card in [
            row("Creature — Goblin Warrior", "Create a Goblin token."),
            row("Instant", ""),
            row("", ""),
        ]:
            score = theme_score(card, self.themes)
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 1.0)

    def test_no_themes_scores_zero(self):
        self.assertEqual(theme_score(row("Creature — Goblin", ""), []), 0.0)


class Tier1StrategyTests(TestCase):
    def _commander(self, **kwargs):
        defaults = {
            "oracle_id": uuid.uuid4(),
            "scryfall_id": uuid.uuid4(),
            "name": "Test Commander",
            "cmc": 4,
            "mana_cost": "{2}{R}{R}",
            "type_line": "Legendary Creature — Goblin Warrior",
            "oracle_text": "Create X 1/1 red Goblin creature tokens.",
            "color_identity": ["R"],
            "legal_commander": True,
            "can_be_commander": True,
            "edhrec_rank": 1000,
            "primary_role": SYNERGY,
        }
        defaults.update(kwargs)
        return Card.objects.create(**defaults)

    def test_reports_tier_and_detected_themes(self):
        commander = self._commander()
        cards = [row("Creature — Goblin", "", rank=5000)]
        pool = Tier1SynergyStrategy().score(cards, commander)
        self.assertEqual(pool.tier, 1)
        self.assertIn("Goblin", pool.label)

    def test_on_theme_card_beats_more_popular_off_theme_card(self):
        commander = self._commander()
        goblin = row("Creature — Goblin Shaman", "Haste", rank=8000, oid="goblin")
        generic = row("Creature — Bear", "", rank=2000, oid="bear")
        scores = Tier1SynergyStrategy().score([goblin, generic], commander).scores
        self.assertGreater(scores["goblin"], scores["bear"])

    def test_popularity_still_matters_within_a_theme(self):
        """Blended, not theme-only: an unplayable on-theme card should lose
        to a popular one."""
        commander = self._commander()
        popular = row("Creature — Goblin", "", rank=50, oid="pop")
        obscure = row("Creature — Goblin", "", rank=30000, oid="obscure")
        scores = Tier1SynergyStrategy().score([popular, obscure], commander).scores
        self.assertGreater(scores["pop"], scores["obscure"])

    def test_falls_back_to_popularity_when_no_themes(self):
        """An abstract commander should get a popularity deck, not a deck
        built from uniformly near-zero theme scores."""
        vanilla = self._commander(type_line="Legendary Creature — Bear", oracle_text="")
        cards = [row("Creature — Goblin", "", rank=10, oid="a")]
        pool = Tier1SynergyStrategy().score(cards, vanilla)
        self.assertIn("no themes", pool.label)
        self.assertAlmostEqual(pool.scores["a"], 1.0 - (2.302585 / 10.373491), places=3)

    def test_handles_missing_commander(self):
        pool = Tier1SynergyStrategy().score([row("Creature", "", oid="a")], None)
        self.assertIn("no themes", pool.label)
