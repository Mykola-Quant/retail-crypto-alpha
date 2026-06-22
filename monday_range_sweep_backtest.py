#!/usr/bin/env python3
"""
monday_range_sweep_validation.py
Розширений бектест з train/test та підбором порогу.
"""

import os
import time
import numpy as np
import pandas as pd
from scipy import stats

# Конфігурація
SYMBOL = "BTC/USDT"
EXCHANGE = "binanceusdm"
START = "2023-01-01"
END = "2026-06-19"
CACHE_FILE = "cache/BTC_USDT_2023_2026.parquet"
TRAIN_END = "2025-12-31"
COST = 0.0013

# Завантаження даних
if not os.path.exists(CACHE_FILE):
    import ccxt
    ex = getattr(ccxt, EXCHANGE)({"enableRateLimit": True})
    since = ex.parse8601(START + "T00:00:00Z")
    end_ms = ex.parse8601(END + "T23:59:59Z")
    tf_ms = ex.parse_timeframe("5m") * 1000
    limit = 1000
    rows = []
    while since < end_ms:
        batch = ex.fetch_ohlcv(SYMBOL, "5m", since=since, limit=limit)
        if not batch:
            break
        rows += batch
        since = batch[-1][0] + tf_ms
        time.sleep(ex.rateLimit / 1000)
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("dt")[["open", "high", "low", "close", "volume"]]
    df.to_parquet(CACHE_FILE)
else:
    df = pd.read_parquet(CACHE_FILE)

df["date"] = df.index.date
df["dow"] = df.index.dayofweek

def backtest_monday_range(df, threshold, train_end=None):
    if train_end:
        df = df[df.index <= train_end]
    mondays = df[df["dow"] == 0]
    tuesdays = df[df["dow"] == 1]
    trades = []
    for mon_date, mon_day in mondays.groupby("date"):
        tue_date = mon_date + pd.Timedelta(days=1)
        if tue_date not in tuesdays["date"].values:
            continue
        tue_day = tuesdays[tuesdays["date"] == tue_date]
        mon_high = mon_day["high"].max()
        mon_low = mon_day["low"].min()

        # Sweep high -> short
        sweep_high = tue_day[tue_day["high"] > mon_high * (1 + threshold)]
        for idx, bar in sweep_high.iterrows():
            if bar["close"] < mon_high:
                entry = bar["close"]
                target = mon_low
                exit_bars = tue_day.loc[idx:][tue_day.loc[idx:]["low"] <= target]
                exit_px = target if not exit_bars.empty else tue_day.iloc[-1]["close"]
                gross = (entry - exit_px) / entry
                trades.append({"date": tue_date, "net": gross - COST})
                break

        # Sweep low -> long
        sweep_low = tue_day[tue_day["low"] < mon_low * (1 - threshold)]
        for idx, bar in sweep_low.iterrows():
            if bar["close"] > mon_low:
                entry = bar["close"]
                target = mon_high
                exit_bars = tue_day.loc[idx:][tue_day.loc[idx:]["high"] >= target]
                exit_px = target if not exit_bars.empty else tue_day.iloc[-1]["close"]
                gross = (exit_px - entry) / entry
                trades.append({"date": tue_date, "net": gross - COST})
                break
    return pd.DataFrame(trades)

# Перебір порогів на тренувальному періоді
print("Підбір порогу на train (2023–2025)...")
best_threshold = None
best_net = -np.inf
results = []
for th in np.arange(0.0005, 0.0051, 0.0005):
    train_trades = backtest_monday_range(df, th, TRAIN_END)
    if len(train_trades) < 10:
        continue
    net_mean = train_trades["net"].mean()
    results.append((th, len(train_trades), net_mean, (train_trades["net"]>0).mean()))
    if net_mean > best_net:
        best_net = net_mean
        best_threshold = th

print("Train результати:")
for r in sorted(results, key=lambda x: x[2], reverse=True)[:5]:
    print(f"  Threshold={r[0]:.4f}, Trades={r[1]}, Net={r[2]:.4f}, Win={r[3]:.2%}")

if best_threshold is None:
    print("Не знайдено підходящого порогу.")
    exit()

print(f"\nНайкращий поріг: {best_threshold:.4f}")

# Тест на test (2026)
print("Тест на test (2026)...")
test_trades = backtest_monday_range(df, best_threshold)
test_trades = test_trades[test_trades["date"] >= pd.Timestamp("2026-01-01").date()]
if len(test_trades) == 0:
    print("Немає угод в тестовому періоді.")
else:
    net_mean = test_trades["net"].mean()
    win_rate = (test_trades["net"] > 0).mean()
    t_stat, p_value = stats.ttest_1samp(test_trades["net"], 0)
    print(f"Test угод: {len(test_trades)}")
    print(f"Net/угоду: {net_mean:.4f}")
    print(f"Win rate: {win_rate:.2%}")
    print(f"t-статистика: {t_stat:.3f}, p-value: {p_value:.4f}")
    if p_value < 0.05:
        print("Результат статистично значущий на рівні 5%.")
    else:
        print("Результат НЕ значущий — ймовірно, випадковий.")
