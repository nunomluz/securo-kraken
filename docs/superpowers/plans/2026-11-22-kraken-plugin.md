# Kraken Securo Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Securo provider plugin that syncs one Kraken spot account per API key, read-only: fiat balances as accounts with cash activity, every coin (Earn included) as a holding with value, cost basis and trades.

**Architecture:** `sync(params, context)` reads `BalanceEx` every call, walks the whole ledger (backfill, then incremental) within a time budget, classifies ledger rows into cash transactions and coin moves kept compactly in `state`, prices them from Kraken's public prices, and **recomputes every coin's trade list from scratch each sync**, sending only trades that are new or changed. Sells "at average cost" are priced by replaying the plugin's own trades in exactly Securo's order, so Securo computes no gain on them.

**Tech Stack:** Python standard library only (runtime 3.14, also runs on local 3.13), pytest for tests.

**Spec:** `docs/superpowers/specs/2026-11-22-kraken-plugin-design.md` — read it before starting any task. Reference implementation for style and patterns: https://github.com/nunomluz/securo-trading212 (same author, same rules).

## Global Constraints

- Standard library only in `securo_kraken/`; no lockfile. Python 3.13 and 3.14.
- Manifest `id` `nunomluz/securo-kraken`; entry point `securo_kraken.plugin:plugin`; `min_securo_version` `0.20.0`; version `0.1.0`.
- Only host: `api.kraken.com`. Private: `POST /0/private/<Method>`; public: `GET /0/public/<Method>?…`. Only private methods ever called: `BalanceEx`, `Ledgers`. Public: `Assets`, `AssetPairs`, `Ticker`, `Trades`.
- Signing: `API-Sign = base64(HMAC-SHA512(base64decode(secret), path + SHA256(nonce + body)))`, body form-encoded with `nonce` first. Nonce = `max(int(wall_ms), last + 1)`.
- Time budget **40 s** (`BUDGET_SECONDS = 40`); the ledger walk stops at **30 s** used (`LEDGER_BUDGET = 30`) so prices still fit.
- Private call cost: `Ledgers` 2, others 1. Proactive pacing with the starter tier: counter max **15**, decay **0.33/s**. Public calls at most **1 per second**.
- Amounts leave as strings from `numbers.number()` (≤ 8 decimals, no exponent, never floats). Trade quantity and price are rounded half-up to **6** decimals before they are sent *and* before any replay.
- Reply caps: `MAX_TRADES = 20_000`, `MAX_TRANSACTIONS = 20_000`.
- External ids (frozen once released):
  - account `kraken:<CCY>`; cash transaction `kraken:<ledger id>`, its fee `kraken:<ledger id>:fee`
  - holding `kraken:<COIN>` (family code, e.g. `kraken:BTC`)
  - trade `kraken:<ledger id>` (the coin's own leg); reward month `kraken:reward:<COIN>:<YYYY-MM>`; `kraken:opening:<COIN>`; `kraken:adjust:<COIN>`
- Valuation currencies allowed: `EUR, USD, GBP, CAD, CHF, AUD, JPY`; default `EUR`. Fiat families: `USD, EUR, GBP, CAD, CHF, AUD, JPY, AED, KRW`.
- Never put the key, secret, signatures, URLs with query strings, response bodies, amounts, ids, coins or dates in exception messages or logs. Logs carry fixed text or sanitised type names (`[A-Z0-9_]`, ≤ 40 chars).
- Fixtures are made up. No real ledger ids, balances or amounts.
- Securo semantics: `holdings` left out = not fetched; never send `holdings: []` unless backfill is complete and there are truly no coins ever seen. `trades: null` leaves a holding's ledger alone. Every coin ever seen is listed every sync (quantity 0 when gone).
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- **Same-day sell before a buy** (by time) of one coin: Securo replays a day's buys first, then by `external_id`. Expected: average-cost sells priced from that order, so Securo's replay shows 0 realized gain on them and never goes below zero → test in Task 7 using `tests/securo_replay.py`.
- **Earn moves whose two rows have different refids or land on different pages** (`spottostaking` on `XETH`, `stakingfromspot` on `ETH2.S`): expected no trades and no cash transactions at all → test in Task 5.
- **A trade's second leg arrives on the next sync** (only one leg read, at the newest edge): expected nothing sent for that refid now, one proper buy/sell next sync, never a lone-leg market buy plus a later swap → test in Task 9.
- **Budget or rate limit hit mid-backfill:** expected success, cash collected so far sent, `holdings` left out, position kept; the next sync resumes without missing or repeating rows → tests in Tasks 4 and 9.
- **Coin fully sold or withdrawn:** expected a quantity-0 holding listed on this and every later sync, with its trades reconciling to 0 → test in Task 8.

---

## File Structure

```
.gitignore  pyproject.toml  securo-plugin.json  README.md  DESIGN.md
AGENTS.md  CLAUDE.md  LICENSE  icon.png  logos/kraken.png
tools/make_icons.py      # stdlib PNG generator (original mark)
tools/kraken_probe.py    # already exists: read-only ledger-shape probe for bug reports
securo_kraken/
  __init__.py
  errors.py     # NeedsAuth, Transient, RateLimited, OutOfTime
  numbers.py    # parse_decimal, number, q6
  log.py        # reset, unknown, warning
  client.py     # signing, nonce, private/public calls, pacing, budget, error mapping
  assets.py     # family codes, fiat, pair index, price routes
  ledger.py     # ledger walking: backfill / incremental, seen-id window
  classify.py   # rows -> Cash / Move (pure)
  prices.py     # current prices (Ticker), historical (Trades) with cache
  book.py       # moves -> per-coin trades, price resolution in Securo order
  balancing.py  # opening / adjust trades
  payload.py    # contract-1 accounts and holdings, trade diff and budget
  plugin.py     # sync(): orchestration and state
tests/
  __init__.py  factories.py  securo_replay.py
  test_numbers.py test_client.py test_assets.py test_ledger.py test_classify.py
  test_prices.py test_book.py test_balancing.py test_payload.py test_plugin.py test_manifest.py
```

Run tests from the repo root: `python -m pytest -q`.

---

### Task 1: Repo, scaffold, errors, numbers, log

**Files:**
- Create: `.gitignore`, `pyproject.toml`, `securo_kraken/__init__.py`, `securo_kraken/errors.py`, `securo_kraken/numbers.py`, `securo_kraken/log.py`, `tests/__init__.py`, `tests/test_numbers.py`

**Interfaces:**
- Produces:
  - `errors.NeedsAuth(message: str, *, code: str = "credentials_invalid")` — message gets ` Help: https://support.kraken.com/articles/360000919966-how-to-create-an-api-key` appended; `errors.Transient`, `errors.RateLimited`, `errors.OutOfTime` (last two never escape `sync`).
  - `numbers.parse_decimal(value: object) -> Decimal | None`, `numbers.number(value: Decimal) -> str` (copy both from `securo_trading212/numbers.py`), `numbers.q6(value: Decimal) -> Decimal` (round half-up to 6 decimals).
  - `log.reset()`, `log.unknown(kind: str, name: object)`, `log.warning(message: str)` — copy `securo_trading212/log.py`, prefix `kraken:`.

- [ ] **Step 1:** `git init -b main` in the repository root. `.gitignore` and `pyproject.toml` as in Trading 212 (project name `securo-kraken`), plus `probe*.txt` in `.gitignore`.
- [ ] **Step 2: Write the failing tests** in `tests/test_numbers.py`:

```python
def test_number_formats_without_exponent():
    assert number(Decimal("1E-8")) == "0.00000001"
    assert number(Decimal("12.3400")) == "12.34"
    assert number(Decimal("0.000000004")) == "0"

def test_q6_rounds_half_up():
    assert q6(Decimal("0.0000005")) == Decimal("0.000001")
    assert q6(Decimal("1.23456749")) == Decimal("1.234567")

def test_parse_decimal_rejects_floats_bools_and_nan():
    assert parse_decimal("1.5") == Decimal("1.5")
    assert parse_decimal(True) is None and parse_decimal("NaN") is None and parse_decimal(1.5) is None
```

- [ ] **Step 3:** Run `python -m pytest tests/test_numbers.py -q` → FAIL (module missing).
- [ ] **Step 4:** Implement the three modules.
- [ ] **Step 5:** Run → PASS.
- [ ] **Step 6:** Commit with the spec, the probe and this plan: `git add -A && git commit -m "chore: scaffold, errors, numbers, log"`.

---

### Task 2: Client (signing, pacing, errors)

**Files:**
- Create: `securo_kraken/client.py`, `tests/factories.py` (FakeHttp part), `tests/test_client.py`

**Interfaces:**
- Consumes: Task 1 errors.
- Produces:
  - `Http = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]` — `(method, url, headers, body, timeout) -> (status, body)`.
  - `urllib_http` (real transport, no redirects, response capped at 32 MB, `OSError`/`http.client.HTTPException` → `Transient("Couldn't reach Kraken (<ExcName>).")`).
  - `sign(secret_b64: str, path: str, nonce: str, body: str) -> str`.
  - `Client(api_key: str, api_secret: str, *, http: Http = urllib_http, clock=time.monotonic, wall=time.time, sleep=time.sleep, budget: float = BUDGET_SECONDS)`; `Client.private(method: str, params: dict[str, str] | None = None) -> dict`; `Client.public(method: str, params: dict[str, str] | None = None) -> dict`; `Client.remaining() -> float`; `Client.elapsed() -> float`.
  - `PERMISSIONS = {"BalanceEx": "Funds: Query", "Ledgers": "Data: Query ledger entries"}`.
  - `HOST = "api.kraken.com"`, `BUDGET_SECONDS = 40`, `LEDGER_BUDGET = 30`.
  - `tests/factories.FakeHttp(routes: dict[str, list[reply]])` keyed by URL path (`/0/private/Ledgers`, `/0/public/Trades`), replies pop in order and the last repeats; records `calls` as `(method, url, headers, body)`.

- [ ] **Step 1: Write the failing tests** in `tests/test_client.py`:
  - `test_sign_matches_kraken_docs_vector`: `sign("kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg==", "/0/private/AddOrder", "1616492376594", "nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25") == "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="`.
  - `test_private_posts_signed_form_with_nonce_first`: body starts with `nonce=`, headers carry `API-Key` and `API-Sign`, method `POST`, URL `https://api.kraken.com/0/private/BalanceEx`.
  - `test_nonce_strictly_increases_with_frozen_clock`: two calls with the same `wall()` give nonces `n` and `n+1`.
  - `test_public_is_get_without_key_headers`.
  - `test_invalid_key_or_signature_is_needs_auth` (`{"error":["EAPI:Invalid key"]}` and `["EAPI:Invalid signature"]`).
  - `test_permission_denied_names_permission`: `["EGeneral:Permission denied"]` on `Ledgers` → `NeedsAuth` whose message contains `Data: Query ledger entries`, `code == "permission_missing"`.
  - `test_otp_error_is_needs_auth`: any error containing `otp` (case-insensitive).
  - `test_invalid_nonce_is_transient_with_dedicated_key_hint`: message contains `used only by Securo`.
  - `test_rate_limit_error_is_rate_limited`; `test_eservice_and_lockout_and_5xx_are_transient`.
  - `test_pacing_sleeps_before_exceeding_starter_counter`: with a fake clock, 8 `Ledgers` calls in a row → total cost 16 > 15, so the client sleeps ≥ 3.0 s (one unit at 0.33/s) before the 8th; if the wait would exceed `remaining() - 2`, raises `RateLimited` instead.
  - `test_public_calls_spaced_one_second`.
  - `test_budget_exhausted_raises_out_of_time`; `test_request_timeout_capped_by_remaining_budget`.
  - `test_messages_never_contain_secrets`: for every error path, `str(exc)` contains neither the key, the secret, `API-Sign` value nor `?`.
  - `test_empty_or_non_base64_secret_is_needs_auth`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Error mapping on the `error` list (first matching rule wins): `Invalid key|Invalid signature` → NeedsAuth("Kraken rejected the API key or private key."); `Permission denied` → NeedsAuth naming `PERMISSIONS[method]`; contains `otp` → NeedsAuth("This API key asks for a 2FA code. Create a key without 2FA for Securo."); `Rate limit exceeded` → RateLimited; `Invalid nonce` → Transient("Kraken refused the request order (nonce). Use an API key used only by Securo."); `EService:`/`Temporary lockout` → Transient; anything else → Transient("Kraken returned an error (<sanitised category>).") with category = text before the second `:` reduced to `[A-Za-z:]`, ≤ 40 chars. HTTP status ≠ 200 → Transient("Kraken answered HTTP <n>."). Parse JSON with `parse_float=Decimal`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: signed read-only Kraken client with pacing`.

---

### Task 3: Asset families and price routes

**Files:**
- Create: `securo_kraken/assets.py`, `tests/test_assets.py`

**Interfaces:**
- Produces:
  - `FIAT: frozenset[str]`, `VALUATIONS: tuple[str, ...]`, `ALIASES: dict[str, str]` (`XBT→BTC, XDG→DOGE, SETH→ETH, ETH2→ETH, FLOWH→FLOW`).
  - `family(code: str, altnames: dict[str, str]) -> str | None` — `None` for `KFEE`.
  - `is_fiat(family: str) -> bool`.
  - `altnames_from(assets_result: dict) -> dict[str, str]` (Kraken code → altname).
  - `index_pairs(asset_pairs_result: dict, altnames: dict[str, str]) -> dict[str, str]` mapping `"BASE/QUOTE"` (families) → Kraken pair key; keeps every pair that has a `base` and a `quote`, whatever its `status` (a paused pair still has a last price).
  - `route(coin: str, valuation: str, pairs: dict[str, str]) -> list[tuple[str, bool]] | None` — legs `(pair_key, invert)`; tries direct, inverted direct, then via `USD`, then via `BTC`. `route(v, v, …) == []`.

- [ ] **Step 1: Write the failing tests:**
  - `family` cases: `XXBT→BTC`, `XBT.M→BTC`, `ZEUR→EUR`, `EUR.HOLD→EUR`, `EUR.M→EUR`, `ETH2.S→ETH`, `ETH2→ETH`, `DOT28.S→DOT`, `USDT.F→USDT`, `ETH.B→ETH`, `FLOWH.S→FLOW`, `LUNA2→LUNA2` (digits kept without a staking suffix), `KFEE→None`, `AAPLx.T→AAPLx.T` with `log.unknown("asset_suffix","T")` called once.
  - `is_fiat("EUR")`, not `is_fiat("USDT")`.
  - `route("BTC","EUR",{"BTC/EUR":"XXBTZEUR"}) == [("XXBTZEUR", False)]`; inverted `route("USD","EUR",{"EUR/USD":"ZEURZUSD"}) == [("ZEURZUSD", True)]`; via USD `route("FOO","EUR",{"FOO/USD":"FOOUSD","EUR/USD":"ZEURZUSD"}) == [("FOOUSD",False),("ZEURZUSD",True)]`; no route → `None`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement (suffix strip, then trailing digits only when a staking suffix was stripped and the stem isn't `ETH2`; `.HOLD` strip; altname; alias). **Step 4:** PASS. **Step 5:** Commit `feat: Kraken asset families and price routes`.

---

### Task 4: Ledger walking

**Files:**
- Create: `securo_kraken/ledger.py`, `tests/test_ledger.py`; extend `tests/factories.py` with `ledger_row(id, refid, time, type, subtype="", asset="ZEUR", amount="0", fee="0", aclass="currency") -> tuple[str, dict]` and `ledger_page(rows) -> dict`.

**Interfaces:**
- Consumes: `Client.private`, `Client.elapsed`, `RateLimited`, `OutOfTime`.
- Produces:
  - `OVERLAP = 3600` seconds.
  - `advance(client, lstate: dict, *, stop_after: float = LEDGER_BUDGET) -> tuple[list[dict], bool]` → (new rows, each the API dict plus `"id"`; `caught_up`). Rows are returned in any order; never one already returned on an earlier sync.
  - `lstate` keys (JSON-safe): `phase` (`"backfill"`/`"incremental"`), `cursor` (float `end` time to resume from, or `None`), `floor` (float time the current walk stops at, `None` in backfill), `newest` (float, newest time seen), `seen` (list of ids whose time ≥ `newest - 2*OVERLAP` or within 1 s of `cursor`).

Walk: each call `Ledgers` with `without_count=true` and, when resuming, `end=<cursor>` (inclusive timestamp). A page's unseen rows are collected; the new cursor is the page's oldest time. Backfill ends when a page has no unseen rows → `phase = "incremental"`. An incremental walk starts from the top (no `end`) with `floor = newest - OVERLAP` and ends when a page's oldest time ≤ `floor` or it has no unseen rows. `RateLimited`/`OutOfTime`/`elapsed() ≥ stop_after` stop the walk with `cursor` kept (`caught_up = False`); the next call resumes from `cursor` with the same `floor`, and only after finishing it starts a fresh top walk.

- [ ] **Step 1: Write the failing tests:**
  - `test_backfill_walks_pages_until_nothing_new` (3 pages of 50, 50, 7 rows; third call has `end` = oldest time of page 2; returns 107 rows, `caught_up`, phase incremental).
  - `test_backfill_resumes_after_rate_limit_without_gaps_or_repeats` (RateLimited on call 2; second `advance` resumes with `end`; union equals all rows, no duplicates).
  - `test_rows_sharing_the_boundary_timestamp_are_not_lost_or_doubled`.
  - `test_incremental_reads_until_overlap_floor`.
  - `test_incremental_interrupted_then_new_rows_arrive` (interrupted walk is finished first, then a new top walk; nothing lost).
  - `test_seen_is_pruned_to_window` (a 10,000-row backfill leaves `len(lstate["seen"])` small).
  - `test_params_never_include_ofs`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat: walk the Kraken ledger with resumable backfill`.

---

### Task 5: Classification (rows → cash and moves)

**Files:**
- Create: `securo_kraken/classify.py`, `tests/test_classify.py`

**Interfaces:**
- Consumes: `assets.family`, `assets.is_fiat`, `numbers.parse_decimal`, `log`.
- Produces:
  - `@dataclass(frozen=True) Cash: external_id: str; currency: str; date: date; amount: Decimal; kind: Literal["credit","debit"]; description: str` (amount > 0).
  - `@dataclass(frozen=True) Move: id: str; coin: str; time: float; kind: Literal["buy","sell"]; quantity: Decimal; pricing: Literal["fixed","zero","market","avg","carry"]; fiat: str | None = None; fiat_amount: Decimal | None = None; fiat_fee: Decimal = 0; group: str | None = None; reward: bool = False; weight_market: bool = False`.
    - `fixed`: `fiat_amount` = gross fiat paid (buy, excl. fee) or received (sell); `fiat_fee` separate.
    - `carry`: buy whose cost = value of the `avg` sells sharing `group` (the refid); `weight_market=True` when the group has several carry buys (split by market value).
  - `classify(rows: list[dict], altnames: dict[str, str], *, edge: float) -> tuple[list[Cash], list[Move], list[dict]]` — third item = rows held back (incomplete refid groups with time ≥ `edge`); `edge = newest - 3600`.
  - `compact(move) -> list`, `expand(item: list) -> Move` for state.

Rules exactly as the spec's "Classification" (rules 1–7) and Accounts section. Specifics the tests pin:
  - Leg units = `amount - fee` (fee in the row's asset). Fiat paid on a buy = `-(amount) + fee` total, `fiat_fee = fee`, `fiat_amount = total - fee`. Sell: `fiat_amount = amount` of the fiat leg, `fiat_fee = fee`, quantity = `-(amount) + fee` of the crypto leg.
  - Trade groups (types `trade, spend, receive, conversion, sale, adjustment` sharing a refid) are netted per family. One fiat out + crypto ins → `fixed` buys (several crypto ins: `fixed` split by market value — mark `weight_market=True`, `fiat_amount` = whole amount; book splits). Crypto outs + one fiat in → `fixed` sells (split likewise). Crypto outs + crypto ins → `avg` sells and `carry` buys with `group=refid`. Fiat-only → cash transactions on each account, description `Conversion <A> → <B>`. Anything else → crypto outs `avg`, crypto ins `market`, fiat legs as cash transactions, `log.warning("unusual trade group")`.
  - Move ids: the coin leg's ledger id. When one family has several legs in a group, the id of its leg with the largest |amount|.
  - Rewards (`zero`, `reward=True`): positive `staking`, `earn`/`reward`, `reward`, `dividend`, `invite bonus`, `transfer` with empty subtype.
  - Internal (nothing): `transfer` subtypes `spottostaking, stakingfromspot, stakingtospot, spotfromstaking`; `earn` with subtype ≠ `reward` (unknown subtype also logged); negative `staking`; mirrored pairs. A non-zero `fee` on an internal crypto row → `avg` sell of `fee` units; on an internal fiat row → fee cash debit.
  - Fiat cash: `deposit`/`withdrawal` → "Deposit"/"Withdrawal"; fiat rewards → credit "Kraken rewards"; rule-5 types by sign with description = sanitised type; every fiat `fee > 0` outside a crypto trade → debit `kraken:<id>:fee` "Kraken fee". Fiat legs of crypto trades → no cash.
  - `KFEE` rows ignored.
  - Dates are UTC dates of `time`.

- [ ] **Step 1: Write the failing tests** (one per behaviour, made-up rows from `factories.ledger_row`):
  - `test_fiat_buy_is_fixed_buy_with_fee` (ZEUR −100.0 fee 0.26, XXBT +0.002 → one Move buy qty 0.002, fiat EUR, fiat_amount 100.0, fiat_fee 0.26; no Cash).
  - `test_fiat_sell_is_fixed_sell`.
  - `test_fee_on_receiving_leg_reduces_quantity`.
  - `test_swap_gives_avg_sell_and_carry_buy_sharing_group`.
  - `test_card_purchase_deposit_spend_receive` (deposit is Cash credit; spend/receive is a buy).
  - `test_dust_sweep_several_outs_one_in`.
  - `test_earn_rows_with_different_refids_are_internal` (Review Focus): `transfer/spottostaking XETH −1` refid A and `transfer/stakingfromspot ETH2.S +1` refid B → no Cash, no Move.
  - `test_earn_allocation_and_migration_internal_and_unknown_subtype_logged`.
  - `test_negative_staking_is_internal`.
  - `test_rewards_are_zero_cost` for each reward kind incl. `transfer` with empty subtype (airdrop).
  - `test_crypto_deposit_market_and_withdrawal_avg_with_fee` (withdrawal qty = amount + fee).
  - `test_futures_transfer_in_market_out_avg`.
  - `test_fiat_deposit_withdrawal_fee_and_rewards_cash`.
  - `test_fiat_to_fiat_conversion_two_cash_transactions`.
  - `test_lone_leg_at_edge_held_back_and_final_when_older` (time ≥ edge → in third return; older → lone receive `market` buy / lone spend `avg` sell, logged).
  - `test_kfee_ignored`; `test_unknown_type_by_sign_and_logged_by_name_only` (capture stderr; no digits from amounts appear).
  - `test_compact_roundtrip`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat: classify Kraken ledger rows`.

---

### Task 6: Prices

**Files:**
- Create: `securo_kraken/prices.py`, `tests/test_prices.py`

**Interfaces:**
- Consumes: `Client.public`, `assets.route`, `RateLimited`, `OutOfTime`.
- Produces:
  - `current(client, coins: list[str], valuation: str, pairs: dict[str, str]) -> dict[str, Decimal]` — one `Ticker` call with every leg pair (`pair=` comma list), last price `c[0]`; coins with no route or no answer are left out (`log.warning`). Tokenized codes (`.T` kept by `family`) use a second `Ticker` call with `asset_class=tokenized_asset`.
  - `class History(cache: dict)`: `get(family: str, time: float, valuation: str, pairs) -> Decimal | None` (valuation-ccy price, from cache, or `None` and the need recorded in `self.missing`); `fetch(client, valuation, pairs, *, deadline: float)` fills `missing` with public `Trades?pair=<p>&since=<int(time)>&count=1`, price = first trade's `[0]`; cache key `"<pair>:<hour>"` with `hour = int(time // 3600)`, value a string. A pair whose first trade is > 7 days after `time` still gives its price (`log.warning("price taken after the event")`); no trades at all → cached as `"none"` and `get` returns the current price passed in via `History.fallback: dict[str, Decimal]`.
  - `History.cache` is JSON-safe and lives in `state["prices"]`.

- [ ] **Step 1: Write the failing tests:** `test_current_direct_inverted_and_via_usd`; `test_current_skips_coin_without_route`; `test_history_get_records_missing_then_fetch_fills_cache`; `test_history_multiplies_route_legs`; `test_history_same_hour_reuses_cache`; `test_history_stops_on_deadline_keeping_missing`; `test_history_no_trades_falls_back_to_current`; `test_trades_query_uses_since_seconds_and_count_1`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat: current and historical Kraken prices`.

---

### Task 7: Book (moves → trades, priced in Securo's order)

**Files:**
- Create: `securo_kraken/book.py`, `tests/securo_replay.py`, `tests/test_book.py`

**Interfaces:**
- Consumes: `classify.Move`, `prices.History`, `numbers.q6`.
- Produces:
  - `@dataclass Trade: id: str; coin: str; date: date; kind: Literal["buy","sell"]; quantity: Decimal; price: Decimal | None; fee: Decimal; pricing: str; group: str | None; time: float`.
  - `trades_from_moves(moves: list[Move]) -> dict[str, list[Trade]]` — reward moves merged per coin and UTC month into `kraken:reward:<COIN>:<YYYY-MM>`, dated the month's **earliest** reward date, quantity summed, price 0.
  - `resolve(trades_by_coin: dict[str, list[Trade]], valuation: str, history: History, pairs) -> set[str]` — fills every `price`/`fee` in place; returns coins it couldn't price yet (missing history). Prices and quantities are `q6`'d.
  - `average_costs(trades: list[Trade]) -> tuple[Decimal, Decimal]` (units, cost) by Securo's replay.
  - `MICRO = Decimal("0.01")`; `value_only(trades: list[Trade], current_price: Decimal | None) -> bool`.
  - `tests/securo_replay.py`: `replay(trades) -> dict(units, cost, realized, low)` copying `_recompute` (order `(date, kind != "buy", external_id)`, `cost += q*p + fee` on buys; on sells `avg = cost/qty`, `realized += (p-avg)*q - fee`, `cost -= avg*q`) and `reconciles(trades, quantity) -> bool` (never below zero, ends within `max(0.0001, 1e-6 × largest lot)`).

Resolution algorithm (the one the tests don't fully determine):

```
order all trades of all coins by (date, kind != "buy", id)
for each date D in order:
    repeat until no price changes (max 20 rounds; then log.warning and use the last values):
        for each buy on D: price from its pricing
            fixed  -> (fiat_amount converted to valuation at its time) / qty ; fee = fiat_fee converted
                      (weight_market: share of fiat_amount by market value of each coin at that time)
            zero   -> 0
            market -> history.get(coin, time, valuation, pairs)
            carry  -> sum(q * price of the group's avg sells) / qty   (weight_market: split by market value)
        per coin, after all its buys on D: avg = cost / units (Securo replay, q6 values)
        for each sell on D: avg -> price = q6(avg of its coin) ; fixed -> fiat converted / qty
    advance each coin's (units, cost) through D's buys then sells
```

- [ ] **Step 1: Write the failing tests** (assert with `securo_replay`):
  - `test_fiat_buy_then_avg_withdrawal_realizes_zero`.
  - `test_same_day_sell_before_buy_priced_in_securo_order` (Review Focus): sell at 10:00 and buy at 15:00 same day, a prior buy the day before → replay `realized == 0` for the avg sell and `low >= 0`.
  - `test_swap_carries_cost` (A bought for 100 €, swapped for B → B cost 100 €, A realized 0).
  - `test_same_day_chain_a_to_b_to_c_converges`.
  - `test_usd_buy_converted_with_historical_eurusd`.
  - `test_rewards_grouped_monthly_dated_earliest_price_zero` (30 daily rewards of 0.0000004 → one trade of 0.000012).
  - `test_missing_history_reports_coin_and_leaves_price_none`.
  - `test_fixed_split_by_market_value_for_multi_coin_group`.
  - `test_value_only_below_one_cent`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat: price coin trades in Securo's replay order`.

---

### Task 8: Balancing trades

**Files:**
- Create: `securo_kraken/balancing.py`, `tests/test_balancing.py`

**Interfaces:**
- Consumes: `book.Trade`, `numbers.q6`.
- Produces:
  - `TOLERANCE = Decimal("0.00001")`, `FLOOR = Decimal("0.000001")`.
  - `balancing(coin: str, trades: list[Trade], target: Decimal, today: date, prev: dict) -> tuple[list[Trade], dict]` — returns the opening/adjust trades to add (pricing `market` for the opening, dated the day before the coin's first trade or `today - 1` when it has none; `avg` for the adjust, dated `prev` date while its quantity is unchanged, else `today`) and the new `prev` (`{"opening": "<qty>", "adjust": "<qty>", "adjust_date": "YYYY-MM-DD"}`). The low is replayed by `(date, buys first, id)` on q6 quantities: `dip = max(0, -low)`, `final = sum`, `opening = dip + max(0, target - (final + dip))`, `adjust = max(0, final + opening - target)`; differences ≤ `TOLERANCE` count as 0; a balancing trade sent before and now 0 is returned at `FLOOR`.
  - These trades go through `book.resolve` like the others (so the adjust sell is at average cost and the opening at market).

- [ ] **Step 1: Write the failing tests:** `test_no_balancing_when_trades_match`; `test_opening_for_missing_history`; `test_adjust_for_surplus_dated_today_then_kept`; `test_shrunk_to_floor_when_no_longer_needed`; `test_dip_from_sell_before_any_buy`; `test_sold_out_coin_reconciles_to_zero` (Review Focus: target 0 → `securo_replay.reconciles(all, 0)`).
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat: opening and adjustment trades`.

---

### Task 9: Payload and sync

**Files:**
- Create: `securo_kraken/payload.py`, `securo_kraken/plugin.py`, `tests/test_payload.py`, `tests/test_plugin.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `payload.CONTRACT = 1`, `payload.LOGO = "kraken"`.
  - `payload.account(ccy: str, balance: Decimal, cash: list[Cash]) -> dict` (fields per spec; `transactions` always a list).
  - `payload.holding(coin: str, *, valuation: str, quantity: Decimal, value: Decimal, price: Decimal | None, cost: Decimal, split: dict[str, str], trades: list[dict] | None, value_only: bool, tokenized: bool) -> dict` (`account_name: "Kraken"`, `type` `crypto` or `stock`, `quantity`/`trades` `None` when value-only by size; value-only by price keeps `quantity`, `trades: None`).
  - `payload.diff(trades_by_coin: dict[str, list[Trade]], sent: dict[str, list]) -> tuple[dict[str, list[dict] | None], dict]` — per coin: trades whose `(date, kind, quantity, price, fee)` differ from `sent` or are new; a sent id no longer produced → resent at `FLOOR`; coins with nothing to send → `None`; respects `MAX_TRADES` by deferring whole coins (their `sent` unchanged).
  - `plugin.Kraken(*, http=None, clock=None, wall=None, sleep=None, today=None)` with `sync(params: dict, context) -> dict`; module-level `plugin = Kraken()`.

State (`state["v"] = 1`): `valuation`, `altnames`/`altnames_at`, `pairs`/`pairs_at`, `ledger` (Task 4), `moves` (compact), `held` (held-back rows), `fiats`, `coins`, `prices` (History cache), `last_value` per coin, `balancing` per coin, `sent` per coin, `value_only` (sticky list).

Sync flow exactly as the spec's "Sync flow and state": validate config (`currency` blank → `EUR`; not in `VALUATIONS` → `NeedsAuth(code="currency_invalid")`; different from `state["valuation"]` → `NeedsAuth("Add a new connection to value assets in another currency.", code="currency_changed")`); `BalanceEx`; refresh `Assets`/`AssetPairs` when older than 24 h or a code is unknown; `ledger.advance`; `classify` (held rows re-fed next time); cash sent immediately; when `caught_up`: build trades, balancing, resolve, history fetch within the remaining budget minus 5 s, resolve again; `prices.current`; holdings only if nothing missing. `RateLimited`/`OutOfTime` never escape. `log.reset()` at start.

- [ ] **Step 1: Write the failing tests** (`tests/test_plugin.py` with FakeHttp, fake clock, `today`):
  - `test_first_sync_full_history_sends_accounts_holdings_trades` (EUR deposit, BTC buy, ETH buy + Earn allocation, rewards; holdings reconcile with `securo_replay`).
  - `test_backfill_interrupted_sends_cash_and_no_holdings_key` (Review Focus).
  - `test_second_sync_sends_only_new_or_changed_trades` (unchanged coins `trades: None`).
  - `test_split_leg_across_syncs_becomes_one_buy` (Review Focus: second leg arrives next sync).
  - `test_sold_out_coin_listed_every_sync`.
  - `test_valuation_change_needs_auth`; `test_blank_currency_defaults_to_eur`.
  - `test_missing_history_defers_holdings_until_priced`.
  - `test_state_and_data_are_json_serialisable_and_under_16mb_for_10k_rows`.
  - `test_earn_balance_split_in_metadata` (`ETH` holding quantity = `XETH + ETH.B + ETH2.S`, metadata lists each key).
  - `test_fiat_account_balance_sums_family` (`ZEUR + EUR.M + EUR.HOLD`).
  - `test_fiat_account_listed_after_balance_reaches_zero` (a USD account seen once stays in `accounts` with balance 0).
  - `tests/test_payload.py`: `test_diff_new_changed_unchanged`, `test_diff_vanished_trade_resent_at_floor`, `test_diff_trade_budget_defers_whole_coins`, `test_holding_value_only_by_size_and_by_price`, `test_amounts_are_strings`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS (`python -m pytest -q`, whole suite). **Step 5:** Commit `feat: sync Kraken accounts and holdings`.

---

### Task 10: Manifest, docs, icon

**Files:**
- Create: `securo-plugin.json`, `tools/make_icons.py`, `icon.png`, `logos/kraken.png`, `README.md`, `DESIGN.md`, `AGENTS.md`, `CLAUDE.md` (`@AGENTS.md`), `LICENSE` (MIT, 2026, nunomluz), `tests/test_manifest.py`

**Interfaces:**
- Consumes: `client` host constant, `payload.LOGO`, `assets.VALUATIONS`.

- [ ] **Step 1: Write the failing test** `tests/test_manifest.py`: network hosts == `{"api.kraken.com"}`; entrypoint; fields `{"api_key","api_secret","currency"}` with the first two `secret: true` and `currency` `required: false`; `provides == ["accounts","transactions","holdings","trades"]`; `sync_minutes == 60`; `logos[payload.LOGO]` is a file; no `api_scopes`; id matches `nunomluz/securo-kraken`; icons are PNG ≤ 64 KB; the integration description names both permissions (`Query Funds`, `Query Ledger Entries`).
- [ ] **Step 2:** FAIL. **Step 3:** Write the manifest (copy shape from Trading 212; English, plus `pt`), the icon generator (original mark, not Kraken's artwork: e.g. a dark-purple rounded square with a white stylised wave), run it, and the docs: README (beta; create a key with only the two permissions, used only by Securo; what syncs where; cost-basis rules from the spec in plain words; dust and micro-priced coins; ETH reward double-count caveat; how to report a problem with `tools/kraken_probe.py`); DESIGN.md with decisions D1… from the spec's "Calls made" and "Decided by the person"; AGENTS.md mirroring Trading 212's (layout table for this package, hard rules incl. frozen ids and "trades must replay in Securo's order").
- [ ] **Step 4:** PASS, whole suite green.
- [ ] **Step 5:** Commit `docs: manifest, README, design notes, agent files and icon`.
