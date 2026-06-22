#!/usr/bin/env python3
"""
strategy_suite_backtest.py
==========================
Тестує три прості стратегії на BTC, ETH, SOL, XAU, CL (5m OHLCV).
  A. Intraday Momentum: перші 2 год амер. сесії > +0.5% -> long до кінця сесії.
  B. Monday Effect: long від відкриття понеділка до закриття вівторка.
  C. Trend Filter: daily close > SMA200 -> long наступний день.
ПЛЮС аналіз ефекту дня тижня: середній дохід для кожного дня входу (long на 1 день).
Усі результати з урахуванням round-trip cost 0.13%.
"""

import os
import numpy as np
import pandas as pd

# --------------------------- CONFIG ----------------------------------------
INSTRUMENTS = [
    {"symbol": "BTC/USDT", "cache": "btc_5m.parquet"},
    {"symbol": "ETH/USDT", "cache": "eth_5m.parquet"},
    {"symbol": "SOL/USDT", "cache": "sol_5m.parquet"},
    {"symbol": "XAU/USDT", "cache": "xau_5m.parquet"},
    {"symbol": "CL/USDT",  "cache": "cl_5m.parquet"},
]

START          = "2026-03-01"
END            = "2026-06-19"
TZ_UTC         = "UTC"
ROUND_TRIP_COST = 0.0013   # 0.13%
SEED           = 42

# Параметри стратегій
MOMENTUM_SESSION_START_UTC = (13, 30)  # 13:30 UTC (09:30 ET)
MOMENTUM_SESSION_END_UTC   = (20, 0)   # 20:00 UTC (16:00 ET)
MOMENTUM_LOOKBACK_MIN      = 120       # 2 години
MOMENTUM_THRESHOLD         = 0.005     # 0.5%

# --------------------------- DATA LOADING ----------------------------------
def load_data(cache_path):
    if not os.path.exists(cache_path):
        print(f"  [WARN] файл {cache_path} не знайдено")
        return None
    df = pd.read_parquet(cache_path)
    if not isinstance(df.index, pd.DatetimeIndex):
        tcol = next(c for c in ("dt","timestamp","open_time","time") if c in df.columns)
        df["dt"] = pd.to_datetime(df[tcol], utc=True)
        df = df.set_index("dt")
    if df.index.tz is None:
        df.index = df.index.tz_localize(TZ_UTC)
    df = df[["open","high","low","close","volume"]].sort_index()
    return df.loc[START:END]

