# edh-budget-brewer-cli

Standalone command-line Magic: The Gathering Commander deck optimizer. A
commander and a dollar budget in, a complete legal 100-card deck out.

**This repo is a self-contained snapshot** of the engine from
`../edh-budget-brewer` (github.com/husomichael/edh-budget-brewer), which is the
hosted web version. The web layer — DRF, React, API views/serializers/urls —
is deliberately absent here.

## State

Complete and working. 97 tests passing, ruff clean, CI green. There is no
open work: this repo exists as a showcase of the optimizer itself.

If the engine changes in the web repo, those changes must be **manually
copied here**. That is the accepted cost of the snapshot approach, chosen
over a shared package so both repos stay clone-and-run.

Shared modules, in dependency order: `classification.py`, `scoring.py`,
`tier1.py`, `pool.py`, `manabase.py`, `solver.py`, `upgrades.py`,
`importers.py`, `formats.py`, `decks.py`, `models.py`, plus
`management/commands/`.

## Running it

```bash
docker compose up -d                        # Postgres
.venv/bin/python manage.py migrate
.venv/bin/python manage.py sync_catalogs
.venv/bin/python manage.py sync_cards       # ~25 MB from Scryfall
.venv/bin/python manage.py classify_cards
.venv/bin/python manage.py brew "Krenko, Mob Boss" --budget 75
.venv/bin/pytest && .venv/bin/ruff check .
```

## Gotchas

- **Postgres is required, not a preference.** The candidate pool filters by
  colour identity with the array containment operator
  (`color_identity <@ ARRAY[...]`) against a GIN index. SQLite cannot express
  it. `docker-compose.yml` is the path of least resistance.
- **Run `classify_cards` after every `sync_cards`** — a sync overwrites rows
  and leaves roles stale.
- An empty database makes every brew return `infeasible_budget`.
- **macOS python.org Python ignores the system cert store**; Scryfall
  downloads need `certifi`, already handled in `sync_cards`.
- **Scryfall's bulk format is gzipped JSONL** via `jsonl_download_uri`.
- `.env` here points at the compose Postgres on 5432. The web repo's local
  setup uses a Homebrew instance on **5433** instead — do not copy `.env`
  between the two without checking the port.
- Commander names must be unambiguous: `brew "Krenko"` matches three cards and
  exits with an ambiguity error. Use `"Krenko, Mob Boss"`.

## Conventions

- **Money is integer cents everywhere.** Format only at the edge.
- **The solver must stay deterministic.** Every sort key ends in `oracle_id`;
  there is a test for it.
- **A missing Scryfall price is NULL, never 0** — except basic lands, which
  genuinely are free and are the one exception.
- ruff for lint and format; `E501` is intentionally ignored under
  `cards/tests/` where real Oracle text is quoted one line per card.

## EDHREC

**Do not scrape EDHREC.** No API, no data export, and their Terms of Use
prohibit automated queries. `edhrec_rank` is fine — it arrives through
Scryfall's licensed bulk data.
