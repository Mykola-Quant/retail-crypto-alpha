#!/usr/bin/env python3
"""
multi_fvg_backtest.py
=====================
Повний тест opening-range 5m FVG «unmitigated» на BTC, ETH, SOL, XAU, CL.
Використовує Binance USDS-M (binanceusdm) для всіх інструментів.
Для XAU/USDT та CL/USDT вкажіть правильні символи, якщо відрізняються.
"""

import os
import time
import numpy as np
import pandas as pd

# ----------------------------- CONFIG ---------------------------------------
INSTRUMENTS = [
    {
        "symbol": "BTC/USDT",
        "exchange": "binanceusdm",
        "local_file": "",
        "cache": "btc_5m.parquet",
    },
    {
        "symbol": "ETH/USDT",
        "exchange": "binanceusdm",
        "local_file": "",
        "cache": "eth_5m.parquet",
    },
    {
        "symbol": "SOL/USDT",
        "exchange": "binanceusdm",
        "local_file": "",
        "cache": "sol_5m.parquet",
    },
    {
        "symbol": "XAU/USDT",          # або "PAXG/USDT" чи "XAUT/USDT" — золото
        "exchange": "binanceusdm",
        "local_file": "",
        "cache": "xau_5m.parquet",
    },
    {
        "symbol": "CL/USDT",           # або "OIL/USDT" — нафта
        "exchange": "binanceusdm",
        "local_file": "",
        "cache": "cl_5m.parquet",
    },
]

START           = "2026-03-01"
END             = "2026-06-19"
TZ              = "America/New_York"

OPEN_START      = (9, 30)
OPEN_END        = (10, 0)
SESSION_END     = (16, 0)
CONTROL_START   = (12, 0)

WEEKDAYS_ONLY   = True
REQUIRE_ALL3_IN_WINDOW = True
ROUND_TRIP_COST = 0.0013

TRADES_OUT_DIR  = "fvg_trades"
SEED            = 42

# --------------------------- DATA LOADING -----------------------------------
def fetch_crypto(symbol, exchange_id, start, end, cache_path):
    import ccxt
    try:
        ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    except AttributeError:
        print(f"  [fetch] біржа {exchange_id} не підтримується ccxt")
        return None
    tf_ms = ex.parse_timeframe("5m") * 1000
    since = ex.parse8601(start + "T00:00:00Z")
    end_ms = ex.parse8601(end + "T23:59:59Z")
    limit = 1000
    rows = []
    print(f"  [fetch] {symbol} з {exchange_id} ...")
    while since < end_ms:
        try:
            batch = ex.fetch_ohlcv(symbol, "5m", since=since, limit=limit)
        except Exception as e:
            print(f"  [fetch] помилка при отриманні {symbol}: {e}")
            break
        if not batch:
            break
        rows += batch
        next_since = batch[-1][0] + tf_ms
        if next_since <= since:
            break
        since = next_since
        time.sleep(ex.rateLimit / 1000)
    if not rows:
        print(f"  [fetch] немає даних для {symbol}")
        return None
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("ts").sort_values("ts")
    df = df[df["ts"] <= end_ms]
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("dt")[["open", "high", "low", "close", "volume"]]
    if cache_path:
        df.to_parquet(cache_path)
    return df

def load_instrument(cfg):
    # 1. Локальний файл
    if cfg["local_file"] and os.path.exists(cfg["local_file"]):
        print(f"  [data] локальний parquet: {cfg['local_file']}")
        df = pd.read_parquet(cfg["local_file"])
        if not isinstance(df.index, pd.DatetimeIndex):
            tcol = next(c for c in ("dt", "timestamp", "open_time", "time") if c in df.columns)
            df["dt"] = pd.to_datetime(df[tcol], utc=True)
            df = df.set_index("dt")
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df = df[["open", "high", "low", "close", "volume"]].sort_index()
        return df.loc[START:END]

    # 2. Кеш
    if cfg["cache"] and os.path.exists(cfg["cache"]):
        print(f"  [data] кеш: {cfg['cache']}")
        return pd.read_parquet(cfg["cache"])

    # 3. Фетч з біржі
    if cfg["exchange"] in ("binance", "binanceusdm", "bybit", "kucoin"):
        df = fetch_crypto(cfg["symbol"], cfg["exchange"], START, END, cfg["cache"])
        if df is not None and not df.empty:
            print(f"  [data] збережено кеш -> {cfg['cache']} ({len(df)} свічок)")
            return df

    print(f"  [data] НЕ ВДАЛОСЯ завантажити {cfg['symbol']} — пропускаю.")
    return None

