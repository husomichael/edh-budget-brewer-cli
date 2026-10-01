# edh-budget-brewer-cli

A command-line deck optimizer for Magic: The Gathering Commander. Give it a
commander and a dollar budget; it returns a complete, legal, playable 100-card
deck.

```
$ manage.py brew "Krenko, Mob Boss" --budget 75

=== RAMP (8) ===
  Sol Ring                                       $   1.54
  Arcane Signet                                  $   0.49
  Impulsive Pilferer                             $   0.46
  ...

=== SPOT REMOVAL (10) ===
  Goblin Trashmaster                             $   6.70
  Siege-Gang Commander                           $   0.31
  Goblin Cratermaker                             $   0.27
  ...

Total: $74.57 / $75.00 budget
Cards: 100
Curve: 1:7  2:23  3:18  4:9  5:4  6+:2
Scoring: commander text (thematic): Goblin, tokens (tier 1)
```

## The problem

Budget deckbuilding is a **multi-constraint knapsack**: maximize total card
quality subject to a dollar budget *and* the structural requirements of a
deck. The constraints are what make it hard.

- Sort by quality and buy until broke, and you get a pile with no lands, no
  ramp and no interaction.
- Sort by quality-per-dollar and you get 99 one-mana cards.
- Let lands compete with spells and a single fetchland eats the budget.

So the solver builds the cheapest deck that satisfies the structure, then
spends what is left on upgrades — ranked by **score gained per dollar**, not
by the best card it can currently afford. That distinction matters more than
anything else in the project: ranking by "best affordable" put $51.36 of a $75
budget into a single card and filled the other 61 slots with three-cent draft
commons. Ranking by density, at the same budget, more than doubled the deck's
score and left nothing under ten cents.

## How it works

```
Input: commander, budget B

1. Resolve commander -> color identity, legality
2. Candidate pool = every commander-legal card whose color identity is a
   subset of the commander's, priced, scored, and tagged with a role
3. Reserve the mana base first; lands span four orders of magnitude in price
4. Fill each role's floor with the cheapest cards that qualify
5. Spend the remaining budget on the best score-per-dollar upgrades
6. Rebuild the mana base now that the spells' colour pips are known
```

### Scoring tiers

| Tier | Source | Works for | Quality |
|---|---|---|---|
| 0 | Scryfall `edhrec_rank` | any commander | legal, playable, generic |
| 1 | the commander's own oracle text | any commander | thematically coherent |

**Tier 1 is the default**, and needs no input beyond the commander's name. A
commander's rules text usually states what the deck wants, and that text is
already in the local database:

| Commander | Themes inferred | On-theme cards, tier 0 → tier 1 |
|---|---|---|
| Krenko, Mob Boss | Goblin, tokens | 4 → **52** goblins |
| Muldrotha, the Gravetide | graveyard | 6 → **61** |
| Atraxa, Praetors' Voice | counters | 8 → **62** |
| Talrand, Sky Summoner | spellslinger, tokens, Drake | 9 → **50** |

The role quotas then get filled *by on-theme cards*: a Tier 1 Krenko deck ramps
with Impulsive Pilferer, draws with Dark-Dweller Oracle, removes with
Siege-Gang Commander and recurs with Squee. All Goblins — which is how the deck
is really built.

A bare subtype on the type line is treated as flavour rather than a plan.
Muldrotha is an "Elemental Avatar" but is a graveyard deck; Atraxa is a
"Phyrexian Angel Horror" but is a counters deck; nearly every commander is also
a Human. What marks a type as a genuine tribal theme is the commander's *rules
text* naming it too.

### Role classification

Every card is assigned a functional role — ramp, draw, spot removal, sweeper,
protection, recursion, tutor, win condition — by regex over its type line and
oracle text, with a curated override table for the cards whose role is not
inferable from wording.

Heuristics agree with the hand-curated table on **53 of 57** cards. The four
exceptions are genuinely invisible in card text: Craterhoof Behemoth and
Triumph of the Hordes win games by effect rather than wording, Animate Dead
never names a graveyard in its own returning clause, and Fact or Fiction is
idiosyncratic.

Getting there meant finding what the patterns actually missed:

- Land fetchers name basic land *types*, not the word "land" — Nature's Lore
  says "Search your library for a **Forest** card", which read as a tutor.
- Reanimation targets "**a** graveyard", not "your graveyard".
- Overload's mass mode exists only by rule: Cyclonic Rift never says "Return
  all", so the keyword itself has to imply a sweeper.
