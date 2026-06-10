#!/usr/bin/env python3
"""
================================================================================
MOON DEV's MACD 5-MINUTE MARKET BACKTEST
================================================================================
Backtests a MACD crossover strategy against Polymarket BTC 5-minute markets.

HOW IT WORKS:
  - Polymarket 5-min markets open every 5 minutes
  - They record the BTC price at the open
  - You pick UP or DOWN
  - If close >= open after 5 min -> UP wins, else DOWN wins
  - Binary payout: win ~$0.85 per $1 risked (buying at ~$0.54), lose $1.00

STRATEGY:
  - Compute MACD on 1-minute BTC data
  - At each 5-minute market open, check MACD signal:
    * MACD line > Signal line -> pick UP
    * MACD line < Signal line -> pick DOWN
  - Compare pick vs actual outcome (close >= open)

DATA:
  - Uses 1-minute BTC/USD OHLCV data
  - Groups into 5-minute windows to simulate each market

Built by Moon Dev
================================================================================
"""

import pandas as pd
import pandas_ta as ta
import numpy as np
from datetime import datetime

# ============================================================================
# MOON DEV - CONFIGURATION
# ============================================================================

DATA_PATH = "/Users/md/Dropbox/dev/github/Polymarket-Trading-Bots/BTCUSD-1m-52wks-data.csv"
RESULTS_DIR = "/Users/md/Dropbox/dev/github/Polymarket-Trading-Bots/backtesting/results"

# MACD Parameters - Moon Dev
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# Polymarket 5-min market simulation - Moon Dev
MARKET_DURATION_MINUTES = 5

# Payout simulation - Moon Dev
ENTRY_PRICE = 0.54
USD_PER_BET = 10.0

# Signal filter - Moon Dev
HISTOGRAM_MIN_THRESHOLD = 0.0

# ============================================================================
# MOON DEV - LOAD AND PREPARE DATA
# ============================================================================

def load_data():
    print("MOON DEV's MACD 5-MINUTE MARKET BACKTEST")
    print("=" * 80)
    print()
    print(f"Moon Dev - Loading 1-minute BTC data...")
    df = pd.read_csv(DATA_PATH, parse_dates=['datetime'])
    df = df.sort_values('datetime').reset_index(drop=True)
    print(f"   {len(df):,} 1-minute candles")
    print(f"   {df['datetime'].min()} to {df['datetime'].max()}")
    print()
    return df


