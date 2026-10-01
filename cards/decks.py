"""Persisting generated decks."""

from django.db import transaction
from django.utils import timezone

from cards.models import Card, Deck, DeckCard


@transaction.atomic
def save_brew(result, name=None):
    """Persist a `Brew` as a `Deck` with its `DeckCard` rows.

    Prices are recorded as of generation time. Scryfall prices move, and
    without the snapshot there is no way to explain why a $100 deck is now a
    $140 deck.
    """
    budget = result.budget_cents / 100
    deck = Deck.objects.create(
        name=name or f"{result.commander.name} (${budget:.0f})",
        commander=result.commander,
        budget_cents=result.budget_cents,
        generated_at=timezone.now(),
        scoring_tier=result.tier,
        is_assembled=False,
    )

    # One query for every card in the deck rather than one per card.
    oracle_ids = [c.oracle_id for c in result.spells] + [
        c.oracle_id for c, _ in result.lands
    ]
    by_oracle = {
        str(card.oracle_id): card
        for card in Card.objects.filter(oracle_id__in=oracle_ids)
    }

    rows = []
    for cand in result.spells:
        card = by_oracle.get(cand.oracle_id)
        if card is None:
            continue
        rows.append(
            DeckCard(
                deck=deck,
                card=card,
                quantity=1,
                role=cand.primary_role,
                price_at_generation=cand.retail_cents,
            )
        )
    for cand, quantity in result.lands:
        card = by_oracle.get(cand.oracle_id)
        if card is None:
            continue
        rows.append(
            DeckCard(
                deck=deck,
                card=card,
                quantity=quantity,
                role=cand.primary_role,
                price_at_generation=cand.retail_cents,
            )
        )

    DeckCard.objects.bulk_create(rows)
    return deck
