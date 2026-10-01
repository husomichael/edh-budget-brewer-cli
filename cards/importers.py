"""Parsing pasted decklists and collection exports.

Tolerant by design: people paste from Moxfield, Archidekt, text files, and
spreadsheets, with set codes, collector numbers, section headers, and comments
mixed in. Unresolved names are *reported*, never dropped silently -- a bad
paste should be obvious rather than quietly producing a thinner collection.
"""

import re
import unicodedata

from cards.models import Card, CollectionItem

# "1 Sol Ring", "1x Sol Ring", "4 Mountain (LTR) 123", "Sol Ring"
LINE = re.compile(
    r"""^\s*
    (?:(?P<qty>\d+)\s*[xX]?\s+)?      # optional leading quantity
    (?P<name>[^(\[\n]+?)              # name, stopping before (SET) or [tag]
    (?:\s*[(\[].*)?                   # optional set code / collector number
    \s*$""",
    re.VERBOSE,
)

# Section headers and comments that are not cards.
SKIP = re.compile(
    r"^\s*(//|#|$|sideboard|commander|companion|maybeboard|deck\b|total\b)",
    re.IGNORECASE,
)


def normalize(name):
    """Fold a card name to a comparable form.

    Handles the mismatches that actually occur: curly vs straight apostrophes
    (`Lim-Dul's Vault`), accented characters (`Bartolome del Presidio`), and
    split-card separators (`Fire // Ice`).
    """
    name = name.strip()
    # Straight-quote curly apostrophes before stripping accents.
    name = name.replace("’", "'").replace("‘", "'")
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = re.sub(r"\s*//\s*", " // ", name)
    return re.sub(r"\s+", " ", name).casefold()


def parse_lines(text):
    """Extract (quantity, name) pairs from pasted text."""
    entries = []
    for raw in text.splitlines():
        if SKIP.match(raw):
            continue
        match = LINE.match(raw)
        if not match:
            continue
        name = (match.group("name") or "").strip()
        if not name:
            continue
        quantity = int(match.group("qty") or 1)
        entries.append((quantity, name))
    return entries


def resolve_names(names):
    """Map pasted names to Card rows.

    Returns (resolved, unresolved). Matching is done on a normalized form, and
    split cards are also tried by their front face, since people often paste
    `Fire` for `Fire // Ice`.
    """
    wanted = {normalize(n): n for n in names}

    lookup = {}
    for card in Card.objects.only("id", "oracle_id", "name"):
        key = normalize(card.name)
        lookup.setdefault(key, card)
        if "//" in card.name:
            front = normalize(card.name.split("//")[0])
            lookup.setdefault(front, card)

    resolved = {}
    unresolved = []
    for key, original in wanted.items():
        card = lookup.get(key)
        if card is None:
            unresolved.append(original)
        else:
            resolved[original] = card
    return resolved, unresolved


def import_collection(text, replace=False):
    """Load pasted card names into the user's collection.

    Returns a summary including every name that could not be resolved, so a
    typo or an unsupported export format is visible rather than silent.
    """
    entries = parse_lines(text)
    if not entries:
        return {
            "parsed": 0,
            "created": 0,
            "updated": 0,
            "unresolved": [],
            "detail": "No card lines found in the pasted text.",
        }

    totals = {}
    for quantity, name in entries:
        totals[name] = totals.get(name, 0) + quantity

    resolved, unresolved = resolve_names(totals.keys())

    if replace:
        CollectionItem.objects.all().delete()

    created = updated = 0
    for name, card in resolved.items():
        item, was_created = CollectionItem.objects.get_or_create(
            card=card, defaults={"quantity": totals[name]}
        )
        if was_created:
            created += 1
        else:
            item.quantity = totals[name]
            item.save(update_fields=["quantity"])
            updated += 1

    return {
        "parsed": len(entries),
        "created": created,
        "updated": updated,
        "unresolved": sorted(unresolved),
        "detail": (
            f"Imported {created + updated} cards"
            + (
                f", {len(unresolved)} "
                f"{'name' if len(unresolved) == 1 else 'names'} unresolved."
                if unresolved
                else "."
            )
        ),
    }
