# AGENTS.md

Guidance for coding agents working on this repository. Read [DESIGN.md](DESIGN.md) before changing behaviour: it records every decision (D-numbers) and why. The full spec is `docs/superpowers/specs/2026-11-22-kraken-plugin-design.md`.

## What this is

A [Securo](https://github.com/securo-finance/securo) provider plugin that syncs one Kraken spot account per API key, **read-only**. Securo calls `securo_kraken.plugin:plugin.sync(params, context)` in its plugin runner. `sync` returns `{"state": ..., "data": ...}`, where `data` is a provider-contract-1 payload (accounts with transactions, holdings with trades) or `None`. Securo keeps `state` only after it has written `data`, so a failed sync repeats the same work.

## Commands

```bash
python -m pytest -q                 # whole suite, run from the repo root
python -m pytest tests/test_book.py -q
python tools/make_icons.py          # regenerates icon.png and logos/kraken.png
python tools/kraken_probe.py        # read-only ledger-shape probe (needs KRAKEN_API_KEY / KRAKEN_API_SECRET)
```

There is nothing to install for the plugin itself. pytest is the only development dependency.

## Layout

| Module | Responsibility |
|---|---|
| `client.py` | The only code that does HTTP: signing, nonce, private POST and public GET calls, 40 s budget (ledger walk stops at 30 s), estimated rate-limit counter, error mapping |
| `assets.py` | Coin family codes, fiat list, valuation currencies, pair discovery and price routes (pure) |
| `ledger.py` | Resumable walk over `Ledgers`: backfill newest to oldest, then incremental with an overlap and seen ids |
| `classify.py` | Pure: ledger rows to fiat cash transactions and coin moves (the spec's rules 1-7) |
| `prices.py` | Current prices (`Ticker`) and historical prices (`Trades`) with a cache; best effort |
| `book.py` | Per-coin units and cost; moves to Securo trades, priced by replaying in Securo's order |
| `balancing.py` | Opening and adjustment trades so trades add up to the reported quantity |
| `payload.py` | Contract-1 dicts: accounts, transactions, holdings, trades diff |
| `plugin.py` | `sync()` orchestration and state |
| `errors.py`, `numbers.py`, `log.py` | Exceptions, decimal formatting, stderr logging |

Tests mirror the modules. `tests/factories.py` holds the fake HTTP and builders for made-up Kraken JSON; `tests/securo_replay.py` replays trades the way Securo does. Never call the real API from tests.

## Hard rules

- **Standard library only** in `securo_kraken/`. No lockfile, no third-party imports. Code must run on Python 3.13 locally and on 3.14 in Securo's runner.
- **Read-only.** Only host `api.kraken.com` (the manifest's `network`). Private calls are only `POST BalanceEx` and `POST Ledgers`; public calls only `GET Assets`, `AssetPairs`, `Ticker` and `Trades`. Never add a write endpoint, another permission or `api_scopes`.
- **External-id formats are frozen**: account `kraken:<CCY>`; cash transaction `kraken:<ledger id>` and its fee `kraken:<ledger id>:fee`; holding `kraken:<COIN>`; trade `kraken:<ledger id>`, `kraken:reward:<COIN>:<YYYY-MM>`, `kraken:opening:<COIN>`, `kraken:adjust:<COIN>`. Securo matches records by them, so changing one duplicates data for every user.
- **No secrets or personal data in output.** The key, the private key, signatures, URLs with query strings, response bodies, amounts, ids, coins and dates must never appear in exception messages or logs. Logs carry fixed text or sanitised type names only (`[A-Z0-9_]`, at most 40 characters).
- **Amounts leave as strings** from `numbers.number()` (at most 8 decimals, no exponent), never floats. Trade quantity and price are rounded half-up to 6 decimals before they are sent and before any replay; fees to 2.
- **Never send `holdings: []`** and never drop a coin that was listed before: Securo archives holdings that stop being listed. Leaving the `holdings` key out means "not fetched, leave alone"; `trades: None` on a holding leaves its stored trades alone.
- **Trades must replay in Securo's order and reconcile.** Securo keeps a holding's trades only if replaying them, sorted by (date, buys first, external_id), never dips below zero and ends on the reported quantity. Any change to `book.py`, `balancing.py` or `payload.py` needs a check in `tests/securo_replay.py` (multi-sync, including rows that arrive late).
- `state` and `data` must stay JSON-serialisable (no `Decimal`, `date` or sets).
- All fixtures are made up: no real ledger ids, balances or amounts.

## Securo's side

The contract Securo validates against is `backend/app/plugins/contract.py` in [securo-finance/securo](https://github.com/securo-finance/securo). How it merges trades is in `backend/app/services/connection_service.py` (`_reconciled_trades`, `_sync_holding_trades`), and holding ids are prefixed per connection in `backend/app/plugins/provider.py`. Check those before relying on an assumption about Securo's behaviour.

## Releasing

1. Bump `version` in both `securo-plugin.json` and `pyproject.toml`.
2. Add a decision to DESIGN.md for any behaviour change.
3. Tag `vX.Y.Z` on `main` and publish a GitHub release. Securo's catalog pins each plugin to its latest release and rebuilds about every 6 hours.

The manifest `id` (`nunomluz/securo-kraken`) is the plugin's identity in the catalog; don't change it.
