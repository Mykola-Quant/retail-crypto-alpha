#!/usr/bin/env python3
"""
weekday_effect_analysis.py
==========================
Розширений аналіз ефекту дня тижня для BTC, ETH, SOL.
Завантажує 5m дані за 2025-01-01..2026-06-19, агрегує денні доходи,
проводить t-тести та перевіряє out-of-sample стабільність.
"""

import os
import time
import numpy as np
import pandas as pd
from scipy import stats

# --------------------------- CONFIG ----------------------------------------
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
EXCHANGE = "binanceusdm"
TIMEFRAME = "5m"
START_DATE = "2025-01-01"
END_DATE = "2026-06-19"
CACHE_DIR = "cache"
ROUND_TRIP_COST = 0.0013

# --------------------------- DATA FETCH ------------------------------------
def fetch_ohlcv(symbol, start, end):
    import ccxt
    ex = getattr(ccxt, EXCHANGE)({"enableRateLimit": True})
    tf_ms = ex.parse_timeframe(TIMEFRAME) * 1000
    since = ex.parse8601(start + "T00:00:00Z")
    end_ms = ex.parse8601(end + "T23:59:59Z")
    limit = 1000
    all_rows = []
    print(f"  [fetch] {symbol} ...")
    while since < end_ms:
        try:
            batch = ex.fetch_ohlcv(symbol, TIMEFRAME, since=since, limit=limit)
        except Exception as e:
            print(f"    помилка: {e}")
            break
        if not batch:
            break
        all_rows += batch
        next_since = batch[-1][0] + tf_ms
        if next_since <= since:
            break
        since = next_since
        time.sleep(ex.rateLimit / 1000)
    if not all_rows:
        return None
    df = pd.DataFrame(all_rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("ts").sort_values("ts")
    df = df[df["ts"] <= end_ms]
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("dt")[["open", "high", "low", "close", "volume"]]
    return df

def load_data(symbol):
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(CACHE_DIR, f"{symbol.replace('/','_')}_2025_2026.parquet")
    if os.path.exists(cache_file):
        print(f"  [data] кеш: {cache_file}")
        return pd.read_parquet(cache_file)
    df = fetch_ohlcv(symbol, START_DATE, END_DATE)
    if df is not None:
        df.to_parquet(cache_file)
        print(f"  [data] збережено {len(df)} свічок -> {cache_file}")
    return df

# --------------------------- ANALYSIS --------------------------------------
def weekday_analysis(df_5m, symbol):
    # Денна агрегація
    df_day = df_5m.resample("D").agg({"open": "first", "high": "max",
                                      "low": "min", "close": "last"}).dropna()
    df_day["dow"] = df_day.index.dayofweek  # 0=Mon..6=Sun
    df_day["year"] = df_day.index.year

    # Розрахунок доходів (long на 1 день)
    entries = df_day["open"].iloc[:-1].values
    exits = df_day["close"].iloc[1:].values
    gross = exits / entries - 1.0
    net = gross - ROUND_TRIP_COST
    dow_entry = df_day["dow"].iloc[:-1].values
    years = df_day["year"].iloc[:-1].values

    df_res = pd.DataFrame({"entry_dow": dow_entry, "year": years, "net": net})

    # Групування за днем тижня
    dow_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
    summary = df_res.groupby("entry_dow").agg(
        n=("net", "count"),
        mean=("net", "mean"),
        std=("net", "std"),
        win_rate=("net", lambda x: (x > 0).mean())
    ).rename(index=dow_names)

    # t-тести
    midweek_mask = df_res["entry_dow"].isin([1, 2, 3])  # Tue, Wed, Thu
    mon_mask = df_res["entry_dow"] == 0
    weekend_mask = df_res["entry_dow"].isin([5, 6])    # Sat, Sun

    t_mon, p_mon = stats.ttest_ind(df_res.loc[mon_mask, "net"],
                                   df_res.loc[midweek_mask, "net"],
                                   equal_var=False)
    t_weekend, p_weekend = stats.ttest_ind(df_res.loc[weekend_mask, "net"],
                                          df_res.loc[midweek_mask, "net"],
                                          equal_var=False)

    # Train/test по роках
    train_mask = df_res["year"] == 2025
    test_mask = df_res["year"] == 2026
    train_summary = df_res[train_mask].groupby("entry_dow")["net"].agg(["count", "mean", "std"])
    test_summary = df_res[test_mask].groupby("entry_dow")["net"].agg(["count", "mean", "std"])
    train_summary.index = train_summary.index.map(dow_names)
    test_summary.index = test_summary.index.map(dow_names)

    return summary, (t_mon, p_mon), (t_weekend, p_weekend), train_summary, test_summary

def main():
    for symbol in SYMBOLS:
        print(f"\n{'='*60}\n  {symbol}\n{'='*60}")
        df = load_data(symbol)
        if df is None or df.empty:
            print("  Немає даних.")
            continue

        summary, t_mon, t_weekend, train_sum, test_sum = weekday_analysis(df, symbol)

        print("  Середні net-доходи за днем тижня (весь період):")
        print(summary.to_string(float_format=lambda x: f"{x:.4f}"))
        print(f"\n  t-тест Mon vs Tue-Thu: t={t_mon[0]:.3f}, p={t_mon[1]:.4f}")
        print(f"  t-тест Sat-Sun vs Tue-Thu: t={t_weekend[0]:.3f}, p={t_weekend[1]:.4f}")

        print("\n  Train (2025):")
        print(train_sum.to_string(float_format=lambda x: f"{x:.4f}"))
        print("  Test (2026):")
        print(test_sum.to_string(float_format=lambda x: f"{x:.4f}"))

if __name__ == "__main__":
    main()
