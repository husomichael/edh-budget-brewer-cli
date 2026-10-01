from django.contrib import admin

from cards.models import Card, CollectionItem, Deck, DeckCard


@admin.register(Card)
class CardAdmin(admin.ModelAdmin):
    """Read-only browser for synced Scryfall data.

    Cards are not editable here: `sync_cards` would silently overwrite any
    hand-edit on the next run, which is a confusing way to lose work.
    """

    list_display = (
        "name",
        "mana_cost",
        "type_line",
        "price_display",
        "edhrec_rank",
        "primary_role",
        "can_be_commander",
    )
    list_filter = (
        "primary_role",
        "legal_commander",
        "is_banned",
        "can_be_commander",
        "is_land",
        "is_basic",
        "layout",
    )
    search_fields = ("name", "type_line", "oracle_text")
    ordering = ("edhrec_rank",)
    list_per_page = 50

    readonly_fields = [f.name for f in Card._meta.fields]
    # Required so other admins can use autocomplete_fields pointing at Card.
    # view permission is enough; add/change/delete stay disabled below.

    @admin.display(description="price", ordering="price_cents")
    def price_display(self, obj):
        return obj.price_dollars or "--"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class DeckCardInline(admin.TabularInline):
    model = DeckCard
    extra = 0
    # A plain select widget would try to render 34k options and hang the page.
    autocomplete_fields = ("card",)
    fields = ("card", "quantity", "role", "price_at_generation")
    readonly_fields = ("price_at_generation",)


@admin.register(Deck)
class DeckAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "commander",
        "is_assembled",
        "card_count",
        "price_display",
        "created_at",
    )
    list_filter = ("is_assembled", "scoring_tier")
    search_fields = ("name", "commander__name")
    autocomplete_fields = ("commander", "partner")
    # Without this, the changelist fires a query per row for the commander.
    list_select_related = ("commander", "partner")
    inlines = [DeckCardInline]

    @admin.display(description="price now")
    def price_display(self, obj):
        return f"${obj.total_cents / 100:.2f}"

    def get_queryset(self, request):
        # card_count and total_cents both walk the deck's cards; prefetch so a
        # 20-deck changelist does not fire 2,000 queries.
        return super().get_queryset(request).prefetch_related("cards__card")


@admin.register(CollectionItem)
class CollectionItemAdmin(admin.ModelAdmin):
    list_display = ("card", "quantity", "in_assembled_decks", "added_at")
    search_fields = ("card__name",)
    autocomplete_fields = ("card",)
    list_select_related = ("card",)

    @admin.display(description="sleeved in")
    def in_assembled_decks(self, obj):
        names = [d.name for d in obj.assembled_decks]
        return ", ".join(names) if names else "--"
