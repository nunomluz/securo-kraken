# Kraken plugin for Securo — design

Date: 2026-11-22
Status: approved (2026-11-22)

## Intent

A Securo code plugin that connects a Kraken (spot) account to Securo the way
the Trading 212 plugin does: you add it from **Accounts → + Connect Bank**,
paste a Kraken API key and secret, and Securo keeps it synced on a schedule.
It is strictly read-only: it never trades, moves money, or allocates to or
from Earn.

The person wants the same picture Trading 212 gives, applied to Kraken:

- each fiat balance (EUR, USD, …) as a **cash account**, with deposits,
  withdrawals, fees and rewards as cash activity;
- each coin they hold, stablecoins included, as an **asset** on the Assets
  page, with its quantity, current value, cost basis and gains;
- what is in **Earn** counted in the coin's asset (not a separate asset), with
  Earn rewards showing up as gain;
- full history, so cost basis and gains are real, not a snapshot.

One Securo connection per Kraken API key. A Kraken sub-account has its own
keys, so it is just another connection.

## Goals

- Read-only sync through Kraken's Spot REST API (`api.kraken.com`).
- Fiat → accounts; every crypto asset (stablecoins too) → one holding per
  coin, spot and Earn balances combined.
- Full history on the first syncs (backfilled over several syncs if long),
  then incremental.
- Re-running a sync never duplicates anything.
- Clear messages when the key is wrong or lacks a permission.
- Pure standard-library Python, tested with pytest against fake HTTP.

## Non-goals

