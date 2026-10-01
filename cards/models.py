"""Card data mirrored from Scryfall.

This table is read-only application data. It is populated by
`manage.py sync_cards` and must never be hand-edited -- the next sync would
silently overwrite the change.
"""

from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models

from cards.classification import ROLE_CHOICES, SYNERGY

# Scryfall's WUBRG color codes.
COLOR_CHOICES = [
    ("W", "White"),
    ("U", "Blue"),
    ("B", "Black"),
    ("R", "Red"),
    ("G", "Green"),
]


class Card(models.Model):
    # oracle_id is stable across printings; scryfall_id identifies one printing.
    # We sync the `oracle_cards` bulk file, which is one row per logical card,
    # so oracle_id is the natural key.
    oracle_id = models.UUIDField(unique=True)
    # Not unique: this is whichever printing the oracle_cards file picked as
    # representative, and that choice can change between syncs. Making it
    # unique would make upserts fail on a second unique constraint.
    scryfall_id = models.UUIDField()

    name = models.CharField(max_length=300, db_index=True)
    mana_cost = models.CharField(max_length=100, blank=True)
    # Wide enough for Un-set absurdities: Gleemax costs {1000000}, so a
    # 5-digit field overflows on a real card in the bulk data.
    cmc = models.DecimalField(max_digits=12, decimal_places=1, default=0)
    type_line = models.CharField(max_length=300, blank=True)
    oracle_text = models.TextField(blank=True)

    # ArrayField rather than JSONField so color identity can be tested with
    # Postgres' array containment operator: a card is castable in a deck when
    # its color identity is a subset of the commander's.
    #   Card.objects.filter(color_identity__contained_by=["W", "U"])
    # which compiles to `color_identity <@ ARRAY['W','U']` and uses the GIN
    # index below. JSONField cannot do this without a sequential scan.
    color_identity = ArrayField(
        models.CharField(max_length=1, choices=COLOR_CHOICES),
        default=list,
        blank=True,
    )
    colors = ArrayField(
        models.CharField(max_length=1, choices=COLOR_CHOICES),
        default=list,
        blank=True,
    )

    # Money is stored as integer cents. The optimizer sums ~99 prices per
    # candidate deck across thousands of candidate swaps; float dollars
    # accumulate rounding error and produce decks that miss the budget by a
    # few dollars for no visible reason.
    price_cents = models.IntegerField(null=True, blank=True)
    price_updated = models.DateTimeField(null=True, blank=True)

    legal_commander = models.BooleanField(default=False)
    is_banned = models.BooleanField(default=False)

    # Global EDH popularity rank from Scryfall (lower is more popular). This is
    # the Tier 0 scoring signal; it is not commander-specific.
    edhrec_rank = models.IntegerField(null=True, blank=True, db_index=True)

    is_land = models.BooleanField(default=False)
    is_basic = models.BooleanField(default=False)

    # Legendary creatures and anything whose text says it can be a commander.
    can_be_commander = models.BooleanField(default=False)

    layout = models.CharField(max_length=40, blank=True)
    image_uri = models.URLField(max_length=500, blank=True)

    # Functional role, assigned by `manage.py classify_cards`. Stored rather
    # than computed so the candidate pool can filter and group on it in SQL.
    # Kept out of sync_cards because classification rules get tuned often and
    # re-downloading the bulk file to re-tune would be wasteful.
    primary_role = models.CharField(
        max_length=20, choices=ROLE_CHOICES, default=SYNERGY, db_index=True
    )
    secondary_role = models.CharField(
        max_length=20, choices=ROLE_CHOICES, blank=True, default=""
    )
    role_source = models.CharField(
        max_length=10,
        default="",
        help_text="'heuristic' or 'override' -- where this role came from.",
    )

    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            GinIndex(fields=["color_identity"]),
            # Candidate-pool queries always filter on these two together.
            models.Index(
                fields=["legal_commander", "edhrec_rank"],
                name="card_legal_rank_idx",
            ),
            models.Index(fields=["can_be_commander"], name="card_is_commander_idx"),
            models.Index(
                fields=["primary_role", "edhrec_rank"], name="card_role_rank_idx"
            ),
        ]
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def price_dollars(self):
        """Price as a display string. None when Scryfall has no USD price."""
        if self.price_cents is None:
            return None
        return f"${self.price_cents / 100:.2f}"

    @property
    def is_priceable(self):
        """Whether this card can be bought, and so considered by the optimizer.

        A card with no USD price is unbuyable. It must not be treated as free,
        or the optimizer will happily 'afford' it.
        """
        return self.price_cents is not None