# --------------------------- FVG LOGIC --------------------------------------
def detect_fvgs(o, h, l, c, et_min, n):
    out = []
    for i in range(2, n):
        if l[i] > h[i - 2]:
            out.append(dict(kind="bull", pos=i, et_min=et_min[i],
                            gap_low=h[i - 2], gap_high=l[i]))
        elif h[i] < l[i - 2]:
            out.append(dict(kind="bear", pos=i, et_min=et_min[i],
                            gap_low=h[i], gap_high=l[i - 2]))
    return out

def mitig_pos(fvg, c, upto=None):
    start = fvg["pos"] + 1
    end = len(c) if upto is None else min(upto + 1, len(c))
    for j in range(start, end):
        if fvg["kind"] == "bull" and c[j] < fvg["gap_low"]:
            return j
        if fvg["kind"] == "bear" and c[j] > fvg["gap_high"]:
            return j
    return None

def in_window(et_min, w_start_min, w_end_min, require_all3):
    lo = w_start_min + 10 if require_all3 else w_start_min
    return lo <= et_min < w_end_min

# ----------------------------- BACKTEST FOR ONE INSTRUMENT -----------------
def backtest_one(df, symbol):
    et = df.index.tz_convert(TZ)
    df = df.copy()
    df["et_date"] = et.normalize().tz_localize(None)
    df["et_min"]  = et.hour * 60 + et.minute
    df["et_dow"]  = et.dayofweek

    om_s = OPEN_START[0]   * 60 + OPEN_START[1]
    om_e = OPEN_END[0]     * 60 + OPEN_END[1]
    se_m = SESSION_END[0]  * 60 + SESSION_END[1]
    cm_s = CONTROL_START[0]* 60 + CONTROL_START[1]
    cm_e = cm_s + (om_e - om_s)
    entry_min = om_e - 5
    exit_min  = se_m - 5

    desc = {"open": {"bull": [0,0], "bear": [0,0]},
            "ctrl": {"bull": [0,0], "bear": [0,0]}}
    trades = []
    drift_list = []
    n_days = n_conflict = n_no_signal = 0

    for date, day in df.groupby("et_date"):
        if WEEKDAYS_ONLY and day["et_dow"].iloc[0] >= 5:
            continue
        sess = day[(day.et_min >= om_s) & (day.et_min < se_m)].sort_index()
        if len(sess) < (se_m - om_s) // 5 - 3:
            continue
        sess = sess.reset_index(drop=True)
        o = sess["open"].values; h = sess["high"].values
        l = sess["low"].values;  c = sess["close"].values
        em = sess["et_min"].values
        n = len(sess)

        ent = np.where(em == entry_min)[0]
        ext = np.where(em == exit_min)[0]
        if len(ent) == 0 or len(ext) == 0:
            continue
        ent_pos, ext_pos = ent[0], ext[0]
        entry_px, exit_px = c[ent_pos], c[ext_pos]
        n_days += 1
        drift_list.append(exit_px / entry_px - 1.0)

        fvgs = detect_fvgs(o, h, l, c, em, n)

        # Descriptive
        for f in fvgs:
            if in_window(f["et_min"], om_s, om_e, REQUIRE_ALL3_IN_WINDOW):
                bucket = desc["open"][f["kind"]]
            elif in_window(f["et_min"], cm_s, cm_e, REQUIRE_ALL3_IN_WINDOW):
                bucket = desc["ctrl"][f["kind"]]
            else:
                continue
            bucket[1] += 1
            if mitig_pos(f, c, upto=None) is None:
                bucket[0] += 1

        # Tradable
        win = [f for f in fvgs
               if in_window(f["et_min"], om_s, om_e, REQUIRE_ALL3_IN_WINDOW)
               and f["pos"] <= ent_pos]
        alive_bull = [f for f in win if f["kind"] == "bull"
                      and mitig_pos(f, c, upto=ent_pos) is None]
        alive_bear = [f for f in win if f["kind"] == "bear"
                      and mitig_pos(f, c, upto=ent_pos) is None]

        if alive_bull and alive_bear:
            n_conflict += 1; direction = 0
        elif alive_bull:
            direction = +1
        elif alive_bear:
            direction = -1
        else:
            n_no_signal += 1; direction = 0

        if direction != 0:
            gross = direction * (exit_px / entry_px - 1.0)
            net = gross - ROUND_TRIP_COST
            trades.append(dict(date=str(date.date()), dir=direction,
                               n_bull=len(alive_bull), n_bear=len(alive_bear),
                               entry=entry_px, exit=exit_px,
                               gross=gross, net=net))

    # Збираємо статистику
    summary = {}
    for win_name, kind in [("open","bull"),("open","bear"),("ctrl","bull"),("ctrl","bear")]:
        u, t = desc[win_name][kind]
        rate = u/t if t else np.nan
        summary[f"{win_name}_{kind}_rate"] = rate
        summary[f"{win_name}_{kind}_n"] = t

    drift = np.array(drift_list) if drift_list else np.array([np.nan])
    summary["n_sessions"] = n_days
    summary["drift_mean"] = drift.mean()
    summary["drift_up_pct"] = (drift > 0).mean() if len(drift) else np.nan

    if trades:
        tdf = pd.DataFrame(trades)
        net = tdf["net"].values
        gross = tdf["gross"].values
        n = len(net)
        t_stat = net.mean() / (net.std(ddof=1) / np.sqrt(n)) if n>1 and net.std() else np.nan
        lo, hi = np.nan, np.nan
        if n >= 2:
            rng = np.random.default_rng(SEED)
            means = net[rng.integers(0, n, size=(10000, n))].mean(axis=1)
            lo, hi = np.percentile(means, [2.5, 97.5])
        summary["trades"] = n
        summary["long_pct"] = (tdf["dir"]==1).sum() / n
        summary["win_rate"] = (net>0).mean()
        summary["gross_mean"] = gross.mean()
        summary["net_mean"] = net.mean()
        summary["t_stat"] = t_stat
        summary["ci_low"] = lo
        summary["ci_high"] = hi
        summary["net_total"] = net.sum()
        summary["conflict_days"] = n_conflict
        summary["no_signal_days"] = n_no_signal
    else:
        summary["trades"] = 0
        summary["conflict_days"] = n_conflict
        summary["no_signal_days"] = n_no_signal

    # Зберегти угоди
    if not os.path.exists(TRADES_OUT_DIR):
        os.makedirs(TRADES_OUT_DIR)
    if trades:
        tdf.to_csv(f"{TRADES_OUT_DIR}/{symbol.replace('/','_')}_trades.csv", index=False)

    return summary

