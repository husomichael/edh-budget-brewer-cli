"""The budget deck solver.

Picks 99 cards maximizing total score subject to a dollar budget *and* role
quotas. Multi-constraint knapsack is NP-hard, so this is a heuristic: build the
cheapest legal deck that satisfies the structure, then spend the remaining
budget on the best upgrades available. That lands within a few percent of
optimal in milliseconds, which is the right trade for an interactive tool.

Why not simpler approaches:

  * Sort by score, buy until broke -> no lands, no ramp, no interaction.
  * Sort by score-per-dollar -> 99 one-mana cards.

The constraints are what make it a deck rather than a pile.
"""

from dataclasses import dataclass, field

from cards.classification import (
    DRAW,
    PROTECTION,
    RAMP,
    RECURSION,
    SPOT_REMOVAL,
    SWEEPER,
    SYNERGY,
    TUTOR,
    WINCON,
)
from cards.manabase import (
    DEFAULT_LAND_COUNT,
    build_mana_base,
    color_weights_from_cards,
)

DECK_SIZE = 100  # including the commander

# Fraction of total budget reserved for the mana base before spells are
# considered. Lands span four orders of magnitude in price and will eat an
# unbounded budget if left to compete with spells.
LAND_BUDGET_FRACTION = 0.15

# (floor, ceiling) per role. Soft on purpose: the solver can trade a sweeper
# for a draw spell when prices force it, which a hard count forbids.
DEFAULT_QUOTAS = {
    RAMP: (8, 12),
    DRAW: (8, 12),
    SPOT_REMOVAL: (6, 10),
    SWEEPER: (2, 4),
    PROTECTION: (2, 5),
    RECURSION: (1, 4),
    WINCON: (1, 3),
    TUTOR: (0, 3),
    SYNERGY: (0, 99),  # absorbs whatever is left
}

MAX_UPGRADE_PASSES = 12

# Per role, how many of the top-scoring candidates to consider when hunting for
# upgrades. Anything further down cannot beat a card already in the deck.
CANDIDATES_PER_ROLE = 300


class InfeasibleBudget(Exception):
    """Raised when no legal deck exists at the requested budget."""

    def __init__(self, message, minimum_cents):
        super().__init__(message)
        self.minimum_cents = minimum_cents


