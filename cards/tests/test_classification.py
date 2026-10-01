"""Tests for role classification.

`classify` takes plain strings, so these run without a database -- hence
SimpleTestCase. Card text is quoted close to the real Oracle wording; the
point is to pin the patterns against how cards are actually worded, not
against paraphrases that happen to match.
"""

from django.test import SimpleTestCase

from cards.classification import (
    DRAW,
    LAND,
    PROTECTION,
    RAMP,
    RECURSION,
    SPOT_REMOVAL,
    SWEEPER,
    SYNERGY,
    TUTOR,
    WINCON,
    classify,
)

# (name, type_line, oracle_text, expected_primary_role)
CASES = [
    # -- ramp ---------------------------------------------------------------
    ("Sol Ring", "Artifact", "{T}: Add {C}{C}.", RAMP),
    (
        "Arcane Signet",
        "Artifact",
        "{T}: Add one mana of any color in your commander's color identity.",
        RAMP,
    ),
    ("Llanowar Elves", "Creature — Elf Druid", "{T}: Add {G}.", RAMP),
    (
        "Rampant Growth",
        "Sorcery",
        "Search your library for a basic land card, put it onto the battlefield tapped, then shuffle.",
        RAMP,
    ),
    # Land fetchers name basic land TYPES rather than the word "land".
    (
        "Nature's Lore",
        "Sorcery",
        "Search your library for a Forest card, put it onto the battlefield, then shuffle.",
        RAMP,
    ),
    (
        "Farseek",
        "Sorcery",
        "Search your library for a Plains, Island, Swamp, or Mountain card, put it onto the battlefield tapped, then shuffle.",
        RAMP,
    ),
    # Modern ramp often makes Treasure and never says "Add".
    (
        "Smothering Tithe",
        "Enchantment",
        "Whenever an opponent draws a card, that player may pay {2}. If the player doesn't, you create a Treasure token.",
        RAMP,
    ),
    (
        "Exploration",
        "Enchantment",
        "You may play an additional land on each of your turns.",
        RAMP,
    ),
    # -- draw ---------------------------------------------------------------
    ("Divination", "Sorcery", "Draw two cards.", DRAW),
    (
        "Phyrexian Arena",
        "Enchantment",
        "At the beginning of your upkeep, you draw a card and you lose 1 life.",
        DRAW,
    ),
    # An adjective can sit between the number and "cards".
    (
        "Sylvan Library",
        "Enchantment",
        "At the beginning of your draw step, draw two additional cards.",
        DRAW,
    ),
    (
        "Light Up the Stage",
        "Sorcery",
        "Exile the top two cards of your library. Until the end of your next turn, you may play those cards.",
        DRAW,
    ),
    # Symmetrical/punisher draw must not count as your card advantage.
    (
        "Howling Mine",
        "Artifact",
        "At the beginning of each player's draw step, that player draws an additional card.",
        SYNERGY,
    ),
    # -- removal ------------------------------------------------------------
    (
        "Swords to Plowshares",
        "Instant",
        "Exile target creature. Its controller gains life equal to its power.",
        SPOT_REMOVAL,
    ),
    ("Murder", "Instant", "Destroy target creature.", SPOT_REMOVAL),
    (
        "Lightning Bolt",
        "Instant",
        "Lightning Bolt deals 3 damage to any target.",
        SPOT_REMOVAL,
    ),
    (
        "Pongify",
        "Instant",
        "Destroy target creature. It can't be regenerated. That creature's controller creates a 3/3 green Ape creature token.",
        SPOT_REMOVAL,
    ),
    (
        "Chaos Warp",
        "Instant",
        "The owner of target permanent shuffles it into their library, then reveals the top card of their library.",
        SPOT_REMOVAL,
    ),
    # -- sweepers (must beat spot removal; "Destroy all" contains "Destroy") -
    (
        "Wrath of God",
        "Sorcery",
        "Destroy all creatures. They can't be regenerated.",
        SWEEPER,
    ),
    (
        "Blasphemous Act",
        "Sorcery",
        "Blasphemous Act deals 13 damage to each creature.",
        SWEEPER,
    ),
    (
        "Toxic Deluge",
        "Sorcery",
        "Pay X life. All creatures get -X/-X until end of turn.",
        SWEEPER,
    ),
    # Overload's mass mode exists only by rule, never as card text.
    (
        "Cyclonic Rift",
        "Instant",
        "Return target nonland permanent you don't control to its owner's hand. Overload {6}{U}",
        SWEEPER,
    ),
    # -- tutors (a land search is ramp, not a tutor) -------------------------
    (
        "Demonic Tutor",
        "Sorcery",
        "Search your library for a card, then put that card into your hand. Then shuffle.",
        TUTOR,
    ),
    (
        "Mystical Tutor",
        "Instant",
        "Search your library for an instant or sorcery card, reveal it, then shuffle and put that card on top.",
        TUTOR,
    ),
    # -- recursion ----------------------------------------------------------
    (
        "Eternal Witness",
        "Creature — Human Shaman",
        "When Eternal Witness enters, return target card from your graveyard to your hand.",
        RECURSION,
    ),
    # Reanimation targets "a graveyard", not only "your graveyard".
    (
        "Reanimate",
        "Sorcery",
        "Put target creature card from a graveyard onto the battlefield under your control. You lose life equal to its mana value.",
        RECURSION,
    ),
    # -- protection ---------------------------------------------------------
    ("Counterspell", "Instant", "Counter target spell.", PROTECTION),
    # Counterspells often name specific card types.
    (
        "Swan Song",
        "Instant",
        "Counter target enchantment, instant, or sorcery spell. Its controller creates a 2/2 blue Bird creature token with flying.",
        PROTECTION,
    ),
    (
        "Heroic Intervention",
        "Instant",
        "Permanents you control gain hexproof and indestructible until end of turn.",
        PROTECTION,
    ),
    (
        "Lightning Greaves",
        "Artifact — Equipment",
        "Equipped creature has haste and shroud.",
        PROTECTION,
    ),
    (
        "Teferi's Protection",
        "Instant",
        "Until your next turn, your life total can't change, you gain protection from everything, and all permanents you control phase out.",
        PROTECTION,
    ),
    # "choose new targets", not "change the target".
    (
        "Deflecting Swat",
        "Instant",
        "You may choose new targets for target spell or ability.",
        PROTECTION,
    ),
    # -- wincons ------------------------------------------------------------
    (
        "Thassa's Oracle",
        "Creature — Merfolk Wizard",
        "When Thassa's Oracle enters, look at the top X cards of your library. If X is greater than or equal to the number of cards in your library, you win the game.",
        WINCON,
    ),
    (
        "Felidar Sovereign",
        "Creature — Cat Beast",
        "At the beginning of your upkeep, if you have 40 or more life, you win the game.",
        WINCON,
    ),
    # -- lands --------------------------------------------------------------
    ("Forest", "Basic Land — Forest", "({T}: Add {G}.)", LAND),
    (
        "Command Tower",
        "Land",
        "{T}: Add one mana of any color in your commander's color identity.",
        LAND,
    ),
    ("Reliquary Tower", "Land", "You have no maximum hand size. {T}: Add {C}.", LAND),
    # -- catch-all ----------------------------------------------------------
    ("Serra Angel", "Creature — Angel", "Flying, vigilance", SYNERGY),
    ("Grizzly Bears", "Creature — Bear", "", SYNERGY),
]


