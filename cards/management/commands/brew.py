"""Generate a budget Commander deck from the command line.

Usage:
    manage.py brew "Krenko, Mob Boss" --budget 75
    manage.py brew "Muldrotha, the Gravetide" --budget 200 --format text
    manage.py brew "Atraxa, Praetors' Voice" --budget 150 --format json
"""

import json

from django.core.management.base import BaseCommand, CommandError

from cards.decks import save_brew
from cards.models import Card
from cards.pool import build_pool
from cards.scoring import Tier0PopularityStrategy
from cards.solver import DEFAULT_LAND_COUNT, InfeasibleBudget, brew
from cards.tier1 import Tier1SynergyStrategy
from cards.upgrades import upgrade_path

ROLE_ORDER = [
    "ramp",
    "draw",
    "spot_removal",
    "sweeper",
    "protection",
    "recursion",
    "tutor",
    "wincon",
    "synergy",
]


class Command(BaseCommand):
    help = "Generate a budget Commander deck for a commander."

    def add_arguments(self, parser):
        parser.add_argument("commander", help="Commander name (exact or partial).")
        parser.add_argument(
            "--budget",
            type=float,
            required=True,
            help="Budget in dollars, e.g. --budget 75",
        )
        parser.add_argument(
            "--lands",
            type=int,
            default=DEFAULT_LAND_COUNT,
            help=f"Target land count (default {DEFAULT_LAND_COUNT}).",
        )
        parser.add_argument(
            "--strategy",
            choices=["auto", "tier0", "tier1"],
            default="auto",
            help=(
                "Scoring strategy. auto (default) uses tier1 commander-text "
                "synergy; tier0 is pure popularity."
            ),
        )
        parser.add_argument(
            "--upgrade-path",
            action="store_true",
            help="Also show what the next increments of budget would buy.",
        )
        parser.add_argument(
            "--owned-free",
            action="store_true",
            help=(
                "Price cards in your collection at $0, so the budget means "
                "new money to spend rather than total retail value."
            ),
        )
        parser.add_argument(
            "--save",
            action="store_true",
            help="Persist the generated deck.",
        )
        parser.add_argument(
            "--name",
            default="",
            help="Name for the saved deck (defaults to commander + budget).",
        )
        parser.add_argument(
            "--format",
            choices=["table", "text", "json"],
            default="table",
            help="table (default), text (paste-ready decklist), or json.",
        )

    def handle(self, *args, **options):
        commander = self._resolve_commander(options["commander"])
        budget_cents = int(round(options["budget"] * 100))

        strategy = (
            Tier0PopularityStrategy()
            if options["strategy"] == "tier0"
            else Tier1SynergyStrategy()
        )
        pool = build_pool(
            commander, strategy=strategy, owned_free=options["owned_free"]
        )
        try:
            result = brew(pool, budget_cents, land_count=options["lands"])
        except InfeasibleBudget as exc:
            raise CommandError(str(exc)) from exc

        renderer = {
            "table": self._render_table,
            "text": self._render_text,
            "json": self._render_json,
        }[options["format"]]
        renderer(result)

        if options["upgrade_path"] and options["format"] == "table":
            self._render_upgrade_path(pool, budget_cents)

        if options["save"]:
            deck = save_brew(result, name=options["name"] or None)
            self.stdout.write(
                self.style.SUCCESS(f'\nSaved as deck #{deck.pk}: "{deck.name}"')
            )

    # -- commander lookup --------------------------------------------------

    def _resolve_commander(self, name):
        """Find a commander by exact name, else by unique partial match."""
        exact = Card.objects.filter(name__iexact=name, can_be_commander=True).first()
        if exact:
            return exact

        matches = list(
            Card.objects.filter(name__icontains=name, can_be_commander=True)[:11]
        )
        if not matches:
            raise CommandError(
                f'No commander matching "{name}". Names must be legendary '
                f"creatures or cards that say they can be your commander."
            )
        if len(matches) == 1:
            return matches[0]

        listing = "\n  ".join(c.name for c in matches[:10])
        more = " (and more)" if len(matches) > 10 else ""
        raise CommandError(
            f'"{name}" matches {len(matches)} commanders{more}:\n  {listing}\n'
            f"Use a more specific name."
        )

    # -- renderers ---------------------------------------------------------

    def _render_table(self, r):
        w = self.stdout.write
        w("")
        w(self.style.MIGRATE_HEADING(f"COMMANDER: {r.commander.name}"))
        w("")

        grouped = {}
        for cand in r.spells:
            grouped.setdefault(cand.primary_role, []).append(cand)

        for role in ROLE_ORDER:
            cards = grouped.get(role)
            if not cards:
                continue
            w(
                self.style.MIGRATE_LABEL(
                    f"=== {role.upper().replace('_', ' ')} ({len(cards)}) ==="
                )
            )
            for cand in sorted(cards, key=lambda c: (-c.price_cents, c.name)):
                w(f"  {cand.name[:44]:<46} ${cand.price_cents / 100:>7.2f}")
            w("")

        lands = sum(q for _, q in r.lands)
        w(self.style.MIGRATE_LABEL(f"=== LANDS ({lands}) ==="))
        for cand, qty in r.lands:
            label = f"{cand.name} x{qty}" if qty > 1 else cand.name
            w(f"  {label[:44]:<46} ${cand.price_cents / 100:>7.2f}")
        w("")

        over = r.total_cents > r.budget_cents
        total = f"${r.total_cents / 100:.2f} / ${r.budget_cents / 100:.2f} budget"
        w(
            self.style.ERROR(f"Total: {total}  OVER BUDGET")
            if over
            else self.style.SUCCESS(f"Total: {total}")
        )
        w(f"  lands  ${r.land_cents / 100:.2f}")
        w(f"  spells ${r.spell_cents / 100:.2f}")
        if r.owned_cards:
            w(f"  new spend    ${r.total_cents / 100:.2f}")
            w(f"  retail value ${r.retail_cents / 100:.2f}")
            w(f"  from collection: {len(r.owned_cards)} cards")
        w(f"Cards: {r.card_count}")
        w("Curve: " + "  ".join(f"{k}:{v}" for k, v in r.curve.items()))
        w(f"Scoring: {r.tier_label} (tier {r.tier})")
        if r.tier == 0 or "no themes" in r.tier_label:
            w(
                "  Note: tier 0 ranks by global EDH popularity, so this deck is "
                "in the right\n  colors but is not synergy-aware for this "
                "commander specifically."
            )
        for warning in r.warnings:
            w(self.style.WARNING(f"  ! {warning}"))

    def _render_upgrade_path(self, pool, budget_cents):
        w = self.stdout.write
        path = upgrade_path(pool, budget_cents)

        w("")
        w(self.style.MIGRATE_HEADING("=== UPGRADE PATH ==="))
        w("")
        best = path.best_next_buys
        if best:
            w("Best next buys, cheapest first:")
            for cand in best[:8]:
                w(f"  ${cand.price_cents / 100:>7.2f}  {cand.name}")
            w("")

        for step in path.steps:
            tag = " (aspirational)" if step.is_no_limit else ""
            w(
                self.style.MIGRATE_LABEL(
                    f"{step.label}{tag} -> ${step.total_cents / 100:.2f} spent, "
                    f"score {step.score:.1f} ({step.score_delta:+.1f})"
                )
            )
            value = step.cost_per_point
            if value is not None:
                w(f"  ${value:.2f} per point of score gained")
            w(f"  {len(step.added)} cards in, {len(step.removed)} out")
            for cand in step.added[:5]:
                w(f"    + {cand.name[:38]:40} ${cand.price_cents / 100:>7.2f}")
            if len(step.added) > 5:
                w(f"    ... and {len(step.added) - 5} more")
            w("")

    def _render_text(self, r):
        """Paste-ready decklist. Imports directly into Moxfield or Archidekt."""
        w = self.stdout.write
        w(f"1 {r.commander.name}")
        for cand in sorted(r.spells, key=lambda c: c.name):
            w(f"1 {cand.name}")
        for cand, qty in sorted(r.lands, key=lambda lq: lq[0].name):
            w(f"{qty} {cand.name}")

    def _render_json(self, r):
        payload = {
            "commander": r.commander.name,
            "budget_cents": r.budget_cents,
            "total_cents": r.total_cents,
            "land_cents": r.land_cents,
            "spell_cents": r.spell_cents,
            "card_count": r.card_count,
            "tier": r.tier,
            "tier_label": r.tier_label,
            "curve": r.curve,
            "role_counts": r.role_counts,
            "role_coverage": r.role_coverage,
            "warnings": r.warnings,
            "spells": [
                {
                    "name": c.name,
                    "price_cents": c.price_cents,
                    "cmc": float(c.cmc),
                    "role": c.primary_role,
                    "secondary_role": c.secondary_role,
                    "score": round(c.score, 4),
                }
                for c in r.spells
            ],
            "lands": [
                {"name": c.name, "quantity": q, "price_cents": c.price_cents}
                for c, q in r.lands
            ],
        }
        self.stdout.write(json.dumps(payload, indent=2))
