# Binary Hedge Arbitrage

Scanner/trader for same-market binary complete-set arbitrage on Polymarket US.

The bot scans active markets with exactly two tradable `marketSides`, including
team-side sports markets, and looks for:

```text
buy LONG + buy SHORT + estimated fees <= configured target
```

By default it uses executable order-book pricing:

```text
LONG buy cost  = best offer
SHORT buy cost = 1 - best bid
```

That default is intentionally conservative. Displayed side prices can make a
market look cheap, but they are often derived from the opposite side of the
same book and are not both immediately buyable.

The default target is `0.99` all-in, after estimated taker fees. A raw displayed
or gross sum of `0.99` is not enough if fees push the complete-set cost over
`1.00`.

## Dry Run

```bash
python3 binary_arbitrage/binary_arb.py --once --market-limit 500
```

Sports/team markets are included automatically when they are represented as a
two-sided market. To scan only sports:

```bash
python3 binary_arbitrage/binary_arb.py --once --category sports
```

Continuous mode checks once per second by default and reads market books in
parallel:

```bash
python3 binary_arbitrage/binary_arb.py --market-limit 500 --book-workers 16
```

## Research Mode

`--pricing-mode displayed` compares displayed side quotes instead of executable
book prices. This is useful for diagnosing apparent dislocations, but live
trading is disabled in this mode because it can produce false arbitrage signals.

```bash
python3 binary_arbitrage/binary_arb.py --once --pricing-mode displayed
```

## Live Mode

Live mode requires the Polymarket US SDK and API credentials:

```bash
python3 -m pip install polymarket-us
export POLYMARKET_KEY_ID="..."
export POLYMARKET_SECRET_KEY="..."
python3 binary_arbitrage/binary_arb.py --live
```

The script submits the long and short legs concurrently, but it does not yet
manage partial-fill exits. It cancels tracked live orders after 2 seconds by
default:

```bash
python3 binary_arbitrage/binary_arb.py --live --max-order-age-seconds 2
```

Keep `--trade-dollars` small until private order and position streams are added.
