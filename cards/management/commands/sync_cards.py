"""Sync the Card table from Scryfall's bulk `oracle_cards` data.

Scryfall asks that large lookups go through their bulk downloads rather than
the per-card API, so this fetches one file and processes it locally. Their
terms also require a descriptive User-Agent and an explicit Accept header,
both set from settings.

The bulk file is gzipped JSONL -- one JSON card object per line -- reached via
the `jsonl_download_uri` field of the bulk-data index. It is kept compressed on
disk (~25 MB) and streamed line by line, so neither the download nor the parse
holds the whole dataset in memory.

Usage:
    manage.py sync_cards [--force] [--dry-run] [--limit N]
"""

import gzip
import json
import shutil
import ssl
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import certifi
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cards.models import Card

# Layouts that are not real playable cards.
EXCLUDED_LAYOUTS = {
    "token",
    "double_faced_token",
    "emblem",
    "art_series",
    "vanguard",
    "scheme",
    "planar",
    "augment",
    "host",
}

EXCLUDED_SET_TYPES = {"memorabilia", "token"}

# Fields updated on conflict. oracle_id is the conflict target and is excluded.
UPDATE_FIELDS = [
    "scryfall_id",
    "name",
    "mana_cost",
    "cmc",
    "type_line",
    "oracle_text",
    "color_identity",
    "colors",
    "price_cents",
    "price_updated",
    "legal_commander",
    "is_banned",
    "edhrec_rank",
    "is_land",
    "is_basic",
    "can_be_commander",
    "layout",
    "image_uri",
    "synced_at",
]

BATCH_SIZE = 1000