class Deck(models.Model):
    """A Commander deck, either generated or hand-built."""

    name = models.CharField(max_length=120)
    commander = models.ForeignKey(
        Card, on_delete=models.PROTECT, related_name="commanding_decks"
    )
    # Partner / Background / Friends Forever pairings.
    partner = models.ForeignKey(
        Card,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="partnering_decks",
    )

    # Whether the deck is physically built right now. This is what makes
    # cross-deck card conflicts meaningful: a card sleeved in an assembled
    # deck is not actually available for a new one.
    is_assembled = models.BooleanField(default=False)

    # Null for hand-built decks.
    budget_cents = models.IntegerField(null=True, blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)
    scoring_tier = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.commander.name})"

    @property
    def card_count(self):
        """Cards in the deck including the commander."""
        total = self.cards.aggregate(n=models.Sum("quantity"))["n"] or 0
        return total + 1 + (1 if self.partner_id else 0)

    @property
    def total_cents(self):
        """Current retail price, using today's Scryfall prices."""
        rows = self.cards.select_related("card")
        return sum((r.card.price_cents or 0) * r.quantity for r in rows)

    @property
    def generation_cents(self):
        """What the deck cost when it was generated.

        Kept separately from `total_cents` because Scryfall prices move. Without
        it there is no way to answer why a $100 deck is now a $140 deck.
        """
        rows = self.cards.all()
        return sum((r.price_at_generation or 0) * r.quantity for r in rows)


class DeckCard(models.Model):
    """One card in a deck. The join row does the real work."""

    deck = models.ForeignKey(Deck, on_delete=models.CASCADE, related_name="cards")
    card = models.ForeignKey(Card, on_delete=models.PROTECT)

    # Basic lands are the one legitimate exception to the singleton rule, so
    # quantity lives here rather than being modelled as duplicate rows. That
    # keeps the unique constraint below simple and still enforces singleton
    # for everything else.
    quantity = models.PositiveSmallIntegerField(default=1)

    # Why the optimizer chose this card. Stored so a generated deck can be
    # grouped by role and explain itself.
    role = models.CharField(max_length=20, blank=True)
    price_at_generation = models.IntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            # Singleton, enforced by Postgres rather than trusted to
            # application code.
            models.UniqueConstraint(
                fields=["deck", "card"], name="unique_card_per_deck"
            ),
            # A CheckConstraint cannot traverse a relation, so "only basics
            # may appear in multiples" cannot be expressed here -- it is
            # enforced in DeckCard.clean() and by the solver, which never
            # emits a non-basic with quantity > 1.
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1), name="quantity_at_least_one"
            ),
        ]
        ordering = ["role", "card__name"]

    def __str__(self):
        prefix = f"{self.quantity}x " if self.quantity > 1 else ""
        return f"{prefix}{self.card.name}"

    def clean(self):
        """Enforce singleton for non-basics.

        The database constraint above cannot check this, since a CheckConstraint
        may not traverse the `card` relation.
        """
        from django.core.exceptions import ValidationError

        if self.quantity > 1 and self.card_id and not self.card.is_basic:
            raise ValidationError(
                {
                    "quantity": (
                        f"{self.card.name} is not a basic land, so a deck may "
                        f"contain only one copy."
                    )
                }
            )


class CollectionItem(models.Model):
    """A card the user physically owns.

    Powers own-it-already pricing: a card in the collection costs $0 of new
    money, so a budget means "what I need to buy" rather than "retail value of
    the whole deck".
    """

    card = models.ForeignKey(
        Card, on_delete=models.PROTECT, related_name="collection_items"
    )
    quantity = models.PositiveSmallIntegerField(default=1)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["card"], name="unique_collection_card"),
        ]
        ordering = ["card__name"]

    def __str__(self):
        prefix = f"{self.quantity}x " if self.quantity > 1 else ""
        return f"{prefix}{self.card.name}"

    @property
    def assembled_decks(self):
        """Assembled decks currently using this card.

        A card owned once but already sleeved in a built deck is not really
        free -- taking it means dismantling that deck. Surfacing the conflict
        beats silently deciding for the user.
        """
        return Deck.objects.filter(
            is_assembled=True, cards__card_id=self.card_id
        ).distinct()
