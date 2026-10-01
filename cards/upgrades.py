"""The marginal upgrade path.

Solve the same commander at several budgets and diff the results to produce an
ordered purchase sequence: *what is the single best next thing I can buy for
this deck?*

EDHREC's budget pages show cheap cards and Archidekt lets you filter by price.
Neither tells you the sequence from where you are to where you want to be, and
that sequence is what a player actually wants. It falls out almost free once
the solver exists.
"""

from dataclasses import dataclass

from cards.solver import InfeasibleBudget, brew

# Dollar steps above the base budget. The last entry is the aspirational
# ceiling -- what the deck wants to be with money removed as a constraint,
# which makes the gap legible.
DEFAULT_STEPS_CENTS = [2_500, 5_000, 10_000, 25_000]
NO_LIMIT_CENTS = 10_000_000


@dataclass(frozen=True)
class UpgradeStep:
    """One budget tier and what changed relative to the previous one."""

    budget_cents: int
    label: str
    added: list
    removed: list
    total_cents: int
    score: float
    score_delta: float
    is_no_limit: bool = False

    @property
    def spend_delta_cents(self):
        """Extra money this tier costs over the previous one."""
        return sum(c.price_cents for c in self.added) - sum(
            c.price_cents for c in self.removed
        )

    @property
    def cost_per_point(self):
        """Dollars per point of score gained. Lower is better value."""
        if self.score_delta <= 0:
            return None
        return (self.spend_delta_cents / 100.0) / self.score_delta


@dataclass(frozen=True)
class UpgradePath:
    base: object  # the Brew at the starting budget
    steps: list

    @property
    def best_next_buys(self):
        """Cards from the first tier, priciest last, as a purchase order.

        Within a tier the cheapest additions are listed first so the user can
        stop partway and still have spent their money well.
        """
        if not self.steps:
            return []
        return sorted(self.steps[0].added, key=lambda c: (c.price_cents, c.name))


def upgrade_path(pool, base_budget_cents, steps_cents=None, include_no_limit=True):
    """Build the upgrade path for a commander from `base_budget_cents` up.

    Deck selection is **not monotonic**: a tier with more money may differ from
    the previous one by more than a single card, because extra budget in one
    role can enable a cheaper reshuffle in another. So each tier reports a *set*
    of changes rather than pretending to be a single-card chain.
    """
    steps_cents = DEFAULT_STEPS_CENTS if steps_cents is None else list(steps_cents)

    base = brew(pool, base_budget_cents)
    previous = base
    results = []

    budgets = [(base_budget_cents + s, f"+${s / 100:.0f}", False) for s in steps_cents]
    if include_no_limit:
        budgets.append((NO_LIMIT_CENTS, "no limit", True))

    for budget, label, is_no_limit in budgets:
        try:
            candidate = brew(pool, budget)
        except InfeasibleBudget:
            continue

        before = {c.oracle_id: c for c in previous.spells}
        after = {c.oracle_id: c for c in candidate.spells}

        added = [after[k] for k in after.keys() - before.keys()]
        removed = [before[k] for k in before.keys() - after.keys()]

        # Deterministic ordering: best cards first, then by id for ties.
        added.sort(key=lambda c: (-c.score, c.oracle_id))
        removed.sort(key=lambda c: (-c.score, c.oracle_id))

        results.append(
            UpgradeStep(
                budget_cents=budget,
                label=label,
                added=added,
                removed=removed,
                total_cents=candidate.total_cents,
                score=candidate.total_score,
                score_delta=candidate.total_score - previous.total_score,
                is_no_limit=is_no_limit,
            )
        )
        previous = candidate

    return UpgradePath(base=base, steps=results)