# macOS python.org builds do not use the system certificate store, so point
# OpenSSL at certifi's bundle explicitly rather than depending on whether
# `Install Certificates.command` was ever run on this machine.
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class Command(BaseCommand):
    help = "Sync the Card table from Scryfall bulk oracle_cards data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-download even if the local file is already current.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and report counts without writing to the database.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Process only the first N cards. For quick testing.",
        )

    def handle(self, *args, **options):
        data_dir = Path(settings.SCRYFALL_DATA_DIR)
        data_dir.mkdir(parents=True, exist_ok=True)

        meta = self._fetch_bulk_metadata()
        updated_at = meta["updated_at"]
        download_uri = meta["jsonl_download_uri"]

        self.stdout.write(
            f"Scryfall oracle_cards updated {updated_at} "
            f"({meta.get('compressed_size', 0) / 1_000_000:.0f} MB compressed)"
        )

        local_path = data_dir / f"oracle-cards-{updated_at[:10]}.jsonl.gz"
        stamp_path = data_dir / "last_sync.json"

        if local_path.exists() and not options["force"]:
            self.stdout.write(f"Using cached download: {local_path.name}")
        else:
            self._download(download_uri, local_path)

        created, updated, skipped, malformed = self._load(
            local_path,
            dry_run=options["dry_run"],
            limit=options["limit"],
        )

        if not options["dry_run"]:
            stamp_path.write_text(
                json.dumps({"updated_at": updated_at, "synced": _now_iso()}, indent=2)
            )

        total = Card.objects.count()
        verb = "Would write" if options["dry_run"] else "Wrote"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb}: {created} created, {updated} updated. "
                f"Skipped {skipped} non-cards, {malformed} malformed. "
                f"Card table now holds {total} rows."
            )
        )

    # -- fetching ----------------------------------------------------------

    def _headers(self):
        return {
            "User-Agent": settings.SCRYFALL_USER_AGENT,
            "Accept": "application/json",
        }

    def _fetch_bulk_metadata(self):
        """Find the current oracle_cards entry in Scryfall's bulk-data index."""
        req = urllib.request.Request(
            settings.SCRYFALL_BULK_INDEX, headers=self._headers()
        )
        with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
            payload = json.load(resp)

        for entry in payload.get("data", []):
            if entry.get("type") == "oracle_cards":
                return entry
        raise CommandError("No oracle_cards entry in Scryfall's bulk-data index.")

    def _download(self, uri, dest):
        """Stream the bulk file to disk rather than loading it into memory.

        The body is already a gzip file, so it is written verbatim and
        decompressed at parse time.
        """
        self.stdout.write(f"Downloading to {dest.name} ...")
        tmp = dest.with_suffix(".tmp")
        req = urllib.request.Request(uri, headers=self._headers())
        with urllib.request.urlopen(req, timeout=300, context=SSL_CONTEXT) as resp:
            with open(tmp, "wb") as fh:
                shutil.copyfileobj(resp, fh, length=1024 * 256)
        tmp.replace(dest)
        size_mb = dest.stat().st_size / 1_000_000
        self.stdout.write(f"Downloaded {size_mb:.1f} MB")

    # -- loading -----------------------------------------------------------

    def _load(self, path, *, dry_run, limit):
        created = updated = skipped = malformed = 0
        batch = []
        seen = 0

        existing = set(
            str(oid) for oid in Card.objects.values_list("oracle_id", flat=True)
        )

        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                if limit is not None and seen >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                seen += 1

                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue

                if self._should_skip(raw):
                    skipped += 1
                    continue

                try:
                    card = self._to_card(raw)
                except (KeyError, TypeError, ValueError):
                    malformed += 1
                    continue

                if str(card.oracle_id) in existing:
                    updated += 1
                else:
                    created += 1
                    existing.add(str(card.oracle_id))

                batch.append(card)

                if len(batch) >= BATCH_SIZE:
                    if not dry_run:
                        self._flush(batch)
                    batch.clear()
                    self.stdout.write(f"  ... {seen} processed", ending="\r")

        if batch and not dry_run:
            self._flush(batch)

        self.stdout.write("")
        return created, updated, skipped, malformed

    @transaction.atomic
    def _flush(self, batch):
        Card.objects.bulk_create(
            batch,
            update_conflicts=True,
            unique_fields=["oracle_id"],
            update_fields=UPDATE_FIELDS,
        )

    def _should_skip(self, raw):
        if raw.get("layout") in EXCLUDED_LAYOUTS:
            return True
        if raw.get("set_type") in EXCLUDED_SET_TYPES:
            return True
        # Cards with no oracle_id cannot be keyed.
        if not raw.get("oracle_id"):
            return True
        return False

    def _to_card(self, raw):
        faces = raw.get("card_faces") or []

        # Double-faced and split cards carry mana_cost / oracle_text / images on
        # the faces rather than at the top level.
        mana_cost = raw.get("mana_cost")
        if mana_cost is None and faces:
            mana_cost = faces[0].get("mana_cost", "")

        oracle_text = raw.get("oracle_text")
        if oracle_text is None and faces:
            oracle_text = "\n//\n".join(
                f.get("oracle_text", "") for f in faces if f.get("oracle_text")
            )

        type_line = raw.get("type_line") or ""
        if not type_line and faces:
            type_line = " // ".join(f.get("type_line", "") for f in faces)

        image_uri = (raw.get("image_uris") or {}).get("normal", "")
        if not image_uri and faces:
            image_uri = (faces[0].get("image_uris") or {}).get("normal", "")

        commander_legality = (raw.get("legalities") or {}).get("commander", "not_legal")

        return Card(
            oracle_id=raw["oracle_id"],
            scryfall_id=raw["id"],
            name=raw["name"][:300],
            mana_cost=(mana_cost or "")[:100],
            cmc=Decimal(str(raw.get("cmc", 0) or 0)),
            type_line=type_line[:300],
            oracle_text=oracle_text or "",
            color_identity=raw.get("color_identity") or [],
            colors=raw.get("colors") or [],
            price_cents=_price_cents(raw.get("prices") or {}),
            price_updated=_now(),
            legal_commander=(commander_legality == "legal"),
            is_banned=(commander_legality == "banned"),
            edhrec_rank=raw.get("edhrec_rank"),
            is_land=_is_land(type_line),
            is_basic="Basic Land" in type_line,
            can_be_commander=_can_be_commander(type_line, oracle_text or ""),
            layout=(raw.get("layout") or "")[:40],
            image_uri=image_uri[:500],
        )


def _price_cents(prices):
    """USD price in integer cents, or None when Scryfall has no price.

    Never coerce a missing price to 0 -- the optimizer would treat an
    unbuyable card as free.
    """
    for key in ("usd", "usd_foil", "usd_etched"):
        value = prices.get(key)
        if value:
            try:
                return int(round(float(value) * 100))
            except (TypeError, ValueError):
                continue
    return None


def _is_land(type_line):
    """Whether the FRONT face is a land.

    Modal double-faced cards like Agadeem's Awakening have type_line
    "Sorcery // Land" -- they are spells that may optionally be played as a
    land, and counting them as lands pollutes the mana base. Pathway lands are
    "Land // Land" and correctly remain lands.
    """
    front = type_line.split("//")[0]
    return "Land" in front


def _can_be_commander(type_line, oracle_text):
    """Whether this card may be a deck's commander.

    The rule is legendary creatures, plus anything whose text explicitly says
    it can be. Backgrounds are excluded: they are chosen alongside a commander
    that has 'Choose a Background', they are not commanders themselves.
    """
    front = type_line.split("//")[0]
    if "Legendary" in front and "Creature" in front:
        return True
    return "can be your commander" in oracle_text.lower()


def _now():
    return datetime.now(UTC)


def _now_iso():
    return _now().isoformat()