- Counterspells name card types — "Counter target enchantment, instant, or
  sorcery spell" does not match "counter target spell".

## Install

Requires **Python 3.13** and **PostgreSQL 16**.

```bash
git clone https://github.com/husomichael/edh-budget-brewer-cli
cd edh-budget-brewer-cli

docker compose up -d                  # or point DATABASE_URL at your own Postgres

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env                  # then set SECRET_KEY
.venv/bin/python manage.py migrate
.venv/bin/python manage.py sync_catalogs
.venv/bin/python manage.py sync_cards       # ~25 MB from Scryfall
.venv/bin/python manage.py classify_cards
```

`sync_cards` loads about 34,500 cards in under ten seconds and is idempotent.

**Postgres is required, not a preference.** The candidate pool filters by
colour identity using the array containment operator
(`color_identity <@ ARRAY[...]`) against a GIN index — the hottest query in the
project, and one SQLite cannot express.

## Usage

```bash
manage.py brew "Krenko, Mob Boss" --budget 75
manage.py brew "Muldrotha" --budget 200 --format text
manage.py brew "Atraxa, Praetors' Voice" --budget 150 --format json
manage.py brew "Krenko, Mob Boss" --budget 50 --upgrade-path
manage.py brew "Edgar Markov" --budget 100 --owned-free
manage.py brew "Talrand" --budget 60 --save --name "Budget Drakes"
```

| Flag | Effect |
|---|---|
| `--budget` | Dollar target (required) |
| `--strategy` | `auto` (tier 1, default), `tier0`, `tier1` |
| `--upgrade-path` | Show what the next increments of budget would buy |
| `--owned-free` | Price cards in your collection at $0 |
| `--save` / `--name` | Persist the deck |
| `--lands` | Target land count (default 36) |
| `--format` | `table`, `text`, `json` |

Partial commander names work when unambiguous. `--format text` emits
`1 Card Name` per line, which pastes straight into Moxfield or Archidekt.

Solving takes 25–500ms depending on colour count, and is **deterministic** —
identical input always produces an identical deck. An infeasible budget exits
with the real minimum rather than emitting a broken deck.

### The upgrade path

`--upgrade-path` solves at several budgets and diffs the results into an
ordered purchase sequence, reporting **dollars per point of score gained**:

```
Best next buys, cheapest first:
  $   0.49  Hordeling Outburst
  $   2.86  Goblin Spymaster
  $   4.21  Brash Taunter

+$25  -> $74.57 spent, score 33.9 (+0.9)     $22.93 per point
+$50  -> $99.98 spent, score 34.1 (+0.2)    $121.36 per point
+$250 -> $297.54 spent, score 34.9 (+0.1)  $1269.30 per point
```

Deck selection is **not monotonic** — a tier with more money can differ by more
than one card, since extra budget in one role enables a cheaper reshuffle in
another. Each tier therefore reports a set of changes, not a single-card chain.

**A caveat on those numbers.** Score derives from `edhrec_rank`, which is
log-normalized and so compresses differences between the top cards. The solver
rates a $50 deck as roughly 94% as good as an unlimited one, which overstates
how close they are: it cannot see that an expensive staple is *qualitatively*
different from a cheap card of similar popularity, only that their ranks are
near each other. Trust the path most under about $100.

## Expectations

Generated decks land around 80% of the way there. The optimizer cannot read
your commander and infer that your particular build wants sacrifice outlets
over +1/+1 counters. Treat the output as a strong first draft, not a finished
deck.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

## Data sources and attribution

Card data, legalities and prices come from [**Scryfall**](https://scryfall.com)
via their public bulk data endpoints. Scryfall is not affiliated with this
project. Prices are sourced from TCGplayer and Cardmarket, updated daily, and
are indicative only.

`edhrec_rank` reaches this project through Scryfall's bulk data. **This project
does not scrape EDHREC** — they publish no API and no data export, and their
Terms of Use prohibit automated queries, so there is no sanctioned programmatic
path to their data. The optimizer is built entirely on data this project is
plainly licensed to use.

This is a personal, non-commercial tool. It is not affiliated with or endorsed
by Scryfall, EDHREC, Space Cow Media, or Wizards of the Coast.

## Related

A hosted web version lives at
[edh-budget-brewer](https://github.com/husomichael/edh-budget-brewer).

## Legal

Unofficial Fan Content permitted under the Wizards of the Coast Fan Content
Policy. Not approved or endorsed by Wizards. Portions of the materials used are
property of Wizards of the Coast LLC. Magic: The Gathering is a trademark of
Wizards of the Coast.
