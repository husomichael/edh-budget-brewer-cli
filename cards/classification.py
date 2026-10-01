"""Classify cards into functional roles.

This is what turns a scored pile of cards into a deck. The quota system in the
solver fills a target number of slots per role, so a card's role matters as
much as its score.

Deliberately **not** an LLM call: a regex pass plus a curated override table is
faster, free, deterministic, and debuggable. Classification must give the same
answer on every run or generated decks become irreproducible.

The entry point is `classify(type_line, oracle_text)`, which takes plain
strings so it can be tested without touching the database.
"""

import re

# -- roles -------------------------------------------------------------------

LAND = "land"
RAMP = "ramp"
DRAW = "draw"
SPOT_REMOVAL = "spot_removal"
SWEEPER = "sweeper"
TUTOR = "tutor"
RECURSION = "recursion"
PROTECTION = "protection"
WINCON = "wincon"
SYNERGY = "synergy"

ROLE_CHOICES = [
    (LAND, "Land"),
    (RAMP, "Ramp"),
    (DRAW, "Card draw"),
    (SPOT_REMOVAL, "Spot removal"),
    (SWEEPER, "Sweeper"),
    (TUTOR, "Tutor"),
    (RECURSION, "Recursion"),
    (PROTECTION, "Protection"),
    (WINCON, "Win condition"),
    (SYNERGY, "Synergy"),
]

# -- patterns ----------------------------------------------------------------
#
# Order matters. Sweepers are checked before spot removal because
# "Destroy all creatures" contains "Destroy". Ramp is checked before tutors
# because a land search is ramp, not a tutor.

_P = re.IGNORECASE

# Mass removal. "each creature" and "all creatures" are the giveaways.
SWEEPER_PATTERNS = [
    re.compile(r"\b(destroy|exile)\s+all\b", _P),
    re.compile(r"\b(destroy|exile)\s+each\b", _P),
    re.compile(r"all\s+creatures?\s+get\s+-\d+/-\d+", _P),
    re.compile(r"all\s+creatures?\s+gets?\s+-[XY]/-[XY]", _P),
    re.compile(r"deals?\s+\d+\s+damage\s+to\s+each\s+(creature|other creature)", _P),
    re.compile(r"deals?\s+[XY]\s+damage\s+to\s+each\s+creature", _P),
    re.compile(r"each\s+player\s+sacrifices?", _P),
    re.compile(r"each\s+opponent\s+sacrifices?", _P),
    re.compile(r"return\s+all\s+(nonland\s+)?permanents", _P),
    re.compile(r"sacrifices?\s+all", _P),
    # Overload replaces "target" with "all" by rule, so the mass mode is never
    # spelled out on the card. Its presence alone means mass removal.
    re.compile(r"\boverload\b", _P),
]

# Targeted interaction.
SPOT_REMOVAL_PATTERNS = [
    re.compile(r"\b(destroy|exile)\s+target\b", _P),
    re.compile(r"\b(destroy|exile)\s+(up to )?(one|two|three|another)\s+target", _P),
    re.compile(r"target\s+creature\s+gets?\s+-\d+/-\d+", _P),
    re.compile(r"deals?\s+\d+\s+damage\s+to\s+(target|any target)", _P),
    re.compile(r"target\s+(player|opponent)\s+sacrifices?\s+a", _P),
    re.compile(r"return\s+target\s+(creature|permanent|nonland)", _P),
    re.compile(r"\bfight(s)?\s+target\b", _P),
    # Chaos Warp removes by shuffling into the library instead of destroying.
    re.compile(r"shuffles?\s+it\s+into\s+(their|his or her)\s+library", _P),
]

# Mana production and land acceleration.
RAMP_PATTERNS = [
    re.compile(r"\badd\s+\{", _P),
    re.compile(r"\badd\s+(one|two|three|four|[XY])\s+mana\b", _P),
    re.compile(r"\badd\s+that\s+much\b", _P),
    re.compile(r"search\s+your\s+library\s+for\s+.{0,60}\bland\b", _P),
    # Land fetchers usually name basic land types rather than saying "land":
    # "Search your library for a Forest card" / "for a Plains, Island, ...".
    re.compile(
        r"search\s+your\s+library\s+for\s+.{0,60}"
        r"\b(plains|island|swamp|mountain|forest|wastes|gate)\b",
        _P,
    ),
    re.compile(r"you\s+may\s+play\s+an\s+additional\s+land", _P),
    re.compile(r"put\s+.{0,30}\bland\b.{0,30}onto\s+the\s+battlefield", _P),
    # Treasure is mana, and plenty of modern ramp never says "Add".
    re.compile(r"\btreasure\s+token", _P),
    re.compile(r"\bmana\s+of\s+any\s+(one\s+)?color\b", _P),
]

# Library search that is not a land search.
TUTOR_PATTERNS = [
    re.compile(r"search\s+your\s+library\s+for", _P),
]

