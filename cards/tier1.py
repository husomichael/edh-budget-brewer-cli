"""Tier 1: infer what a deck wants from the commander's own oracle text.

Tier 0 ranks by global EDH popularity, which gives a legal, playable, but
identity-free deck -- Krenko, Atraxa, and Muldrotha all return the same top
five cards. A commander's rules text usually states what the deck is about, and
that text is already in Postgres from the Scryfall sync, so one pass over it
turns a generic good-cards pile into a themed deck. For every commander in
existence, with no external data and nobody's permission.

The matching is **symmetric**: the same pattern set detects a theme on the
commander and scores candidates against it. If Muldrotha's text says
"graveyard" and a candidate's text says "graveyard", they are related.

Honest limits, worth surfacing in any UI:

  * Works well for tribal and mechanic-themed commanders -- roughly
    two-thirds of the format.
  * Works poorly for abstract value commanders (Kenrith, Najeela) whose text
    does not imply a card pool.
  * Does not work for combo commanders, whose decks are crowd knowledge rather
    than card text. That is what Tier 2 is for.
"""

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from cards.scoring import ScoredPool, normalize_rank

DATA_DIR = Path(__file__).resolve().parent / "data"

# Blend weights. Popularity keeps the deck made of cards people actually play;
# theme keeps it about the commander. Theme alone surfaces obscure cards that
# merely contain a matching word, which is how you end up with a Goblin deck
# full of unplayable Goblins.
POPULARITY_WEIGHT = 0.60
THEME_WEIGHT = 0.40

# A commander naming many types (Morophon, changelings) should not boost
# everything equally.
MAX_THEMES = 5

# Type-line hits are much stronger evidence than a passing mention in rules
# text: a Goblin Warrior IS a goblin, whereas a card that merely says "Goblin"
# might just make one token.
TYPE_LINE_STRENGTH = 1.0
ORACLE_MENTION_STRENGTH = 0.55

# A subtype on the commander's type line is, on its own, mostly flavor.
# Muldrotha is an "Elemental Avatar" but is a graveyard deck; Atraxa is a
# "Phyrexian Angel Horror" but is a counters deck; almost every commander is a
# Human. What marks a type as a real tribal plan is the commander's RULES TEXT
# naming it too -- Krenko says "Goblin", Edgar Markov says "Vampire", Najeela
# says "Warrior". So a bare subtype is only a weak hint, and text mentions are
# what promote it to a theme.
SUBTYPE_HINT_WEIGHT = 0.30
ORACLE_TYPE_MENTION_WEIGHT = 0.60

# Themes weaker than this are dropped rather than diluting the real ones.
MIN_THEME_WEIGHT = 0.50

REMINDER_TEXT = re.compile(r"\([^)]*\)")

# Mechanic themes. Patterns are deliberately narrow -- "attacks" alone matches
# most creatures in the game and carries no information.
MECHANIC_PATTERNS = {
    "counters": [
        r"\+1/\+1 counter",
        r"\bproliferate\b",
        r"counter on (a|target|each)",
    ],
    "sacrifice": [
        r"\bsacrifice (a|an|another|two)\b",
        r"whenever .{0,40}\bdies\b",
        r"\bdies\b.{0,40}\btrigger",
    ],
    "graveyard": [
        r"\bgraveyard\b",
        r"\bmill\b",
        r"from your graveyard",
    ],
    "landfall": [
        r"\blandfall\b",
        r"whenever a land enters",
        r"\blands? you control\b",
        r"additional land",
    ],
    "tokens": [
        r"\btoken\b",
        r"\bpopulate\b",
        r"\bcreate .{0,30}token",
    ],
    # Deliberately excludes a bare "lifelink" keyword: a commander HAVING
    # lifelink does not mean the deck is about gaining life.
    "lifegain": [
        r"whenever you gain life",
        r"\byou gain .{0,12}life\b",
    ],
    "spellslinger": [
        r"\binstant or sorcery\b",
        r"whenever you cast .{0,30}spell",
        r"\bmagecraft\b",
        r"\bstorm\b",
    ],
    "artifacts": [
        r"\bartifacts?\b",
        r"\bmetalcraft\b",
        r"\bimprovise\b",
    ],
    "enchantments": [
        r"\benchantments?\b",
        r"\baura\b",
        r"\bconstellation\b",
    ],
    "equipment": [
        r"\bequipment\b",
        r"\bequipped creature\b",
        r"\bequip\b",
    ],
    "attackers": [
        r"whenever .{0,30}attacks",
        r"\bextra combat\b",
        r"\bgoad\b",
        r"attacks? each combat",
    ],
    "damage": [
        r"deals? damage to (any|target|each)",
        r"whenever .{0,30}deals damage",
        r"\bdouble .{0,20}damage\b",
    ],
    "card_draw": [
        r"whenever you draw",
        r"draw .{0,12}additional card",
        r"\bdraws? (two|three|X) cards?\b",
    ],
    "treasure": [
        r"\btreasure\b",
    ],
    "counterspells": [
        r"counter target spell",
        r"whenever .{0,20}counters? a spell",
    ],
    "blink": [
        r"\bexile .{0,40}return (it|them) to the battlefield\b",
        r"enters the battlefield, .{0,30}exile",
        r"whenever .{0,30}enters the battlefield",
    ],
}