class ClassificationTests(SimpleTestCase):
    def test_known_cards_classify_correctly(self):
        failures = []
        for name, type_line, oracle_text, expected in CASES:
            primary, _secondary = classify(type_line, oracle_text)
            if primary != expected:
                failures.append(f"{name}: expected {expected}, got {primary}")

        self.assertFalse(
            failures,
            f"{len(failures)}/{len(CASES)} misclassified:\n  " + "\n  ".join(failures),
        )

    def test_sweeper_beats_spot_removal(self):
        """'Destroy all creatures' contains 'Destroy', so order matters."""
        primary, secondary = classify("Sorcery", "Destroy all creatures.")
        self.assertEqual(primary, SWEEPER)
        self.assertNotEqual(primary, SPOT_REMOVAL)

    def test_land_search_is_ramp_not_tutor(self):
        primary, _ = classify(
            "Sorcery",
            "Search your library for a basic land card, put it onto the "
            "battlefield tapped, then shuffle.",
        )
        self.assertEqual(primary, RAMP)

    def test_secondary_role_is_recorded(self):
        """Cultivate ramps and replaces itself; both should be captured."""
        primary, secondary = classify(
            "Sorcery",
            "Search your library for up to two basic land cards, reveal them, "
            "put one onto the battlefield tapped and the other into your hand, "
            "then shuffle.",
        )
        self.assertEqual(primary, RAMP)
        self.assertEqual(secondary, "")

    def test_reminder_text_is_ignored(self):
        """Parenthesized reminder text restates rules and causes false hits."""
        primary, _ = classify(
            "Creature — Human Soldier",
            "Flying (This creature can't be blocked except by creatures with "
            "flying or reach.)",
        )
        self.assertEqual(primary, SYNERGY)

    def test_empty_input_is_safe(self):
        self.assertEqual(classify("", ""), (SYNERGY, ""))
        self.assertEqual(classify(None, None), (SYNERGY, ""))