@dataclass
class Brew:
    """A generated deck."""

    commander: object
    lands: list  # [(Candidate, quantity)]
    spells: list  # [Candidate]
    budget_cents: int
    land_cents: int
    spell_cents: int
    tier: int
    tier_label: str
    warnings: list = field(default_factory=list)

    @property
    def total_cents(self):
        """New money required. Equals retail unless own-it-already pricing."""
        return self.land_cents + self.spell_cents

    @property
    def retail_cents(self):
        """What the deck would cost buying every card at retail."""
        spells = sum(c.retail_cents for c in self.spells)
        lands = sum(c.retail_cents * q for c, q in self.lands)
        return spells + lands

    @property
    def owned_cards(self):
        """Cards supplied from the collection rather than bought."""
        return [c for c in self.spells if c.is_owned] + [
            c for c, _ in self.lands if c.is_owned
        ]

    @property
    def locked_cards(self):
        """Owned cards that are currently sleeved in an assembled deck."""
        return [c for c in self.owned_cards if c.locked_in]

    @property
    def card_count(self):
        """Cards in the deck including the commander. Should be 100."""
        lands = sum(qty for _, qty in self.lands)
        return lands + len(self.spells) + 1

    @property
    def role_counts(self):
        """Count by primary role. Sums to the number of nonland cards."""
        counts = {}
        for cand in self.spells:
            counts[cand.primary_role] = counts.get(cand.primary_role, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    @property
    def role_coverage(self):
        """How many cards *provide* each role, counting secondary roles.

        A card that ramps and draws covers both. This is what quota floors are
        measured against -- "does the deck have enough draw" is a question
        about function, not about primary classification. Unlike role_counts,
        these do not sum to the nonland count.
        """
        coverage = {}
        for cand in self.spells:
            for role in (cand.primary_role, cand.secondary_role):
                if role:
                    coverage[role] = coverage.get(role, 0) + 1
        return dict(sorted(coverage.items(), key=lambda kv: -kv[1]))

    @property
    def curve(self):
        """Mana-value histogram of nonland cards, bucketed 1..6+."""
        buckets = {}
        for cand in self.spells:
            cmc = int(cand.cmc)
            key = "6+" if cmc >= 6 else str(cmc)
            buckets[key] = buckets.get(key, 0) + 1
        order = ["0", "1", "2", "3", "4", "5", "6+"]
        return {k: buckets.get(k, 0) for k in order if buckets.get(k)}

    @property
    def total_score(self):
        return sum(c.score for c in self.spells) + sum(c.score for c, _ in self.lands)


def brew(pool, budget_cents, quotas=None, land_count=DEFAULT_LAND_COUNT):
    """Generate a deck for `pool.commander` within `budget_cents`.

    Deterministic: identical inputs always produce an identical deck. Every
    sort key ends in `oracle_id` so ties break consistently rather than
    depending on dictionary ordering.
    """
    quotas = quotas or DEFAULT_QUOTAS
    warnings = []

    spells_available = [c for c in pool.candidates if not c.is_land]

    # --- pass 1: provisional mana base, before spells are known -----------
    land_budget = int(budget_cents * LAND_BUDGET_FRACTION)
    mana = build_mana_base(pool, land_budget, land_count)
    spell_slots = DECK_SIZE - 1 - sum(qty for _, qty in mana.lands)
    spell_budget = budget_cents - mana.total_cents

    _check_feasible(spells_available, spell_slots, spell_budget, mana.total_cents)

    # --- cheapest legal deck that satisfies the quota floors --------------
    chosen, spent, primary_counts = _fill_floors(
        pool, quotas, spell_slots, spell_budget
    )
    chosen, spent, primary_counts = _fill_remainder(
        spells_available,
        chosen,
        spent,
        primary_counts,
        quotas,
        spell_slots,
        spell_budget,
    )

    # --- spend what is left on the best available upgrades ----------------
    chosen, spent = _upgrade(spells_available, chosen, spent, spell_budget, quotas)

    # --- pass 2: rebuild the mana base now that pips are known ------------
    # The first pass had to guess at color weights because no spells existed
    # yet. Now they do, so the basics can match what the deck actually casts.
    weights = color_weights_from_cards(chosen.values(), pool.commander.color_identity)
    if weights:
        mana2 = build_mana_base(
            pool, land_budget, land_count, chosen_spells=list(chosen.values())
        )
        if sum(q for _, q in mana2.lands) == sum(q for _, q in mana.lands):
            mana = mana2

    warnings.extend(mana.warnings)

    spells = sorted(chosen.values(), key=lambda c: (c.primary_role, -c.score, c.name))

    result = Brew(
        commander=pool.commander,
        lands=sorted(
            mana.lands, key=lambda lq: (lq[0].is_basic, -lq[0].score, lq[0].name)
        ),
        spells=spells,
        budget_cents=budget_cents,
        land_cents=mana.total_cents,
        spell_cents=spent,
        tier=pool.tier,
        tier_label=pool.tier_label,
        warnings=warnings,
    )

    if result.card_count != DECK_SIZE:
        result.warnings.append(
            f"Deck has {result.card_count} cards, expected {DECK_SIZE}."
        )
    _warn_unmet_floors(result, quotas)
    for cand in result.locked_cards:
        result.warnings.append(
            f'"{cand.name}" is owned but sleeved in "{cand.locked_in}" -- '
            f"using it means taking that deck apart."
        )
    return result


# -- stages ------------------------------------------------------------------


def _check_feasible(spells, slots, spell_budget, land_cents):
    """Reject budgets no legal deck can satisfy, naming the real minimum.

    A $5 five-color deck cannot be built; saying so beats emitting a deck
    that is quietly 20 cards short.
    """
    cheapest = sorted(spells, key=lambda c: (c.price_cents, c.oracle_id))[:slots]
    if len(cheapest) < slots:
        raise InfeasibleBudget(
            f"Only {len(cheapest)} legal cards available for {slots} slots.",
            minimum_cents=0,
        )
    floor = sum(c.price_cents for c in cheapest)
    if floor > spell_budget:
        total_min = floor + land_cents
        raise InfeasibleBudget(
            f"Budget too low: the cheapest legal deck for this commander "
            f"costs about ${total_min / 100:.2f}.",
            minimum_cents=total_min,
        )


def _fill_floors(pool, quotas, slots, spell_budget):
    """Meet every role's floor using the cheapest cards that qualify.

    Cheapest-first rather than best-first: this stage only has to produce a
    structurally valid deck. Quality comes from the upgrade pass, which can
    then spend the whole remaining budget deliberately instead of having it
    consumed by whichever role happened to be filled first.
    """
    chosen = {}
    # Keyed by PRIMARY role. A card recruited to cover the draw floor via its
    # secondary role still occupies a slot in its primary role's ceiling, and
    # conflating the two lets a role overshoot its cap.
    primary_counts = {}
    spent = 0

    for role, (floor, _) in quotas.items():
        if floor <= 0:
            continue
        # filling_role() accepts secondary roles too, so a card that ramps and
        # draws can cover either floor when primaries run thin.
        available = sorted(
            (c for c in pool.filling_role(role) if not c.is_land),
            key=lambda c: (c.price_cents, -c.score, c.oracle_id),
        )
        covered = 0
        for cand in available:
            if covered >= floor or len(chosen) >= slots:
                break
            if cand.oracle_id in chosen:
                continue
            if spent + cand.price_cents > spell_budget:
                continue
            # Respect the ceiling of the card's own primary role.
            own = cand.primary_role
            _f, own_ceiling = quotas.get(own, (0, 99))
            if primary_counts.get(own, 0) >= own_ceiling:
                continue
            chosen[cand.oracle_id] = cand
            spent += cand.price_cents
            primary_counts[own] = primary_counts.get(own, 0) + 1
            covered += 1

    return chosen, spent, primary_counts


def _fill_remainder(spells, chosen, spent, counts, quotas, slots, spell_budget):
    """Fill the remaining slots cheaply, respecting role ceilings."""
    available = sorted(spells, key=lambda c: (c.price_cents, -c.score, c.oracle_id))

    for cand in available:
        if len(chosen) >= slots:
            break
        if cand.oracle_id in chosen:
            continue
        role = cand.primary_role
        _floor, ceiling = quotas.get(role, (0, 99))
        if counts.get(role, 0) >= ceiling:
            continue
        if spent + cand.price_cents > spell_budget:
            continue
        chosen[cand.oracle_id] = cand
        spent += cand.price_cents
        counts[role] = counts.get(role, 0) + 1

    return chosen, spent, counts


def _coverage_of(cards):
    """How many of `cards` cover each role, counting secondary roles."""
    coverage = {}
    for cand in cards:
        for role in (cand.primary_role, cand.secondary_role):
            if role:
                coverage[role] = coverage.get(role, 0) + 1
    return coverage


def _swap_keeps_floors(outgoing, incoming, coverage, quotas):
    """Whether replacing `outgoing` with `incoming` keeps every floor met.

    Swapping within a primary role preserves primary-role counts, but a card
    can also be the only thing covering a *secondary* role. Hedron Alignment
    is primary=protection, secondary=wincon; upgrading it to a better
    protection card silently dropped the deck's only win condition. So each
    swap has to be checked against coverage, not just counts.
    """
    incoming_roles = {incoming.primary_role, incoming.secondary_role}
    for role in (outgoing.primary_role, outgoing.secondary_role):
        if not role:
            continue
        after = coverage.get(role, 0) - 1
        if role in incoming_roles:
            after += 1
        if after < quotas.get(role, (0, 99))[0]:
            return False
    return True


def _upgrade(spells, chosen, spent, spell_budget, quotas):
    """Spend the remaining budget on the best available upgrades.

    Swaps are ranked by **score gained per dollar spent**, not by "best card I
    can currently afford". That distinction is the whole ballgame: taking the
    best affordable card per slot means the first slot examined sees the entire
    remaining budget as headroom and buys one expensive card, starving the
    other 60 slots. Ranking by density instead buys many solid mid-priced cards
    before one premium card, which is what a budget deck actually wants.

    Swapping within a role keeps every quota intact, so this can run freely
    without re-checking structure.
    """
    by_role = {}
    for cand in spells:
        by_role.setdefault(cand.primary_role, []).append(cand)
    for role in by_role:
        by_role[role].sort(key=lambda c: (-c.score, c.price_cents, c.oracle_id))
        # Cards far down the score order cannot beat anything already chosen,
        # so bounding the scan keeps swap enumeration cheap.
        del by_role[role][CANDIDATES_PER_ROLE:]

    coverage = _coverage_of(chosen.values())

    for _ in range(MAX_UPGRADE_PASSES):
        swaps = []
        for current in chosen.values():
            for cand in by_role.get(current.primary_role, []):
                gain = cand.score - current.score
                if gain <= 0:
                    break  # sorted by score; nothing better remains
                if cand.oracle_id in chosen:
                    continue
                cost = cand.price_cents - current.price_cents
                # A strictly cheaper upgrade is free money -- rank it first.
                density = gain / cost if cost > 0 else float("inf")
                swaps.append((density, cost, gain, current.oracle_id, cand))

        if not swaps:
            break

        # Deterministic ordering: density, then cheaper, then the candidate's
        # own id so ties never depend on dict iteration order.
        swaps.sort(key=lambda s: (-s[0], s[1], s[4].oracle_id))

        applied = 0
        for _density, cost, _gain, out_id, cand in swaps:
            if out_id not in chosen:
                continue  # that slot was already upgraded this pass
            if cand.oracle_id in chosen:
                continue
            if spent + cost > spell_budget:
                continue
            outgoing = chosen[out_id]
            if not _swap_keeps_floors(outgoing, cand, coverage, quotas):
                continue
            del chosen[out_id]
            spent += cand.price_cents - outgoing.price_cents
            chosen[cand.oracle_id] = cand
            for role in (outgoing.primary_role, outgoing.secondary_role):
                if role:
                    coverage[role] -= 1
            for role in (cand.primary_role, cand.secondary_role):
                if role:
                    coverage[role] = coverage.get(role, 0) + 1
            applied += 1

        if applied == 0:
            break

    return chosen, spent


def _warn_unmet_floors(result, quotas):
    """Warn only when a role is genuinely under-covered.

    Measured against role_coverage, not role_counts: a floor filled by a
    card's secondary role is still filled.
    """
    coverage = result.role_coverage
    for role, (floor, _ceiling) in quotas.items():
        have = coverage.get(role, 0)
        if floor and have < floor:
            result.warnings.append(
                f"Only {have} cards cover {role}, wanted {floor} "
                f"(budget too tight or pool too thin)."
            )