COMPILED_MECHANICS = {
    key: [re.compile(p, re.IGNORECASE) for p in patterns]
    for key, patterns in MECHANIC_PATTERNS.items()
}


@dataclass(frozen=True)
class Theme:
    kind: str  # "creature_type" or "mechanic"
    key: str
    weight: float

    def __str__(self):
        return f"{self.key} ({self.kind}, {self.weight:.2f})"


@lru_cache(maxsize=1)
def creature_types():
    """Scryfall's creature-type catalog, cached as a fixture.

    Compiled to word-boundary patterns because several types are short enough
    to appear inside unrelated words -- 'Bat' in 'Battle', 'Elf' in 'Elfari',
    'Ox' in 'Oxidda', and the catalog really does contain 'Q' and 'Qu'.
    """
    path = DATA_DIR / "creature_types.json"
    if not path.exists():
        return {}
    names = json.loads(path.read_text())
    return {
        name: re.compile(rf"\b{re.escape(name)}s?\b", re.IGNORECASE)
        for name in names
        # Single- and double-letter types produce far more noise than signal.
        if len(name) >= 3
    }


def strip_reminders(text):
    return REMINDER_TEXT.sub("", text or "")


def extract_themes(commander):
    """Work out what a commander's deck wants from its own card text."""
    type_line = commander.type_line or ""
    text = strip_reminders(commander.oracle_text or "")
    found = {}

    # The commander's own subtypes. Krenko being a Goblin is the strongest
    # possible signal that the deck is a Goblin deck.
    subtypes = type_line.split("—")[-1] if "—" in type_line else ""
    catalog = creature_types()
    for name, pattern in catalog.items():
        if pattern.search(subtypes):
            found[("creature_type", name)] = SUBTYPE_HINT_WEIGHT

    # Types named in rules text. This is what separates a real tribal commander
    # from one that merely happens to be an Elemental Avatar.
    for name, pattern in catalog.items():
        hits = len(pattern.findall(text))
        if hits:
            key = ("creature_type", name)
            found[key] = found.get(key, 0.0) + min(
                ORACLE_TYPE_MENTION_WEIGHT * hits, 1.0
            )

    # Mechanic themes.
    for key, patterns in COMPILED_MECHANICS.items():
        hits = sum(1 for p in patterns if p.search(text))
        if hits:
            found[("mechanic", key)] = min(0.6 + 0.2 * hits, 1.0)

    themes = [
        Theme(kind=kind, key=key, weight=weight)
        for (kind, key), weight in found.items()
        if weight >= MIN_THEME_WEIGHT
    ]
    # Deterministic: strongest first, then alphabetical so ties never depend
    # on dict ordering.
    themes.sort(key=lambda t: (-t.weight, t.kind, t.key))
    return themes[:MAX_THEMES]


def theme_score(card_row, themes, catalog=None):
    """How well one card fits a commander's themes, in 0..1."""
    if not themes:
        return 0.0

    catalog = catalog if catalog is not None else creature_types()
    type_line = card_row["type_line"] or ""
    text = strip_reminders(card_row["oracle_text"] or "")

    earned = 0.0
    possible = 0.0

    for theme in themes:
        possible += theme.weight
        if theme.kind == "creature_type":
            pattern = catalog.get(theme.key)
            if pattern is None:
                continue
            if pattern.search(type_line):
                earned += theme.weight * TYPE_LINE_STRENGTH
            elif pattern.search(text):
                earned += theme.weight * ORACLE_MENTION_STRENGTH
        else:
            patterns = COMPILED_MECHANICS.get(theme.key, [])
            if any(p.search(text) for p in patterns):
                earned += theme.weight

    return earned / possible if possible else 0.0


class Tier1SynergyStrategy:
    """Blend global popularity with commander-text synergy.

    Popularity alone is Tier 0 and has no identity. Theme alone surfaces
    unplayable cards that happen to contain a matching word. The blend yields
    popular cards that fit the theme, which is what a deck wants.
    """

    tier = 1
    label = "commander text (thematic)"

    def __init__(self, popularity_weight=POPULARITY_WEIGHT, theme_weight=THEME_WEIGHT):
        self.popularity_weight = popularity_weight
        self.theme_weight = theme_weight
        self.themes = []

    def score(self, cards, commander=None):
        if commander is None:
            self.themes = []
        else:
            self.themes = extract_themes(commander)

        # Fall back to pure popularity when a commander's text implies nothing
        # -- an abstract value commander should not get a deck built out of
        # near-zero theme scores.
        if not self.themes:
            return ScoredPool(
                scores={
                    str(c["oracle_id"]): normalize_rank(c["edhrec_rank"]) for c in cards
                },
                tier=self.tier,
                label=f"{self.label} - no themes detected, using popularity",
            )

        catalog = creature_types()
        scores = {}
        for row in cards:
            popularity = normalize_rank(row["edhrec_rank"])
            theme = theme_score(row, self.themes, catalog)
            scores[str(row["oracle_id"])] = (
                self.popularity_weight * popularity + self.theme_weight * theme
            )

        summary = ", ".join(t.key for t in self.themes)
        return ScoredPool(
            scores=scores,
            tier=self.tier,
            label=f"{self.label}: {summary}",
        )