- Any write endpoint: orders, withdrawals, Earn allocate/deallocate, exports.
- Kraken Futures, margin positions, NFTs as things in Securo. Their effect
  on the spot balances is still recorded (see "Transfers, margin and other
  outside moves"), so balances and ledgers stay explained.
- A separate asset per Earn product or per staking flavour.
- Securo pricing the coins itself: synced holdings are "manual" valuation in
  Securo, so the plugin reports each coin's value.
- Tax reports. Cost basis follows the rules below, which are chosen to show
  sensible gains, not to produce a tax filing.
- Keys with two-factor (OTP) protection on the API key itself.

## Anti-goals

- Requesting any key permission beyond the two read permissions below.
- Leaking the API key or secret into exception messages, logs, state, raw
  data or test fixtures.
- Real balances, coins held, ledger ids or amounts committed to the repo. All
  fixtures are made up (real responses may be used locally to shape them, and
  then masked).
- Counting Earn twice (once from the balance, once from Earn allocations).
- Made-up gains: a coin leaving to the person's own wallet, a swap, an
  internal move into or out of Earn, or a balancing trade must never create a
  realized gain.
- Duplicated records after a format change: external-id formats are frozen
  once released.

## Constraints

From Securo (verified in `securo-finance/securo` main, commit f5ab5f9,
2026-11-22, `backend/app/plugins/contract.py`,
`docs/plugins/bank-provider-plugins.md`,
`backend/app/services/connection_service.py`; same rules the Trading 212 plugin
follows):

- Python, standard library only, Python 3.13 locally, 3.14 in the runner.
  60 s per call. Network limited to hosts declared in the manifest.
- `sync(params, context)` returns `{"state", "data"}`; `data` is a
  provider-contract-1 payload or `None`. Securo keeps `state` only after it
  wrote `data`.
- **Account `currency` and holding `currency` must match `^[A-Z]{3}$`.** So
  an account can only be a fiat currency, and USDT/USDC (4 letters) can't be
  accounts. Holdings are valued in a 3-letter fiat currency.
- Accounts are matched by `external_id` within the connection
  (`Account.connection_id == connection.id`). Holdings are matched across
  the workspace by (source, external_id), but Securo prefixes every holding
  id with the bank connection's id before matching
  (`backend/app/plugins/provider.py`, `scope=bank.external_id`). So two
  Kraken connections (main account and a sub-account) don't collide, and
  the plugin's ids need nothing per connection.
- Units and holding values: Numeric(15, 6) — at most 9 digits before the
  point, 6 kept after. Trade quantity/price: Numeric(18, 6). Money: 2
  decimals. Amounts travel as decimal strings, ≤ 8 decimals, no floats.
  Trade quantity > 0, price ≥ 0, fee ≥ 0.
- Trades are overlaid on stored ones by `external_id` (reported wins), and
  the set is kept only if a replay (by date, buys first, then external_id)
  never goes below zero and ends on the reported quantity, within
  max(0.0001, 1e-6 × largest lot).
- `holdings: null` = not fetched (left alone); `[]` archives everything. A
  holding left out of a reply is archived. `trades: null` leaves the stored
  ledger alone.
- Per reply: ≤ 500 accounts, 20,000 transactions, 5,000 holdings, 20,000
  trades; whole stdout ≤ 16 MB.
- `NeedsAuth` (exception named exactly that) stops scheduled syncs until the
  person fixes the connection; anything else is retried with backoff.
- Logo ships in the package (`logos` in the manifest).
- `min_securo_version` 0.20.0.

From Kraken (docs.kraken.com, Spot REST, read 2026-11-22):

- Host `api.kraken.com`. Private methods: `POST /0/private/<Method>`,
  form-encoded body with `nonce`; headers `API-Key` and `API-Sign` =
  base64(HMAC-SHA512(base64-decoded secret, path + SHA256(nonce + body))).
  The docs give a test vector; use it as a unit test.
- Nonce must always increase per key. Millisecond time is the usual choice;
  another program using the same key with a larger nonce breaks it. The
  README asks for a key used by Securo only.
- Permissions: **Funds → Query** (`Balance`, `BalanceEx`) and **Data → Query
  ledger entries** (`Ledgers`, `QueryLedgers`). Nothing else. `GetApiKeyInfo`
  needs no permission and returns the key's permissions (self-check).
- Rate limit: per-key counter; ledger calls cost 2, others 1. Starter tier:
  max 15, decays 0.33/s (Intermediate 20/0.5, Pro 20/1). Over the limit:
  `EAPI:Rate limit exceeded`. No rate-limit headers. Roughly 14 ledger pages
  (≈ 700 entries) fit in a 40 s budget on a starter account.
- `Ledgers`: 50 per page, newest first; `start` (exclusive) / `end`
  (inclusive) take a timestamp or a ledger id; `without_count`;
  `with_cursor`/`cursor`; `ofs` is deprecated. Entry fields: `refid`, `time`
  (unix float), `type`, `subtype`, `aclass`, `asset`, `amount` (signed),
  `fee`, `balance`. Both legs of a trade share a `refid`.
- Ledger `type` values (API spec): `trade, deposit, withdrawal, transfer,
  margin, adjustment, rollover, spend, receive, settled, credit, staking,
  reward, dividend, sale, conversion, nfttrade, nftcreatorfee, nftrebate,
  custodytransfer`; Kraken's support article adds `earn` and `invite
  bonus`, and documents the subtypes `allocation`, `deallocation`,
  `autoallocate`, `reward`, `migration` (for `earn`) and `spottostaking`,
  `stakingfromspot`, `stakingtospot`, `spotfromstaking`, `spottofutures`,
  `spotfromfutures` (for `transfer`). Each row changes exactly one asset;
  `balance` = previous ± `amount` − `fee`, fee in the row's asset.
- Asset codes: legacy `X`/`Z` prefixes (`XXBT`, `XETH`, `ZEUR`), `XBT` =
  BTC. Balance keys carry suffixes for Earn: `.S` (legacy staking), `.M`
  (opt-in rewards), `.B` (new yield products), `.F` (auto-earn), and others
  (`.P`, `.T` tokenized assets). Legacy ETH staking appears as `ETH2`/`ETH2.S`.
- Public (no key): `Assets` (altnames), `AssetPairs`, `Ticker` (`c[0]` last
  price), `Trades` (`pair`, `since`) for historical prices. Public calls have
  their own limit (~1/s).
- `DepositStatus`/`WithdrawStatus` are deprecated; the ledger already has
  deposits and withdrawals, so they aren't used.

## Approaches considered

| | Approach | Trade-off |
|---|---|---|
| **A (chosen)** | **Ledger through the live API.** Balances every sync; the whole ledger backfilled by paging, then read incrementally. Everything (trades, conversions, deposits, withdrawals, Earn moves and rewards, fees) comes from one endpoint and one permission. | Long histories take several hourly syncs to backfill (≈ 700 entries per sync on a starter account). |
| B | CSV ledger export (`AddExport` → poll → `RetrieveExport`). | Whole history in one file, but it creates reports in the person's Kraken account (a write), needs the Export permission, takes minutes to be ready, and the CSV columns aren't documented. |
| C | `TradesHistory` plus the ledger. | Slightly more precise trade prices, but another permission and a second copy of what the ledger already says. |

The person accepted my pick, **A**.

**Swaps (crypto ↔ crypto)** — considered:

| | Rule | Trade-off |
|---|---|---|
| **1 (chosen)** | **Carry the cost over.** The coin given up leaves at its average cost (no realized gain); the coin received takes over that cost. | No historical price lookups; the gain shows on the new coin later. |
| 2 | Sale at market price. | Realized gain at each swap, but one historical price lookup per swap and a slower first sync. |

The person accepted my pick, **1**.

## Design

### Manifest

- `id`: `nunomluz/securo-kraken`. Version 0.1.0, beta.
- `network`: `api.kraken.com` only ("Reads your balances and ledger with the
  API key you enter, and Kraken's public prices").
- One integration, key `api`, `auth: "credentials"`, `sync_minutes: 60`,
  `provides: ["accounts", "transactions", "holdings", "trades"]`.
- Fields:
  - `api_key` — "API key", `secret: true`.
  - `api_secret` — "Private key", `secret: true`.
  - `currency` — "Value assets in (EUR, USD, GBP, CAD, CHF, AUD or JPY;
    default EUR)", `required: false`.
- Integration description tells the person to create a key with only
  **Query Funds** and **Query Ledger Entries**, used by nothing else.
- No `api_scopes`. English strings, plus Portuguese.
- `logos`: `{"kraken": "logos/kraken.png"}`, original artwork.

### Names: coin families and fiat

Every Kraken asset code is reduced to a **family code**:

1. Strip a staking suffix `.S`, `.M`, `.P`, `.F`, `.B`, then (only when a
   suffix was stripped, and not for `ETH2`) trailing digits, which mark
   bonded-days variants (rotki, issue 6587: e.g. `DOT28.S` → `DOT`). Strip
   `.HOLD`. Any other suffix (e.g. `.T`) is kept and logged.
2. Map the legacy code to its display code using `/0/public/Assets`
   `altname` (cached in state, refreshed at most every 24 h, and whenever an
   unknown code shows up): `XXBT` → `XBT`, `ZEUR` → `EUR`.
3. Fixed aliases (rotki and ccxt): `XBT` → `BTC`, `XDG` → `DOGE`, `SETH` →
   `ETH`, `ETH2` → `ETH`, `FLOWH` → `FLOW`.
4. `KFEE` (fee credits) is neither fiat nor a coin and is ignored.

**Fiat** = a family code in a fixed list of Kraken fiat currencies (`USD,
EUR, GBP, CAD, CHF, AUD, JPY, AED, KRW`, extendable). Everything else is
**crypto**, stablecoins included.

### Accounts (fiat)

One account per fiat family that has a balance now or appears in the ledger.
Once listed, it is listed every sync (kept in state).

- `external_id`: `kraken:<CCY>` (e.g. `kraken:EUR`).
- `name`: `Kraken <CCY>`; `type: "investment"`; `currency: <CCY>`;
  `institution_name: "Kraken"`; `institution_logo: "kraken"`.
- `balance`: sum of `BalanceEx.balance` over every key in that family
  (`ZEUR` + `EUR.M` + `EUR.F` …). Fiat in Earn stays in the account.
- `transactions` from fiat ledger entries, positive amounts with
  `credit`/`debit` by the sign of `amount`, payee "Kraken", external id
  `kraken:<ledger id>`:
  - `deposit`, `withdrawal`: description "Deposit" / "Withdrawal".
  - a `fee` > 0 on any fiat entry that becomes a transaction: a separate
    debit `kraken:<ledger id>:fee`, description "Kraken fee".
  - rewards on fiat (`staking`, `reward`, `earn`/`reward`): credit,
    "Kraken rewards".
  - fiat ↔ fiat trades/conversions (EUR → USD): one transaction on each
    account, "Conversion EUR → USD".
  - `adjustment`, `credit`, `settled`, `rollover`, `dividend`, `sale` and
    unknown types: by sign, description = the type name (sanitised); unknown
    types logged by name only.
  - **Not** transactions: the fiat leg of a crypto buy/sell (it belongs to
    the holding, as in Trading 212 D17), and internal moves between members
    of the same family (`transfer`, `earn` allocation/deallocation/migration).
  - Fees on the fiat leg of a crypto trade go into the trade's `fee`.

### Holdings (crypto)

One holding per crypto family, keyed by family code.

- `external_id`: `kraken:<COIN>` (e.g. `kraken:BTC`).
- `name` and `ticker`: the family code (a small built-in name map for
  well-known coins is the builder's call). `type: "crypto"`.
- `account_name`: "Kraken" (no `account_external_id`, so all coins land in
  one "Kraken" wallet on the Assets page).
- `currency`: the **valuation currency** (field `currency`, default EUR).
- `quantity`: sum of `BalanceEx.balance` over every key in the family,
  rounded half-up to 6 decimals (dust below 0.000001 is lost; documented).
- `current_value`: quantity × current price in the valuation currency;
  `unit_price` that price.
- `metadata`: the per-key split, e.g. `{"spot": "0.5", "earn": {"ETH.B":
  "1.2"}}`, plus the price source pair.
- `purchase_price`: total cost basis held (average-cost method below).
- Coins sold out stay listed with quantity 0 (kept in state), as Trading 212
  D15.
- A coin whose quantity would exceed Securo's 9-digit column is sent with
  value only (`quantity`/`trades` null) and logged.
- **Micro-priced coins** (SHIB, PEPE, BONK…): Securo stores trade prices to
  6 decimals and replays cost as quantity × stored price, so a coin priced
  below about 0.01 in the valuation currency would get several-percent
  cost errors and made-up gains on every average-cost sell. A coin whose
  price, at any trade or now, is below 0.01 is sent **value-only**: no
  trades (`trades: null`), cost basis in `purchase_price` from the
  plugin's own exact running cost. The rule is checked every sync; once a
  coin goes value-only it stays so (state), to avoid a half-stored ledger.

**Current prices**: one public `Ticker` call per sync for all needed pairs.
Pair for each coin: direct `<COIN>/<valuation>` if it exists (from
`AssetPairs`, cached 24 h); otherwise via `<COIN>/USD` × USD→valuation, then
via `<COIN>/BTC` × BTC→valuation. No price at all → the last value seen
(state), else 0, logged.

### Trades from the ledger

Ledger entries are grouped by `refid`. Every crypto family keeps, in state,
its running **units** and **cost** (in the valuation currency), processed in
time order. Each crypto-side movement becomes a Securo trade:

**Classification.** Sourced from Kraken's support article "How to
interpret ledger history fields" (updated 2026-10-06), rotki's Kraken
importer (`rotkehlchen/exchanges/kraken.py`, `assets/converters.py`) and
ccxt's `kraken.py`; see "Sources". Rules are applied in this order:

1. **Internal by subtype, whatever the refid** (nothing recorded, on either
   side, because the holding's quantity is the sum of the family anyway):
   - `transfer` with subtype `spottostaking`, `stakingfromspot`,
     `stakingtospot`, `spotfromstaking` (legacy staking: one row on the base
     asset, one on the `.S` asset);
   - `earn` with any subtype other than `reward` (`allocation`,
     `deallocation`, `autoallocate`, `migration`, and unknown ones, logged);
   - `staking` with a **negative** amount (rotki: the "automatic virtual
     conversion of staked ETH rewards to ETH" on ETH2);
   - two rows of one refid with the same type and subtype and mirrored
     amount and fee (rotki: "staking transfer pairs can cancel out with
     different asset symbols").
2. **Rewards** → buy at price 0: positive `staking`, `earn`/`reward`,
   `reward`, `dividend`, `invite bonus`, and `transfer` with **no subtype**
   (Kraken's article: airdrops and supported forks use this).
3. **Trades by refid**: the remaining legs of a refid of type `trade`,
   `spend`, `receive`, `conversion`, `sale`, `adjustment` are netted per
   family, net of each leg's `fee` (fees can sit on the receiving leg):

   | Net result | Trade on the coin | Price / cost |
   |---|---|---|
   | Crypto up, fiat down (buy; also card purchases: `deposit` → `spend` → `receive`) | buy, quantity = units received net of fees | cost = fiat spent + fiat fee, in valuation ccy; price = cost excl. fee ÷ qty; fee = fiat fee |
   | Crypto down, fiat up (sell) | sell | price = fiat received ÷ qty; fee = fiat fee |
   | Crypto A down, crypto B up (swap; also delisting conversions via `adjustment`, e.g. ICN → ETH) | sell A at A's average cost (no gain); buy B | B's cost = cost carried from A, fees in crypto included |
   | More than one coin down or up (dust sweeps) | sells at average cost; buys | carried cost split across received coins by their market value at that time |
   | One leg only (Kraken writes no fiat leg when it rounds to 0, per rotki) | lone receive → buy at market; lone spend → sell at average cost | logged |
   | Fiat ↔ fiat only | none (cash transactions, see Accounts) | — |

   A refid whose legs aren't all in yet waits only while it sits at the
   newest edge of what has been read (within 1 hour of the newest entry);
   older lone legs are final.
4. **Deposits and withdrawals** of crypto: `deposit` → buy at market price
   then (it includes transfers in from the Futures wallet, per Kraken's
   article); `withdrawal` → sell at average cost, quantity = amount + fee.
5. **Futures, margin and anything else**, by sign: `transfer`
   `spottofutures`/`spotfromfutures`, `margin`, `rollover`, `settled`,
   `credit`, `custodytransfer`, NFT types and unknown types. Crypto down →
   sell at average cost; crypto up → buy at market. Logged by type/subtype
   name only.
6. **KFEE** (Kraken fee credits) is not a coin: its rows are ignored and
   fees paid in KFEE count as 0.
7. A fee charged in crypto on a row that is otherwise internal → sell at
   average cost.

- **Fiat side** of rules 4–5 (incl. `margin`, `rollover`, `settled`,
  Futures transfers): a cash transaction by sign, description = type
  (sanitised), so the account's activity explains its balance. Fiat rows of
  rule 1 (e.g. `EUR` ↔ `EUR.M`) are internal.
- **ETH rewards double count risk**: rotki shows legacy ETH2.S rewards
  credited on ETH2 and then "virtually converted" to ETH. The negative leg
  is internal (rule 1); if the positive ETH leg is also typed as a reward,
  rewards would count twice. The balancing trades (below) keep the quantity
  right either way; the README notes it.
- **Fiat other than the valuation currency** (BTC bought with USD, valued in
  EUR): converted at Kraken's own USD→EUR price at that time (historical
  lookup).
- **Historical prices**: public `Trades` with `pair` and `since` = the
  event's time; the first trade's price is the price then. Cached in state
  per (pair, UTC hour). Paced under the public limit and inside the time
  budget; events whose price isn't fetched yet wait for the next sync. If
  the pair didn't trade then (not listed yet), fall back to the next price
  Kraken has, logged.
- **Rewards are grouped**: all reward buys of one coin in one calendar month
  (UTC) become one trade `kraken:reward:<COIN>:<YYYY-MM>`, resent with its
  growing quantity during the month (reported wins). Rewards can be daily
  or hourly dust: one trade each would round to 0 at Securo's 6 decimals and
  could exceed the trade cap.
- **External ids**: a trade from a ledger event is `kraken:<ledger id>` of
  that coin's leg (a swap gives two trades with their two leg ids);
  rewards as above; balancing trades `kraken:opening:<COIN>` and
  `kraken:adjust:<COIN>`.
- **Balancing trades** (as Trading 212 D14): if the trades don't add up to
  the reported quantity (history Kraken no longer shows, rounding, a
  mapping gap), an opening buy (dated before the first known event, priced
  at the market price on its date) and an adjustment sell (dated the sync
  day, priced at average cost) are sized from scratch every sync and resent
  under the same ids; one no longer needed is resent at 0.000001. Neither
  may create a gain at the moment it is made.
- Trade quantities are the exact ledger figures rounded to 6 decimals;
  rounding drift is absorbed by the balancing trades.

### Sync flow and state

Every sync:

1. Read credentials and `currency`; validate. Unknown currency, empty key
   or secret → `NeedsAuth`. The valuation currency is fixed at the first
   sync (state); a later change → `NeedsAuth` ("Add a new connection to
   value assets in another currency").
2. `BalanceEx` → accounts' balances and holdings' quantities.
3. Public `Assets`/`AssetPairs` if the cache is stale or a code is unknown.
4. Ledger:
   - **Backfill**: walk from newest to oldest page (`end` = oldest id seen,
     de-duplicated by ledger id) until a page brings nothing new. Entries
     are kept in state in a compact form until backfill completes.
   - **Incremental**: read from the newest entry seen (with a small time
     overlap, de-duplicated by a complete seen-ids set) until caught up.
   - Stop on the ~40 s budget or a rate-limit error, keep the position,
     return what was gathered: not a failure.
5. Historical prices still needed, within what's left of the budget.
6. Public `Ticker` for current prices.
7. Build the payload.

- **Cash transactions** are sent as they are collected (Securo dedups by id),
  so they show up during backfill.
- **Holdings and trades are only sent once the ledger backfill is complete
  and every price they need is known** (Trading 212 D8). Until then
  `holdings` is left out (`null`), so nothing is archived.
- After the first full holdings payload, state keeps per coin only what
  later syncs need: units, cost, last price, the trades of the current
  reward month, balancing-trade sizes and dates, seen ledger ids.
- A trade group with only one leg seen so far (the other on an unread page)
  waits in state until its partner arrives.
- If one reply would exceed 20,000 trades, spread them over syncs as Trading
  212 does (holdings beyond the budget go with `trades: null`).

### Errors

- `EAPI:Invalid key`, `EAPI:Invalid signature`, `EAPI:Bad request` on auth →
  `NeedsAuth("Kraken rejected the API key or private key.")`.
- `EGeneral:Permission denied` → `NeedsAuth` naming the permission the
  failing call needs (Query Funds or Query Ledger Entries).
- `EAPI:Invalid nonce` → retryable, with a message suggesting a key used
  only by Securo.
- `EAPI:Rate limit exceeded`, running out of budget → stop, keep position,
  return what was gathered.
- `EService:*`, `EGeneral:Temporary lockout`, 5xx, network errors →
  retryable, fixed messages without URLs, keys or bodies.
- An OTP-protected key (`EAPI:Invalid arguments: otp` or similar) →
  `NeedsAuth` asking for a key without 2FA.

### Package layout (mirrors Trading 212)

```
securo-plugin.json, pyproject.toml (dev: pytest), README.md, DESIGN.md,
AGENTS.md, CLAUDE.md, LICENSE (MIT), icon.png, logos/kraken.png
securo_kraken/
  client.py     # signing, nonce, private/public calls, budget, error mapping
  assets.py     # family codes, fiat list, pair discovery
  ledger.py     # paging the ledger: backfill, incremental, seen ids
  prices.py     # current prices (Ticker) and historical (Trades) with cache
  book.py       # per-coin units and cost; ledger events → trades
  balancing.py  # opening/adjust trades
  payload.py    # contract-1 accounts, transactions, holdings
  plugin.py     # sync() orchestration and state
  errors.py, numbers.py, log.py
tests/          # fake HTTP + made-up JSON
```

## Verification

- Signing tested against the Kraken docs' test vector.
- Unit tests per module with made-up ledgers covering: buys and sells in the
  valuation and another fiat, swaps, deposits, withdrawals with fees, Earn
  allocation/deallocation/migration (no trades), `.S`/`ETH2` legacy staking,
  hourly rewards grouped by month, partial trade groups across pages, a
  multi-sync backfill, rate-limit stops, and that every holding's trades
  replay to its quantity under Securo's rule.
- **No Kraken key during development** (the person's choice). Fixtures
  are built from the API spec, Kraken's support article and the shapes
  rotki and ccxt handle. Everything not confirmed there is handled
  defensively: unknown types are classified by sign (rule 5), and the
  balancing trades keep every quantity right, so a misread entry can only
  misprice a lot, never misstate a balance.
- The first real sync is the real-data check. The README asks for reports
  of mismatched quantities or odd gains, and points to
  `tools/kraken_probe.py`, a read-only script that prints only the shape of
  a ledger (types, subtypes, asset codes, leg patterns; no amounts, ids or
  dates beyond the month) so a report can be shared safely.
- Unknown ledger types and subtypes are logged by name only, never amounts,
  ids or coins.
- Release 0.1.0 marked beta.

## Decided by the person

- Kraken spot accounts, read-only, everything Trading 212 gives (cost basis,
  gains, cash activity, full history).
- Approach A (ledger through the live API).
- Swaps carry the cost over (rule 1).
- Earn rewards at zero cost (gain); withdrawals at average cost (no gain).
- No Kraken key during development: rules come from public sources
  (Kraken support, rotki, ccxt) and are defensive where those are silent.

## Calls made by me (correct me)

- Fiat → accounts (type `investment`), crypto incl. stablecoins → holdings,
  spot and Earn combined per coin, Earn split in metadata.
- Fiat in Earn (`EUR.M`) stays in the fiat account; fiat rewards are cash
  transactions.
- One valuation currency per connection (default EUR), fixed after the
  first sync.
- Crypto deposits priced at market at deposit time.
- Rewards grouped into one trade per coin per month.
- Crypto buys/sells not in cash activity; fiat ↔ fiat conversions are.
- All coins in one "Kraken" wallet.
- Futures, margin, NFTs out of scope as things in Securo; their effect on
  spot balances is recorded by net effect (outflows at average cost,
  unknown inflows at market, airdrops/forks at 0).
- Coins priced below 0.01 in the valuation currency are value-only (no
  trades), because Securo's 6-decimal trade prices would make up gains.
- `sync_minutes: 60`, version 0.1.0 beta, permissions Query Funds + Query
  Ledger Entries only.

- Tokenized stocks (xStocks, `aclass: tokenized_asset` / `.T`): holdings
  with `type: "stock"`, priced with `Ticker` `asset_class=tokenized_asset`;
  otherwise the same rules.
- Airdrops and forks (`transfer` with no subtype) at zero cost; Futures
  transfers in at market.
- Dust sweeps split carried cost by market value.

## Sources

- Kraken Spot REST docs and OpenAPI spec, docs.kraken.com (2026-11-22).
- Kraken support: "How to interpret ledger history fields" (360001169383,
  updated 2026-10-06), "What is Asset.S and Asset.M" (360039879471),
  on-chain staking and Opt-In Rewards overviews (360037682011,
  360044886311).
- rotki `develop`: `rotkehlchen/exchanges/kraken.py`,
  `rotkehlchen/assets/converters.py`.
- ccxt `master`: `python/ccxt/kraken.py` (`commonCurrencies`,
  `safe_currency_code`, `parse_ledger_entry`).

## Left to the builder

- Exact time budget split between ledger pages and price lookups.
- Ledger paging mechanics (`end`/`start` by id vs cursor), as long as
  nothing is missed or doubled.
- Compact state encoding.
- Name map for well-known coins.
- Test structure and fixture shapes.
- README wording; icon/logo artwork (original, no Kraken trademarks beyond
  naming the service).
