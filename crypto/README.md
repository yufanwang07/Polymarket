# Crypto Momentum Bot

`crypto_momentum_bot.py` is a dry-run-first Polymarket US crypto bot inspired by:

- TheOverLordEA's Rust HFT engine: keep signal, execution, risk, and state separate.
- Chudi's BTC latency strategy: watch Binance for a large move over a rolling window, then enter the matching Polymarket crypto market before odds fully reprice.

## Setup

Install dependencies:

```bash
python3 -m pip install polymarket-us websockets requests
```

The root `.env` is loaded automatically:

```bash
POLYMARKET_KEY_ID=...
POLYMARKET_SECRET_KEY=...
```

## Safe Commands

List candidate crypto markets:

```bash
python3 crypto/crypto_momentum_bot.py --scan-once
```

Run one REST momentum check:

```bash
python3 crypto/crypto_momentum_bot.py --once
```

Run streaming dry-run:

```bash
python3 crypto/crypto_momentum_bot.py
```

Run paper simulation:

```bash
python3 crypto/crypto_momentum_bot.py --simulate
```

When Polymarket US has no active matching crypto market, simulation mode uses
synthetic 5-minute binary markets so the bot still paper-trades and reports
runtime performance. Disable that with:

```bash
python3 crypto/crypto_momentum_bot.py --simulate --no-sim-synthetic-markets
```

Live mode:

```bash
python3 crypto/crypto_momentum_bot.py --live
```

## Defaults

- Symbols: `BTCUSDT,ETHUSDT`
- Signal: absolute move >= `0.003` over `60` seconds
- Notional: `$5` per order
- Simulation sizing: minimum 1 share, maximum 10 shares
- Synthetic sim market price: `0.50`
- Entry: maker-style limit by default
- Risk: max 3 tracked open orders, 180 second cooldown, stale order cancellation, Coinbase/Binance depeg check
- Price source: `auto`, which falls back from Binance.com to Binance.US in REST checks and uses Binance.US for streaming. Override with `--price-source binance` if running somewhere Binance.com is available.
