"""Candidate pool construction.

Everything upstream -- legality, color identity, pricing, roles, scoring --
resolves here. The solver receives a clean list of `Candidate` objects and
nothing else.
"""

from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Q

from cards.models import Card, CollectionItem, Deck
from cards.scoring import Tier0PopularityStrategy

# Fields pulled from the database. Using .values() rather than model instances
# avoids constructing ~24k Card objects per brew.
POOL_FIELDS = (
    "oracle_id",
    "name",
    "price_cents",
    "cmc",
    "mana_cost",
    "type_line",
    "oracle_text",
    "image_uri",
    "primary_role",
    "secondary_role",
    "edhrec_rank",
    "is_land",
    "is_basic",
    "color_identity",
)


@dataclass(frozen=True)
class Candidate:
    """One card the solver may choose, with everything it needs to decide."""

    oracle_id: str
    name: str
    # What this card costs the user. 0 for basics and for owned cards under
    # own-it-already pricing.
    price_cents: int
    # What it costs at retail regardless of ownership, so output can show both
    # "new spend" and "retail value".
    retail_cents: int
    is_owned: bool
    # Name of an assembled deck already using this card, if any. An owned card
    # that is sleeved up is not really free -- taking it means dismantling
    # that deck -- so the conflict is surfaced rather than decided silently.
    locked_in: str
    cmc: Decimal
    mana_cost: str
    type_line: str
    image_uri: str
    primary_role: str
    secondary_role: str
    is_land: bool
    is_basic: bool
    score: float

    @property
    def score_per_dollar(self):
        """Value density, used by the greedy fill.

        Free cards (basic lands, cards already owned) would divide by zero, so
        they are treated as maximally efficient -- which is correct: a free
        card that fills a needed slot is always worth taking.
        """
        if self.price_cents <= 0:
            return float("inf")
        return self.score / (self.price_cents / 100.0)


@dataclass(frozen=True)
class CandidatePool:
    commander: Card
    candidates: list
    tier: int
    tier_label: str
    owned_free: bool = False

    def __len__(self):
        return len(self.candidates)

    def by_role(self, role):
        return [c for c in self.candidates if c.primary_role == role]

    def filling_role(self, role):
        """Candidates whose primary *or* secondary role matches.

        Used when a role's floor cannot be met from primaries alone -- a card
        that ramps and draws can cover either slot.
        """
        return [
            c
            for c in self.candidates
            if c.primary_role == role or c.secondary_role == role
        ]

    @property
    def description(self):
        return f"Tier {self.tier} - {self.tier_label}"


def build_pool(commander, strategy=None, owned_free=False, partner=None):
    """Assemble the scored, filtered candidate pool for a commander.

    Filters, in order of selectivity:

      * commander-legal and not banned
      * color identity is a subset of the commander's (Postgres array
        containment, GIN-indexed -- the hottest query in the project)
      * has a USD price, since an unpriceable card is unbuyable -- except
        basic lands, which Scryfall prices as null but which are free and
        unlimited, the one case where "no price" means free rather than
        unbuyable
      * is not the commander or partner itself
    """
    strategy = strategy or Tier0PopularityStrategy()

    qs = (
        Card.objects.filter(
            Q(price_cents__isnull=False) | Q(is_basic=True),
            legal_commander=True,
            is_banned=False,
            color_identity__contained_by=commander.color_identity,
        )
        .exclude(pk=commander.pk)
        .values(*POOL_FIELDS)
    )
    if partner is not None:
        qs = qs.exclude(pk=partner.pk)

    rows = list(qs)
    scored = strategy.score(rows, commander)

    owned = _owned_oracle_ids() if owned_free else set()
    locked = _locked_oracle_ids() if owned_free else {}

    candidates = [_make_candidate(row, scored, owned, locked) for row in rows]

    return CandidatePool(
        commander=commander,
        candidates=candidates,
        tier=scored.tier,
        tier_label=scored.label,
        owned_free=owned_free,
    )


def _make_candidate(row, scored, owned, locked):
    oracle_id = str(row["oracle_id"])
    is_owned = oracle_id in owned
    retail = row["price_cents"] or 0
    return Candidate(
        oracle_id=oracle_id,
        name=row["name"],
        # Basic lands are free in practice; own-it-already pricing zeroes out
        # cards already in the collection.
        price_cents=0 if (row["is_basic"] or is_owned) else retail,
        retail_cents=retail,
        is_owned=is_owned,
        locked_in=locked.get(oracle_id, ""),
        cmc=row["cmc"],
        mana_cost=row["mana_cost"],
        type_line=row["type_line"],
        image_uri=row["image_uri"],
        primary_role=row["primary_role"],
        secondary_role=row["secondary_role"],
        is_land=row["is_land"],
        is_basic=row["is_basic"],
        score=scored.scores.get(oracle_id, 0.0),
    )


def _owned_oracle_ids():
    """Oracle IDs of every card in the user's collection."""
    return {
        str(oid)
        for oid in CollectionItem.objects.values_list("card__oracle_id", flat=True)
    }


def _locked_oracle_ids():
    """Owned cards currently sleeved in an assembled deck, mapped to its name.

    Priced free like any owned card, but flagged, because using one means
    taking apart a deck that already exists.
    """
    rows = (
        Deck.objects.filter(is_assembled=True)
        .values_list("cards__card__oracle_id", "name")
        .order_by("name")
    )
    locked = {}
    for oracle_id, deck_name in rows:
        if oracle_id is None:
            continue
        locked.setdefault(str(oracle_id), deck_name)
    return locked
