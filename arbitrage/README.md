# Sportsbook Arbitrage Bot

Dry-run-first esports arbitrage bot based on the writeup flow:

1. Load decimal sportsbook odds.
2. De-vig two-way prices with proportional normalization.
3. Match teams to a two-outcome Polymarket market.
4. Post maker buy quotes only when the Polymarket bid can be improved while
   staying at least `7%` below sportsbook fair value.
5. When a fill appears, emit a hedge buy for the opposite outcome only if the
   completed set still costs `0.93` or less by default.

This is intentionally a bot scaffold plus execution guardrail. It does not
pretend one-leg fills are riskless. Those fills are the dangerous part, so they
are first-class inputs via `--fills-file` or the local state file.

## Odds Input

CSV, JSON, and JSONL are supported. Required fields are event id, two teams,
and decimal odds:

```json
[
  {
    "event_id": "cs2-faze-navi-2026-06-10",
    "bookmaker": "example",
    "sport": "cs2",
    "team_a": "FaZe Clan",
    "team_b": "NAVI",
    "odds_a": "1.57",
    "odds_b": "2.28",
    "market_slug": "faze-clan-vs-navi"
  }
]
```

If `market_slug` is present, the bot tries to fetch the Polymarket US
`marketSides` from the gateway. For production, prefer an explicit mapping file
so team aliases and long/short side direction are reviewed before money is
involved.

## Odds Collector

The collector writes the `odds.jsonl` file consumed by `arbitrage_bot.py`.

Preferred route: use an odds API/feed. `odds-api.io` has a free tier and a v3
flow that fits this bot: fetch esports events, batch-fetch odds with
`/v3/odds/multi`, then upsert `odds.jsonl`.

Odds-API.io, one pull:

```bash
export ODDS_API_IO_KEY="..."
# Optional: rotate across multiple free-tier keys
export ODDS_API_IO_KEYS="key1,key2,key3"

python3 arbitrage/odds_collector.py \
  --provider odds-api-io \
  --once \
  --sport esports \
  --bookmakers GG.BET,Pinnacle \
  --output arbitrage/odds.jsonl
```

Odds-API.io, continuous:

```bash
python3 arbitrage/odds_collector.py \
  --provider odds-api-io \
  --sport esports \
  --bookmakers GG.BET,Pinnacle \
  --refresh-seconds 45 \
  --output arbitrage/odds.jsonl
```

If the sport slug differs on your account, discover supported sports:

```bash
curl "https://api.odds-api.io/v3/sports"
```

Then pass the returned slug with `--sport`.

Direct consumer-site scraping for books such as GGBet or SpinBetter is brittle,
dynamic, and Terms-of-Service sensitive. The public scraper paths below are
fallbacks only.

Automatic public scraper, one pull:

```bash
python3 arbitrage/odds_collector.py \
  --provider egamersworld-auto \
  --once \
  --egw-game cs2 \
  --output arbitrage/odds.jsonl
```

If your local Python certificate store rejects a public page that opens in your
browser, you can use `--insecure-ssl` as a last resort. If the site returns
different content to plain HTTP clients, use Playwright rendering:

```bash
python3 arbitrage/odds_collector.py \
  --provider egamersworld-auto \
  --render-html \
  --egw-game cs2 \
  --refresh-seconds 45 \
  --output arbitrage/odds.jsonl
```

The Odds API, one pull:

```bash
export THE_ODDS_API_KEY="..."

python3 arbitrage/odds_collector.py \
  --provider the-odds-api \
  --once \
  --sport esports_cs2 \
  --bookmakers ggbet,pinnacle,betonlineag,bovada \
  --output arbitrage/odds.jsonl
```

The Odds API, continuous:

```bash
python3 arbitrage/odds_collector.py \
  --provider the-odds-api \
  --sport esports_cs2 \
  --sport esports_dota2 \
  --sport esports_lol \
  --bookmakers ggbet,pinnacle,betonlineag,bovada \
  --refresh-seconds 30 \
  --output arbitrage/odds.jsonl
```

GGBet/SpinBetter or another provider JSON export:

```bash
python3 arbitrage/odds_collector.py \
  --provider ggbet-json \
  --once \
  --source-file ggbet-response.json \
  --events-path matches \
  --event-id-path id \
  --team-a-path teams.a \
  --team-b-path teams.b \
  --bookmakers-path books \
  --bookmaker-key-path name \
  --markets-path markets \
  --market-key-path type \
  --outcomes-path prices \
  --outcome-name-path team \
  --outcome-price-path decimal \
  --output arbitrage/odds.jsonl
```

Public web page scrape from embedded JSON:

