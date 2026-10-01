"""Tests for deck/collection models and persistence."""

import uuid

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase

from cards.classification import LAND, RAMP, SYNERGY
from cards.decks import save_brew
from cards.models import Card, CollectionItem, Deck, DeckCard
from cards.pool import build_pool
from cards.solver import brew


def make_card(name, **kwargs):
    defaults = {
        "oracle_id": uuid.uuid4(),
        "scryfall_id": uuid.uuid4(),
        "name": name,
        "cmc": 2,
        "mana_cost": "{1}{R}",
        "type_line": "Creature",
        "color_identity": ["R"],
        "price_cents": 100,
        "legal_commander": True,
        "edhrec_rank": 500,
        "primary_role": SYNERGY,
    }
    defaults.update(kwargs)
    return Card.objects.create(**defaults)


class DeckModelTests(TestCase):
    def setUp(self):
        self.commander = make_card(
            "Cmdr", type_line="Legendary Creature — Goblin", can_be_commander=True
        )
        self.deck = Deck.objects.create(name="Test", commander=self.commander)

    def test_singleton_is_enforced_by_the_database(self):
        card = make_card("Some Spell")
        DeckCard.objects.create(deck=self.deck, card=card)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DeckCard.objects.create(deck=self.deck, card=card)

    def test_non_basic_cannot_be_a_multiple(self):
        card = make_card("Sol Ring Clone", primary_role=RAMP)
        row = DeckCard(deck=self.deck, card=card, quantity=4)
        with self.assertRaises(ValidationError):
            row.clean()

    def test_basics_may_be_multiples(self):
        mountain = make_card(
            "Mountain",
            type_line="Basic Land — Mountain",
            is_land=True,
            is_basic=True,
            primary_role=LAND,
        )
        row = DeckCard(deck=self.deck, card=mountain, quantity=22)
        row.clean()  # must not raise
        row.save()
        self.assertEqual(self.deck.cards.get(card=mountain).quantity, 22)

    def test_card_count_includes_the_commander(self):
        DeckCard.objects.create(deck=self.deck, card=make_card("A"))
        DeckCard.objects.create(deck=self.deck, card=make_card("B"), quantity=1)
        self.assertEqual(self.deck.card_count, 3)

    def test_card_count_counts_quantities(self):
        mountain = make_card(
            "Mountain", type_line="Basic Land — Mountain", is_basic=True, is_land=True
        )
        DeckCard.objects.create(deck=self.deck, card=mountain, quantity=10)
        self.assertEqual(self.deck.card_count, 11)

    def test_generation_price_is_kept_separate_from_current_price(self):
        """Scryfall prices move; without the snapshot there is no way to say
        why a $100 deck is now a $140 deck."""
        card = make_card("Mover", price_cents=500)
        DeckCard.objects.create(deck=self.deck, card=card, price_at_generation=100)
        self.assertEqual(self.deck.generation_cents, 100)
        self.assertEqual(self.deck.total_cents, 500)

    def test_commander_cannot_be_deleted_while_a_deck_uses_it(self):
        """on_delete=PROTECT, so losing a card cannot silently gut a deck."""
        with self.assertRaises(ProtectedError), transaction.atomic():
            self.commander.delete()


class CollectionItemTests(TestCase):
    def test_a_card_appears_at_most_once(self):
        card = make_card("Owned")
        CollectionItem.objects.create(card=card, quantity=2)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CollectionItem.objects.create(card=card)

    def test_assembled_decks_reports_conflicts(self):
        """An owned card sleeved in a built deck is not really available."""
        commander = make_card(
            "C2", type_line="Legendary Creature — Elf", can_be_commander=True
        )
        card = make_card("Contested")
        item = CollectionItem.objects.create(card=card)

        built = Deck.objects.create(
            name="Built", commander=commander, is_assembled=True
        )
        DeckCard.objects.create(deck=built, card=card)
        shelved = Deck.objects.create(
            name="Shelved", commander=commander, is_assembled=False
        )
        DeckCard.objects.create(deck=shelved, card=card)

        names = [d.name for d in item.assembled_decks]
        self.assertEqual(names, ["Built"])


class SaveBrewTests(TestCase):
    def setUp(self):
        self.commander = make_card(
            "Save Cmdr",
            type_line="Legendary Creature — Goblin",
            oracle_text="Create a 1/1 red Goblin creature token.",
            can_be_commander=True,
        )
        make_card(
            "Mountain",
            type_line="Basic Land — Mountain",
            is_land=True,
            is_basic=True,
            primary_role=LAND,
            price_cents=None,
        )
        for i in range(90):
            make_card(f"filler-{i}", price_cents=5 + i, edhrec_rank=1000 + i)

    def test_round_trips_a_generated_deck(self):
        result = brew(build_pool(self.commander), 5_000)
        deck = save_brew(result)

        self.assertEqual(deck.commander, self.commander)
        self.assertEqual(deck.budget_cents, 5_000)
        self.assertIsNotNone(deck.generated_at)
        self.assertEqual(deck.card_count, result.card_count)
        self.assertFalse(deck.is_assembled)

    def test_records_role_and_price_per_card(self):
        result = brew(build_pool(self.commander), 5_000)
        deck = save_brew(result)
        row = deck.cards.exclude(role="").first()
        self.assertTrue(row.role)
        self.assertIsNotNone(row.price_at_generation)

    def test_basics_are_saved_as_a_single_row_with_quantity(self):
        result = brew(build_pool(self.commander), 5_000)
        deck = save_brew(result)
        basics = deck.cards.filter(card__is_basic=True)
        self.assertEqual(basics.count(), 1)
        self.assertGreater(basics.first().quantity, 1)

    def test_custom_name_is_honoured(self):
        result = brew(build_pool(self.commander), 5_000)
        self.assertEqual(save_brew(result, name="My Deck").name, "My Deck")
