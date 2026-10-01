"""Fetch Scryfall catalogs used by Tier 1 theme detection.

Creature types must come from a real list, not guesswork -- there are hundreds
and they change with every set. Scryfall publishes them as a catalog endpoint,
fetched once and cached as a checked-in fixture so Tier 1 needs no network
access at brew time.

Usage:
    manage.py sync_catalogs
"""

import json
import ssl
import urllib.request
from pathlib import Path

import certifi
from django.conf import settings
from django.core.management.base import BaseCommand

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

CATALOGS = {
    "creature_types": "https://api.scryfall.com/catalog/creature-types",
    "land_types": "https://api.scryfall.com/catalog/land-types",
    "artifact_types": "https://api.scryfall.com/catalog/artifact-types",
}

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


class Command(BaseCommand):
    help = "Fetch Scryfall catalogs (creature types, land types) as fixtures."

    def handle(self, *args, **options):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        headers = {
            "User-Agent": settings.SCRYFALL_USER_AGENT,
            "Accept": "application/json",
        }

        for name, url in CATALOGS.items():
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
                payload = json.load(resp)

            entries = sorted(payload.get("data", []))
            dest = DATA_DIR / f"{name}.json"
            dest.write_text(json.dumps(entries, indent=0) + "\n")
            self.stdout.write(
                self.style.SUCCESS(f"{name}: {len(entries)} entries -> {dest.name}")
            )