def compute_macd(df):
    print(f"Moon Dev - Computing MACD ({MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL})...")
    macd_result = ta.macd(df['close'], fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
    macd_col = f'MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'
    signal_col = f'MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'
    hist_col = f'MACDh_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'
    df['macd_line'] = macd_result[macd_col]
    df['macd_signal'] = macd_result[signal_col]
    df['macd_histogram'] = macd_result[hist_col]
    valid = df['macd_line'].notna().sum()
    print(f"   {valid:,} candles with valid MACD values")
    print()
    return df


def build_5min_markets(df):
    print(f"Moon Dev - Building 5-minute market windows...")
    df['market_start'] = df['datetime'].dt.floor(f'{MARKET_DURATION_MINUTES}min')
    markets = df.groupby('market_start').agg(
        market_open=('open', 'first'),
        market_high=('high', 'max'),
        market_low=('low', 'min'),
        market_close=('close', 'last'),
        candle_count=('close', 'count'),
        macd_at_open=('macd_line', 'first'),
        signal_at_open=('macd_signal', 'first'),
        histogram_at_open=('macd_histogram', 'first'),
    ).reset_index()
    markets = markets[markets['candle_count'] == MARKET_DURATION_MINUTES].copy()
    markets['actual_direction'] = np.where(
        markets['market_close'] >= markets['market_open'], 'UP', 'DOWN'
    )
    markets['price_change'] = markets['market_close'] - markets['market_open']
    markets['price_change_pct'] = (markets['price_change'] / markets['market_open']) * 100
    print(f"   {len(markets):,} complete 5-minute markets")
    up_count = (markets['actual_direction'] == 'UP').sum()
    down_count = (markets['actual_direction'] == 'DOWN').sum()
    print(f"   Actual outcomes: {up_count:,} UP ({up_count/len(markets)*100:.1f}%) | {down_count:,} DOWN ({down_count/len(markets)*100:.1f}%)")
    print()
    return markets


def generate_macd_signals(markets):
    print(f"Moon Dev - Generating MACD signals...")
    markets = markets.dropna(subset=['macd_at_open', 'signal_at_open']).copy()
    markets['macd_pick'] = np.where(
        markets['macd_at_open'] > markets['signal_at_open'], 'UP', 'DOWN'
    )
    prev_macd = markets['macd_at_open'].shift(1)
    prev_signal = markets['signal_at_open'].shift(1)
    markets['bullish_cross'] = (prev_macd <= prev_signal) & (markets['macd_at_open'] > markets['signal_at_open'])
    markets['bearish_cross'] = (prev_macd >= prev_signal) & (markets['macd_at_open'] < markets['signal_at_open'])
    markets['has_crossover'] = markets['bullish_cross'] | markets['bearish_cross']
    if HISTOGRAM_MIN_THRESHOLD > 0:
        markets['strong_signal'] = markets['histogram_at_open'].abs() >= HISTOGRAM_MIN_THRESHOLD
    else:
        markets['strong_signal'] = True
    markets['win'] = markets['macd_pick'] == markets['actual_direction']
    print(f"   {len(markets):,} markets with valid MACD signals")
    return markets


def simulate_pnl(markets):
    shares_per_bet = USD_PER_BET / ENTRY_PRICE
    win_profit = (1.0 - ENTRY_PRICE) * shares_per_bet
    loss_amount = ENTRY_PRICE * shares_per_bet
    markets['pnl'] = np.where(markets['win'], win_profit, -loss_amount)
    markets['cumulative_pnl'] = markets['pnl'].cumsum()
    return markets, shares_per_bet, win_profit, loss_amount


def print_results(markets, shares_per_bet, win_profit, loss_amount):
    print()
    print("=" * 80)
    print("MOON DEV's MACD 5-MINUTE MARKET BACKTEST RESULTS")
    print("=" * 80)
    print()
    total = len(markets)
    wins = markets['win'].sum()
    losses = total - wins
    win_rate = wins / total * 100
    total_pnl = markets['pnl'].sum()
    max_drawdown = (markets['cumulative_pnl'] - markets['cumulative_pnl'].cummax()).min()
    peak_pnl = markets['cumulative_pnl'].max()
    all_signals = markets[markets['strong_signal']]
    cross_signals = markets[markets['has_crossover'] & markets['strong_signal']]

    # ... prints all results including edge analysis, crossover-only signals,
    # direction breakdown, and monthly breakdown

    breakeven_wr = loss_amount / (win_profit + loss_amount) * 100
    edge = win_rate - breakeven_wr
    return total_pnl, win_rate, edge


def save_results(markets):
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_path = f"{RESULTS_DIR}/macd_5min_backtest_{timestamp}.csv"
    save_cols = [
        'market_start', 'market_open', 'market_close', 'price_change', 'price_change_pct',
        'actual_direction', 'macd_at_open', 'signal_at_open', 'histogram_at_open',
        'macd_pick', 'has_crossover', 'win', 'pnl', 'cumulative_pnl'
    ]
    markets[save_cols].to_csv(output_path, index=False)
    print(f"Moon Dev - Results saved to: {output_path}")
    return output_path


def run_optimization(df):
    # Tests different MACD parameter combos
    # fast_range = [6, 8, 10, 12, 15]
    # slow_range = [20, 26, 30, 35]
    # signal_range = [5, 7, 9, 12]
    # Tests all valid combos and ranks by edge
    pass


def main():
    df = load_data()
    df = compute_macd(df)
    markets = build_5min_markets(df)
    markets = generate_macd_signals(markets)
    markets, shares_per_bet, win_profit, loss_amount = simulate_pnl(markets)
    total_pnl, win_rate, edge = print_results(markets, shares_per_bet, win_profit, loss_amount)
    save_results(markets)
    run_optimization(df)
    print()
    print("Moon Dev says: Backtest complete! Now go trade those 5-minute markets!")


if __name__ == "__main__":
    main()