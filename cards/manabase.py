"""Mana base construction.

Lands are ~36 of 99 slots and span four orders of magnitude in price, so they
are reserved before anything else. A solver that treats lands as ordinary
candidates spends the whole budget on an Underground Sea and leaves nothing for
spells.

Basics are free and exempt from the singleton rule, which makes them the floor
every budget mana base is built on.
"""

import re
from dataclasses import dataclass, field

from cards.classification import LAND

# Basic land name for each color, plus colorless.
BASIC_FOR_COLOR = {
    "W": "Plains",
    "U": "Island",
    "B": "Swamp",
    "R": "Mountain",
    "G": "Forest",
}
COLORLESS_BASIC = "Wastes"

DEFAULT_LAND_COUNT = 36

# Fraction of the land count that may be nonbasic. Utility lands are good, but
# past this the mana base gets greedy and the budget suffers.
MAX_NONBASIC_FRACTION = 0.40

# No single nonbasic may consume more than this share of the land budget.
# Without it, greedy-by-score buys one $20 fetchland and leaves the rest of
# the mana base to basics -- worse than a dozen $0.50 utility lands.
MAX_SINGLE_LAND_FRACTION = 0.25

# Snow basics cost money and do nothing a free basic does not, absent snow
# payoffs. A budget tool should never buy them.
SNOW_PATTERN = re.compile(r"snow", re.IGNORECASE)

MANA_SYMBOL = re.compile(r"\{([^}]+)\}")


@dataclass(frozen=True)
class ManaBase:
    """A built mana base. `lands` holds (candidate, quantity) pairs."""

    lands: list
    total_cents: int
    color_weights: dict
    warnings: list = field(default_factory=list)

    @property
    def land_count(self):
        return sum(qty for _, qty in self.lands)

    @property
    def nonbasic_count(self):
        return sum(qty for c, qty in self.lands if not c.is_basic)


def color_weights_from_cards(candidates, color_identity):
    """Infer how much each color is needed from the mana costs of real picks.

    Counts colored pips across the given cards. Falls back to an even split
    when there is nothing to count -- which is the first-pass case, before any
    spells have been chosen.
    """
    colors = [c for c in color_identity if c in BASIC_FOR_COLOR]
    if not colors:
        return {}

    pips = dict.fromkeys(colors, 0)
    for cand in candidates:
        for symbol in MANA_SYMBOL.findall(cand.mana_cost or ""):
            for color in colors:
                # Hybrid and phyrexian symbols like {W/U} or {G/P} contain the
                # color letter, and each half counts toward that color.
                if color in symbol:
                    pips[color] += 1

    total = sum(pips.values())
    if total == 0:
        return {c: 1.0 / len(colors) for c in colors}
    return {c: pips[c] / total for c in colors}


def build_mana_base(
    pool,
    budget_cents,
    land_count=DEFAULT_LAND_COUNT,
    chosen_spells=None,
):
    """Build a mana base within `budget_cents`.

    `chosen_spells` lets the caller run this twice: once with nothing chosen to
    get an approximate base, then again once spells are picked so the color
    weights reflect what the deck actually casts. The second pass is what makes
    a three-color deck's basics match its real pip distribution.
    """
    warnings = []
    identity = list(pool.commander.color_identity)
    weights = color_weights_from_cards(chosen_spells or [], identity)

    by_name = {c.name: c for c in pool.candidates}
    nonbasic_cap = int(land_count * MAX_NONBASIC_FRACTION)
    single_cap = max(int(budget_cents * MAX_SINGLE_LAND_FRACTION), 50)

    # --- nonbasic lands, best first, within budget ---
    nonbasics = sorted(
        (
            c
            for c in pool.by_role(LAND)
            if not c.is_basic
            and not SNOW_PATTERN.search(c.name)
            and c.price_cents <= single_cap
        ),
        key=lambda c: (-c.score, c.price_cents, c.oracle_id),
    )

    chosen = []
    spent = 0
    for cand in nonbasics:
        if len(chosen) >= nonbasic_cap:
            break
        if spent + cand.price_cents > budget_cents:
            continue
        chosen.append((cand, 1))
        spent += cand.price_cents

    # --- basics fill the remainder, weighted by the deck's real pips ---
    remaining = land_count - len(chosen)
    if remaining > 0:
        basics = _distribute_basics(remaining, weights, by_name)
        if basics:
            chosen.extend(basics)
        else:
            # No basics available for this identity (colorless commander with
            # no Wastes synced, for instance). Fall back to more nonbasics.
            warnings.append(
                "No basic lands available for this color identity; "
                f"mana base is {len(chosen)} lands instead of {land_count}."
            )

    actual = sum(qty for _, qty in chosen)
    if actual < land_count:
        warnings.append(
            f"Mana base is {actual} lands, short of the {land_count} target."
        )

    return ManaBase(
        lands=chosen,
        total_cents=spent,
        color_weights=weights,
        warnings=warnings,
    )


def _distribute_basics(slots, weights, by_name):
    """Split `slots` basics across colors by weight.

    Uses largest-remainder so the quantities sum to exactly `slots` -- naive
    rounding loses or gains a land and leaves the deck at 98 or 100 cards.
    """
    if not weights:
        wastes = by_name.get(COLORLESS_BASIC)
        return [(wastes, slots)] if wastes else []

    available = {
        color: by_name[BASIC_FOR_COLOR[color]]
        for color in weights
        if BASIC_FOR_COLOR.get(color) in by_name
    }
    if not available:
        return []

    # Reweight over the colors we can actually supply.
    total_weight = sum(weights[c] for c in available) or 1.0
    exact = {c: slots * weights[c] / total_weight for c in available}

    counts = {c: int(v) for c, v in exact.items()}
    shortfall = slots - sum(counts.values())

    # Hand out the leftover slots to the largest fractional remainders.
    remainders = sorted(available, key=lambda c: (-(exact[c] - counts[c]), c))
    for color in remainders[:shortfall]:
        counts[color] += 1

    return [(available[c], n) for c, n in counts.items() if n > 0]
