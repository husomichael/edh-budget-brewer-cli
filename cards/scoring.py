"""Scoring strategies.

A strategy turns a set of candidate cards into a score in 0..1 for a given
commander. Everything downstream -- the candidate pool, the quota filler, the
solver -- is tier-agnostic and only sees normalized scores, so adding a data
source never touches the solver.

Tiers:

  0  `edhrec_rank` from Scryfall. Global EDH popularity, not commander
     specific. Produces a legal, playable, generic deck.
  1  Synergy inferred from the commander's own oracle text (see tier1.py).
  2  Inclusion percentages the user pasted in from EDHREC, per commander.

Tier 0 is the floor: it works for every commander with no input beyond a name
and no data beyond the Scryfall sync.
"""

import math
from dataclasses import dataclass

# Scryfall ranks roughly 32k cards. Cards outside the rank set have never
# meaningfully appeared in an EDH deck, so they score 0 rather than being
# dropped -- the solver may still need them to fill a thin role.
RANK_CEILING = 32000

TIER_LABELS = {
    0: "popularity (generic)",
    1: "commander text (thematic)",
    2: "EDHREC percentages (crowd-validated)",
}


@dataclass(frozen=True)
class ScoredPool:
    """Scores for one commander, plus provenance for the UI to report."""

    scores: dict  # oracle_id (str) -> float in 0..1
    tier: int
    label: str

    @property
    def description(self):
        return f"Tier {self.tier} - {self.label}"


def normalize_rank(rank):
    """Map an `edhrec_rank` to a 0..1 score, higher being more played.

    EDH popularity is power-law distributed: rank 1 (Sol Ring) is far more
    universal than rank 100 is over rank 200. A linear normalization squashes
    every staple into the top sliver of the range and cannot tell them apart,
    so this uses a log scale.

        rank      1 -> 1.00
        rank     10 -> 0.78
        rank    100 -> 0.56
        rank  1,000 -> 0.34
        rank 10,000 -> 0.11
    """
    if rank is None or rank <= 0:
        return 0.0
    if rank >= RANK_CEILING:
        return 0.0
    return 1.0 - (math.log(rank) / math.log(RANK_CEILING))


class Tier0PopularityStrategy:
    """Score purely by global EDH popularity.

    Knows Sol Ring is ubiquitous. Does not know that a Krenko deck wants
    Goblins -- that is Tier 1's job. Output should be labeled "generic" in any
    UI so the user knows which engine produced the deck.
    """

    tier = 0
    label = TIER_LABELS[0]

    def score(self, cards, commander=None):
        return ScoredPool(
            scores={
                str(c["oracle_id"]): normalize_rank(c["edhrec_rank"]) for c in cards
            },
            tier=self.tier,
            label=self.label,
        )