# --------------------------- STRATEGY A: Intraday Momentum -----------------
def backtest_momentum(df_5m):
    df = df_5m.copy()
    df["time"] = df.index.time
    df["date"] = df.index.date
    start_t = pd.Timestamp("2000-01-01") + pd.Timedelta(hours=MOMENTUM_SESSION_START_UTC[0],
                                                         minutes=MOMENTUM_SESSION_START_UTC[1])
    end_t   = pd.Timestamp("2000-01-01") + pd.Timedelta(hours=MOMENTUM_SESSION_END_UTC[0],
                                                         minutes=MOMENTUM_SESSION_END_UTC[1])
    trades = []
    for date, day in df.groupby("date"):
        sess = day.between_time(start_t.time(), end_t.time())
        if len(sess) < (MOMENTUM_LOOKBACK_MIN//5):
            continue
        moment_entry_time = (pd.Timestamp("2000-01-01") +
                             pd.Timedelta(hours=MOMENTUM_SESSION_START_UTC[0],
                                          minutes=MOMENTUM_SESSION_START_UTC[1]) +
                             pd.Timedelta(minutes=MOMENTUM_LOOKBACK_MIN)).time()
        entry_bars = sess[sess.index.time == moment_entry_time]
        if entry_bars.empty:
            continue
        entry_idx = sess.index.get_loc(entry_bars.index[0])
        entry_px = sess.iloc[entry_idx]["close"]
        exit_px = sess.iloc[-1]["close"]
        start_px = sess.iloc[0]["open"]
        moment_return = (entry_px / start_px) - 1.0
        if abs(moment_return) < MOMENTUM_THRESHOLD:
            continue
        direction = 1 if moment_return > 0 else -1
        gross = direction * (exit_px / entry_px - 1.0)
        net = gross - ROUND_TRIP_COST
        trades.append({"date": date, "dir": direction, "gross": gross, "net": net})
    return pd.DataFrame(trades)

# --------------------------- STRATEGY B: Monday Effect ----------------------
def backtest_monday(df_5m):
    df_day = df_5m.resample("D").agg({"open": "first", "high": "max",
                                      "low": "min", "close": "last"}).dropna()
    df_day["dow"] = df_day.index.dayofweek
    trades = []
    for i in range(len(df_day)-1):
        if df_day["dow"].iloc[i] == 0:  # Monday
            entry_px = df_day["open"].iloc[i]
            if i+1 >= len(df_day) or df_day["dow"].iloc[i+1] != 1:
                continue
            exit_px = df_day["close"].iloc[i+1]
            gross = exit_px / entry_px - 1.0
            net = gross - ROUND_TRIP_COST
            trades.append({"date": df_day.index[i], "gross": gross, "net": net})
    return pd.DataFrame(trades)

# --------------------------- STRATEGY C: Trend Filter (SMA200) -------------
def backtest_trend_filter(df_5m):
    df_day = df_5m.resample("D").agg({"open": "first", "high": "max",
                                      "low": "min", "close": "last"}).dropna()
    df_day["SMA200"] = df_day["close"].rolling(200).mean()
    trades = []
    for i in range(200, len(df_day)-1):
        if df_day["close"].iloc[i] > df_day["SMA200"].iloc[i]:
            entry_px = df_day["open"].iloc[i+1]
            exit_px = df_day["close"].iloc[i+1]
            gross = exit_px / entry_px - 1.0
            net = gross - ROUND_TRIP_COST
            trades.append({"date": df_day.index[i+1], "gross": gross, "net": net})
    return pd.DataFrame(trades)

# ------------------------- WEEKDAY EFFECT ANALYSIS --------------------------
def backtest_weekday_effect(df_5m):
    """Повертає середній дохід для кожного дня входу (long 1 день)."""
    df_day = df_5m.resample("D").agg({"open": "first", "high": "max",
                                      "low": "min", "close": "last"}).dropna()
    df_day["dow"] = df_day.index.dayofweek
    results = []
    for i in range(len(df_day)-1):
        entry_px = df_day["open"].iloc[i]
        exit_px = df_day["close"].iloc[i+1]
        gross = exit_px / entry_px - 1.0
        net = gross - ROUND_TRIP_COST
        results.append({"entry_dow": df_day["dow"].iloc[i],
                        "gross": gross, "net": net})
    df_res = pd.DataFrame(results)
    # Групуємо за днем входу
    summary = df_res.groupby("entry_dow").agg(
        n=("net", "count"),
        net_mean=("net", "mean"),
        win_rate=("net", lambda x: (x > 0).mean())
    )
    dow_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
    summary.index = summary.index.map(dow_names)
    return summary

# --------------------------- MAIN -------------------------------------------
def main():
    all_results = []
    for inst in INSTRUMENTS:
        symbol = inst["symbol"]
        print(f"\n{'='*60}\n  {symbol}\n{'='*60}")
        df = load_data(inst["cache"])
        if df is None or df.empty:
            continue

        # A, B, C
        trA = backtest_momentum(df)
        trB = backtest_monday(df)
        trC = backtest_trend_filter(df)

        for strat_name, trades in [("Momentum", trA), ("Monday", trB), ("Trend", trC)]:
            if trades.empty:
                net_mean = np.nan
                win_rate = np.nan
                n = 0
            else:
                net = trades["net"].values
                net_mean = net.mean()
                win_rate = (net > 0).mean()
                n = len(net)
            all_results.append({"symbol": symbol, "strategy": strat_name,
                                "trades": n, "net_mean": net_mean, "win_rate": win_rate})
            print(f"  {strat_name:10s}: угод={n:3d}, net/угоду={net_mean:+.4f}, win_rate={win_rate:.2%}")

        # Weekday effect
        print("  Monday vs інші дні:")
        wd_eff = backtest_weekday_effect(df)
        print(wd_eff.to_string())
        print()

    # Зведена таблиця стратегій
    tbl = pd.DataFrame(all_results)
    print("\n" + "="*80)
    print("ЗВЕДЕНА ТАБЛИЦЯ СТРАТЕГІЙ")
    print("="*80)
    print(f"{'Інструмент':<12} {'Стратегія':<10} {'Угод':>5} {'Net/угоду':>10} {'Win rate':>9}")
    for _, row in tbl.iterrows():
        print(f"{row['symbol']:<12} {row['strategy']:<10} {int(row['trades']):>5} "
              f"{row['net_mean']:>+10.4f} {row['win_rate']:>9.2%}")
    print("="*80)

if __name__ == "__main__":
    main()
