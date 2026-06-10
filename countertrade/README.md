# Countertrade Fader

Dry-run-first tooling for fading consistently losing Polymarket wallets without
depending on paid leaderboard APIs.

The workflow is split in two:

1. Rank wallets offline from resolved fills.
2. Feed fresh fill events into the hot path and emit fixed-size counter-order
   intents.

The script accepts CSV, JSON, or JSONL. It is deliberately feed-agnostic: pipe in
events from a free listener, a browser export, an on-chain indexer, or your own
collector.

## Rank Bad Wallets

```bash
python3 countertrade/countertrade_fader.py rank \
  --fills-file resolved_fills.jsonl \
  --min-decisions 20 \
  --min-volume 500 \
  --max-win-rate 0.30 \
  --write-wallets countertrade/targets.json
```

By default, repeated trades by the same wallet on the same market/outcome count
as one decision. Use `--unit trade` if you want every fill counted.

## Emit Counter Orders

```bash
python3 countertrade/countertrade_fader.py signals \
  --fills-file recent_fills.jsonl \
  --wallets-file countertrade/targets.json \
  --fixed-dollars 25 \
  --min-source-price 0.30 \
  --max-source-price 0.70 \
  --max-slippage 0.01 \
  --max-exposure-per-market 75 \
  --jsonl \
  --write-orders countertrade/orders.jsonl
```

For a source buy of `YES @ 0.60`, the emitted order is `NO @ 0.40` with
`25 / 0.40 = 62.5` shares. If a row includes an opposite-side ask such as
`noAsk`, `yesAsk`, `oppositeAsk`, or `counterAsk`, the order is skipped when
that quote is more than `--max-slippage` above the fair inverse price. If no
quote is present, the output is marked `needs_quote_check: true`.

## Live JSONL Pipe

```bash
your_free_fill_listener | python3 countertrade/countertrade_fader.py stream \
  --wallets-file countertrade/targets.json \
  --jsonl
```

Rows without an explicit `BUY` action are skipped unless you pass
`--assume-buy`.

## Paper Test

```bash
python3 countertrade/countertrade_fader.py paper \
  --fills-file resolved_fills.jsonl \
  --wallets-file countertrade/targets.json \
  --fixed-dollars 25
```

This settlement-only paper test assumes holding to resolution. It does not model
the 20% take-profit exit because that requires intramarket price history after
entry.

## Useful Input Fields

The normalizer accepts common names:

- Wallet: `wallet`, `user`, `proxyWallet`, `trader`, `address`
- Market: `market_id`, `marketId`, `conditionId`, `market`, `slug`
- Outcome: `outcome`, `outcomeName`, `assetOutcome`, `tokenOutcome`, `position`
- Action: `action`, `tradeSide`, `orderSide`, `side`
- Price: `price`, `avgPrice`, `matchedPrice`, `fillPrice`
- Size: `size`, `shares`, `quantity`, `amount`
- Result: `win`, `won`, `pnl`, `realizedPnl`, `winningOutcome`, `resolution`

Binary `YES`/`NO` markets are supported. Multi-outcome markets are skipped
unless your feed already maps them to an invertible binary token.