CARD_DRAW_PATTERNS = [
    re.compile(
        r"\bdraws?\s+(a|an|one|two|three|four|five|six|seven|[XY]|\d+)\s+"
        r"(\w+\s+)?cards?",
        _P,
    ),
    re.compile(r"\bdraw\s+that\s+many\s+cards?", _P),
    re.compile(r"\bdraws?\s+cards?\s+equal\b", _P),
    # Impulse draw: exile from the top of your library and play it. The window
    # has to be generous -- "Exile the top two cards of your library. Until the
    # end of your next turn, you may play those cards" spans ~60 characters.
    re.compile(r"exile\s+the\s+top\s+.{0,90}you\s+may\s+(play|cast)", _P),
    re.compile(r"look\s+at\s+the\s+top\s+.{0,40}into\s+your\s+hand", _P),
]

# Draw that is pointed at opponents, not you. Checked to avoid counting
# symmetrical or punisher effects as your card advantage.
OPPONENT_DRAW_PATTERNS = [
    re.compile(r"(each\s+opponent|target\s+opponent|that\s+player)\s+draws?", _P),
]

RECURSION_PATTERNS = [
    # "a graveyard" as well as "your graveyard" -- Reanimate and Animate Dead
    # both target any graveyard, and reanimation is the main use of this role.
    re.compile(r"from\s+(your|a|target player's)\s+graveyard\s+to\s+", _P),
    re.compile(r"return\s+.{0,40}from\s+(your|a)\s+graveyard", _P),
    re.compile(
        r"put\s+.{0,40}from\s+(your|a)\s+graveyard\s+onto\s+the\s+battlefield", _P
    ),
    re.compile(r"\breanimate\b", _P),
]

PROTECTION_PATTERNS = [
    re.compile(r"\bhexproof\b", _P),
    re.compile(r"\bindestructible\b", _P),
    re.compile(r"\bshroud\b", _P),
    re.compile(r"counter\s+target\b", _P),
    re.compile(r"choose\s+new\s+targets?", _P),
    re.compile(r"\bprotection\s+from\b", _P),
    re.compile(r"\bphases?\s+out\b", _P),
    re.compile(r"change\s+the\s+target", _P),
    re.compile(r"\bregenerate\b", _P),
    re.compile(r"sacrifice\s+.{0,30}\s+instead", _P),
]

WINCON_PATTERNS = [
    re.compile(r"\byou\s+win\s+the\s+game\b", _P),
    re.compile(r"\bwins\s+the\s+game\b", _P),
    re.compile(r"\bloses\s+the\s+game\b", _P),
    re.compile(r"can't\s+lose\s+the\s+game", _P),
]

# Reminder text in parentheses restates rules and produces false matches --
# a card with "(This creature can't be blocked...)" should not read as its
# own effect. Stripped before matching.
REMINDER_TEXT = re.compile(r"\([^)]*\)")

# Role priority when a card matches several. Earlier wins the primary slot.
PRIORITY = [
    LAND,
    RAMP,
    SWEEPER,
    SPOT_REMOVAL,
    TUTOR,
    DRAW,
    RECURSION,
    PROTECTION,
    WINCON,
]


def _matches(patterns, text):
    return any(p.search(text) for p in patterns)


def candidate_roles(type_line, oracle_text):
    """Every role a card plausibly fills, unordered."""
    type_line = type_line or ""
    text = REMINDER_TEXT.sub("", oracle_text or "")

    roles = set()

    if "Land" in type_line:
        roles.add(LAND)

    if _matches(RAMP_PATTERNS, text):
        roles.add(RAMP)

    if _matches(SWEEPER_PATTERNS, text):
        roles.add(SWEEPER)

    if _matches(SPOT_REMOVAL_PATTERNS, text):
        roles.add(SPOT_REMOVAL)

    # A land search is ramp, not a tutor. Only count as a tutor if the search
    # is for something other than a land.
    if _matches(TUTOR_PATTERNS, text) and RAMP not in roles:
        roles.add(TUTOR)

    # Only count draw that benefits you.
    if _matches(CARD_DRAW_PATTERNS, text) and not _matches(
        OPPONENT_DRAW_PATTERNS, text
    ):
        roles.add(DRAW)

    if _matches(RECURSION_PATTERNS, text):
        roles.add(RECURSION)

    if _matches(PROTECTION_PATTERNS, text):
        roles.add(PROTECTION)

    if _matches(WINCON_PATTERNS, text):
        roles.add(WINCON)

    return roles


def classify(type_line, oracle_text):
    """Return (primary_role, secondary_role) for a card.

    `secondary_role` is "" when the card fills only one role. Cards matching
    nothing fall through to SYNERGY, which is the solver's catch-all bucket.
    """
    roles = candidate_roles(type_line, oracle_text)

    if not roles:
        return SYNERGY, ""

    ordered = [r for r in PRIORITY if r in roles]
    primary = ordered[0]
    secondary = ordered[1] if len(ordered) > 1 else ""
    return primary, secondary
