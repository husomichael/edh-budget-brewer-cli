"""Assign a functional role to every card.

Separate from `sync_cards` because classification rules get tuned frequently
and re-downloading 25 MB of bulk data to re-tune would be wasteful.

Usage:
    manage.py classify_cards [--only-unclassified] [--report]
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db.models import Count

from cards.classification import ROLE_CHOICES, classify
from cards.models import Card

OVERRIDES_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "role_overrides.json"
)

BATCH_SIZE = 2000


class Command(BaseCommand):
    help = "Classify cards into functional roles (ramp, draw, removal, ...)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--only-unclassified",
            action="store_true",
            help="Only classify cards with no role_source yet.",
        )
        parser.add_argument(
            "--report",
            action="store_true",
            help="Print a role distribution summary when finished.",
        )

    def handle(self, *args, **options):
        overrides, always_include = self._load_overrides()
        self.stdout.write(
            f"Loaded {len(overrides)} overrides, "
            f"{len(always_include)} always-include entries."
        )

        qs = Card.objects.all()
        if options["only_unclassified"]:
            qs = qs.filter(role_source="")

        total = qs.count()
        self.stdout.write(f"Classifying {total} cards ...")

        batch = []
        counts = {"heuristic": 0, "override": 0}
        matched_overrides = set()

        for card in qs.iterator(chunk_size=BATCH_SIZE):
            if card.name in overrides:
                primary, secondary = overrides[card.name]
                source = "override"
                matched_overrides.add(card.name)
            else:
                primary, secondary = classify(card.type_line, card.oracle_text)
                source = "heuristic"

            card.primary_role = primary
            card.secondary_role = secondary
            card.role_source = source
            counts[source] += 1
            batch.append(card)

            if len(batch) >= BATCH_SIZE:
                self._flush(batch)
                batch.clear()

        if batch:
            self._flush(batch)

        self.stdout.write(
            self.style.SUCCESS(
                f"Classified {counts['heuristic']} by heuristic, "
                f"{counts['override']} by override."
            )
        )

        # Surface typos in the override table rather than failing silently.
        unmatched = set(overrides) - matched_overrides
        if unmatched and not options["only_unclassified"]:
            self.stdout.write(
                self.style.WARNING(
                    f"{len(unmatched)} override names matched no card "
                    f"(typo or not yet synced): {sorted(unmatched)}"
                )
            )

        if options["report"]:
            self._report()

    def _flush(self, batch):
        Card.objects.bulk_update(
            batch, ["primary_role", "secondary_role", "role_source"]
        )

    def _load_overrides(self):
        with open(OVERRIDES_PATH) as fh:
            data = json.load(fh)
        overrides = {
            name: (roles[0], roles[1] if len(roles) > 1 else "")
            for name, roles in data.get("overrides", {}).items()
        }
        return overrides, set(data.get("_always_include", []))

    def _report(self):
        self.stdout.write("")
        self.stdout.write("Role distribution (commander-legal, priced cards):")
        rows = (
            Card.objects.filter(legal_commander=True, price_cents__isnull=False)
            .values("primary_role")
            .annotate(n=Count("id"))
            .order_by("-n")
        )
        labels = dict(ROLE_CHOICES)
        for row in rows:
            role = row["primary_role"]
            self.stdout.write(f"  {labels.get(role, role):16} {row['n']:6}")