```bash
python3 arbitrage/odds_collector.py \
  --provider public-html \
  --once \
  --source-url "https://example.com/esports/cs2" \
  --events-path props.pageProps.matches \
  --event-id-path id \
  --team-a-path teams.a \
  --team-b-path teams.b \
  --sport-path game \
  --league-path league.name \
  --bookmakers-path books \
  --bookmaker-key-path name \
  --markets-path markets \
  --market-key-path type \
  --outcomes-path prices \
  --outcome-name-path team \
  --outcome-price-path decimal \
  --output arbitrage/odds.jsonl
```

If the odds are rendered client-side, add Playwright rendering:

```bash
python3 -m pip install playwright
python3 -m playwright install chromium

python3 arbitrage/odds_collector.py \
  --provider ggbet-html \
  --render-html \
  --source-url "https://example.com/esports/cs2" \
  --events-path props.pageProps.matches \
  --event-id-path id \
  --team-a-path teams.a \
  --team-b-path teams.b \
  --outcomes-path prices \
  --outcome-name-path team \
  --outcome-price-path decimal \
  --output arbitrage/odds.jsonl
```

The `ggbet-json`, `ggbet-html`, `spinbetter-json`, `spinbetter-html`, and
`egamersworld-html` providers are aliases for configurable extraction. Use them
with public pages, permitted endpoints, saved responses, or feed exports. They
do not bypass bot protection, login walls, geofencing, or authentication.

Run the scraper in one terminal and the bot in another:

```bash
python3 arbitrage/odds_collector.py \
  --provider egamersworld-auto \
  --egw-game cs2 \
  --refresh-seconds 45 \
  --output arbitrage/odds.jsonl
```

```bash
python3 arbitrage/arbitrage_bot.py \
  --odds-file arbitrage/odds.jsonl \
  --markets-file markets.json \
  --quote-dollars 5 \
  --max-orders-per-cycle 2
```

## Market Mapping

```json
[
  {
    "event_id": "cs2-faze-navi-2026-06-10",
    "market_slug": "faze-clan-vs-navi",
    "outcomes": [
      {"name": "FaZe Clan", "intent": "ORDER_INTENT_BUY_LONG", "aliases": ["FaZe"]},
      {"name": "NAVI", "intent": "ORDER_INTENT_BUY_SHORT", "aliases": ["Natus Vincere"]}
    ]
  }
]
```

## Dry Run

One scan and exit:

```bash
python3 arbitrage/arbitrage_bot.py \
  --once \
  --odds-file arbitrage/odds.example.json \
  --markets-file arbitrage/markets.example.json \
  --no-fetch-books
```

Continuous dry run. This loops until you stop it with `Ctrl-C`:

```bash
python3 arbitrage/arbitrage_bot.py \
  --odds-file odds.jsonl \
  --markets-file markets.json \
  --quote-dollars 5 \
  --max-orders-per-cycle 2 \
  --write-orders arbitrage/orders.jsonl
```

Useful safety knobs:

```bash
python3 arbitrage/arbitrage_bot.py \
  --once \
  --odds-file odds.jsonl \
  --markets-file markets.json \
  --min-edge 0.07 \
  --min-locked-edge 0.07 \
  --min-spread 0.05 \
  --quote-dollars 10 \
  --max-orders-per-cycle 3 \
  --write-orders arbitrage/orders.jsonl
```

## Hedge A Fill

When one side fills, feed it back in:

```json
[
  {
    "event_id": "cs2-faze-navi-2026-06-10",
    "market_slug": "faze-clan-vs-navi",
    "outcome": "FaZe Clan",
    "intent": "ORDER_INTENT_BUY_LONG",
    "price": "0.52",
    "quantity": "50",
    "fill_id": "fill-abc"
  }
]
```

Then run:

```bash
python3 arbitrage/arbitrage_bot.py \
  --once \
  --odds-file odds.jsonl \
  --markets-file markets.json \
  --fills-file fills.json
```

For a `0.52` fill and `--min-locked-edge 0.07`, the hedge cap is `0.41`
because `0.52 + 0.41 = 0.93`.

## Live Mode

Live mode uses the same `polymarket-us` SDK pattern as the other bots in this
repo:

```bash
python3 -m pip install polymarket-us
export POLYMARKET_KEY_ID="..."
export POLYMARKET_SECRET_KEY="..."
python3 arbitrage/arbitrage_bot.py \
  --live \
  --odds-file odds.jsonl \
  --markets-file markets.json \
  --quote-dollars 1 \
  --max-orders-per-cycle 1 \
  --max-order-age-seconds 3 \
  --write-orders arbitrage/live-orders.jsonl
```

By default live mode also runs reconciliation before each cycle:

```bash
python3 arbitrage/arbitrage_bot.py \
  --live \
  --once \
  --odds-file odds.jsonl \
  --markets-file markets.json \
  --quote-dollars 1 \
  --max-orders-per-cycle 1
```

Keep size tiny. Faster bots picking off stale quotes is a real risk, and this
script will only be as fresh as the odds and books you feed it.