# ----------------------------- MAIN LOOP ------------------------------------
def main():
    all_results = []
    for cfg in INSTRUMENTS:
        symbol = cfg["symbol"]
        print(f"\n{'='*60}\n  Інструмент: {symbol}\n{'='*60}")
        df = load_instrument(cfg)
        if df is None or df.empty:
            print("  -> немає даних, пропускаю.\n")
            continue
        res = backtest_one(df, symbol)
        res["symbol"] = symbol
        all_results.append(res)

    # Зведена таблиця
    if all_results:
        tbl = pd.DataFrame(all_results)
        tbl = tbl.set_index("symbol")
        print("\n\n" + "="*80)
        print("ЗВЕДЕНА ТАБЛИЦЯ РЕЗУЛЬТАТІВ")
        print("="*80)
        print(f"{'Інструмент':<12} {'Сесій':>4} {'Unmit (bull open)':>18} {'Unmit (bear open)':>18} {'Unmit (bull ctrl)':>18} {'Unmit (bear ctrl)':>18} {'Угод':>5} {'Net/угоду':>10} {'Win rate':>9} {'Бета mean':>10}")
        for sym, row in tbl.iterrows():
            print(f"{sym:<12} {int(row['n_sessions']):>4} "
                  f"{row.get('open_bull_rate', np.nan):>18.2%} "
                  f"{row.get('open_bear_rate', np.nan):>18.2%} "
                  f"{row.get('ctrl_bull_rate', np.nan):>18.2%} "
                  f"{row.get('ctrl_bear_rate', np.nan):>18.2%} "
                  f"{int(row.get('trades',0)):>5} "
                  f"{row.get('net_mean', np.nan):>10.2%} "
                  f"{row.get('win_rate', np.nan):>9.2%} "
                  f"{row.get('drift_mean', np.nan):>10.2%}")
        print("="*80)
        print("\nДетальні звіти по угодах збережено в папку", TRADES_OUT_DIR)
    else:
        print("\nНе вдалося отримати дані для жодного інструменту.")

if __name__ == "__main__":
    main()
