#!/usr/bin/env python3
"""
opening_fvg_backtest.py
=======================
Тест тези edgeful ("opening-range 5m FVG лишається unmitigated => напрямок дня
заданий"), перенесеної з NQ на BTCUSDT.

Два НЕЗАЛЕЖНІ шари:

  1. DESCRIPTIVE  -- репліка стату edgeful "% unmitigated". Вимірюється з
                     lookahead на всю сесію. Це ОПИС, НЕ торгівля. Потрібен лише
                     щоб перевірити, чи взагалі ~80% тримається на BTC.

  2. TRADABLE     -- рішення приймається о 10:00 ET лише з доступною на той
                     момент інформацією (без зазирання вперед), вихід на close
                     RTH-сесії, нетто round-trip-косту. ЦЕ реальний тест тези.

Baselines, без яких 80% нічого не значить:
  - unmitigated rate для FVG у КОНТРОЛЬНОМУ вікні (не на відкритті);
  - безумовний дрейф сесії 10:00->16:00 (бета). Якщо стратегія long-biased і
    BTC просто ріс, "edge" = бета.

Дані: якщо LOCAL_PARQUET вказано і існує -- беремо звідти (твій пайплайн).
Інакше тягнемо 5m через ccxt і кешуємо в parquet.

Залежності: pandas numpy ccxt pyarrow
    pip install pandas numpy ccxt pyarrow
"""

import os
import time
import numpy as np
import pandas as pd

# ----------------------------- CONFIG ---------------------------------------
SYMBOL          = "BTC/USDT"
EXCHANGE_ID     = "binanceusdm"     # USDT-M perp; "binance" для споту
TIMEFRAME       = "5m"
START           = "2026-03-01"
END             = "2026-06-19"      # включно
TZ              = "America/New_York"

OPEN_START      = (9, 30)           # ET -- початок вікна відкриття
OPEN_END        = (10, 0)           # ET -- кінець вікна (30 хв) і момент рішення
SESSION_END     = (16, 0)           # ET -- горизонт мітигації та вихід угоди
CONTROL_START   = (12, 0)           # ET -- контрольне вікно (для baseline)

WEEKDAYS_ONLY   = True              # Mon-Fri, як в edgeful (для 24/7 BTC спірно)
REQUIRE_ALL3_IN_WINDOW = True       # всі 3 свічки FVG усередині вікна
ROUND_TRIP_COST = 0.0013            # 0.13% за повний цикл -- твій стандарт

LOCAL_PARQUET   = ""                # шлях до власних 5m-даних; "" => fetch
DATA_CACHE      = "btc_5m_cache.parquet"
TRADES_OUT      = "opening_fvg_trades.csv"
SEED            = 42

# --------------------------- DATA LOADING -----------------------------------
def fetch_ohlcv():
    import ccxt
    ex = getattr(ccxt, EXCHANGE_ID)({"enableRateLimit": True})
    tf_ms  = ex.parse_timeframe(TIMEFRAME) * 1000
    since  = ex.parse8601(START + "T00:00:00Z")
    end_ms = ex.parse8601(END   + "T23:59:59Z")
    limit  = 1000                      # реальний cap binanceusdm на запит
    rows = []
    print(f"[fetch] {SYMBOL} {TIMEFRAME} {START}..{END} via {EXCHANGE_ID}")
    while since < end_ms:
        batch = ex.fetch_ohlcv(SYMBOL, TIMEFRAME, since=since, limit=limit)
        if not batch:
            break
        rows += batch
        next_since = batch[-1][0] + tf_ms      # просунутись на 1 свічку вперед
        if next_since <= since:                # немає прогресу -> стоп
            break
        since = next_since
        print(f"  ...{pd.to_datetime(batch[-1][0], unit='ms', utc=True)} "
              f"({len(rows)})")
        time.sleep(ex.rateLimit / 1000)
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("ts").sort_values("ts")
    df = df[df["ts"] <= end_ms]
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("dt")[["open", "high", "low", "close", "volume"]]
    return df


def load_data():
    if LOCAL_PARQUET and os.path.exists(LOCAL_PARQUET):
        print(f"[data] локальний parquet: {LOCAL_PARQUET}")
        df = pd.read_parquet(LOCAL_PARQUET)
        if not isinstance(df.index, pd.DatetimeIndex):
            # очікуємо колонку часу; підлаштуй під свою схему
            tcol = next(c for c in ("dt", "timestamp", "open_time", "time") if c in df.columns)
            df["dt"] = pd.to_datetime(df[tcol], utc=True)
            df = df.set_index("dt")
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df = df[["open", "high", "low", "close", "volume"]].sort_index()
        return df.loc[START:END]
    if os.path.exists(DATA_CACHE):
        print(f"[data] кеш: {DATA_CACHE}")
        return pd.read_parquet(DATA_CACHE)
    df = fetch_ohlcv()
    df.to_parquet(DATA_CACHE)
    print(f"[data] збережено кеш -> {DATA_CACHE} ({len(df)} свічок)")
    return df


# --------------------------- FVG LOGIC --------------------------------------
def detect_fvgs(o, h, l, c, et_min, n):
    """3-свічковий FVG. Повертає список dict з позицією форм-свічки і зоною гепа."""
    out = []
    for i in range(2, n):
        if l[i] > h[i - 2]:      # bullish imbalance
            out.append(dict(kind="bull", pos=i, et_min=et_min[i],
                            gap_low=h[i - 2], gap_high=l[i]))
        elif h[i] < l[i - 2]:    # bearish imbalance
            out.append(dict(kind="bear", pos=i, et_min=et_min[i],
                            gap_low=h[i], gap_high=l[i - 2]))
    return out


def mitig_pos(fvg, c, upto=None):
    """Перша позиція, де свічка ЗАКРИВАЄТЬСЯ наскрізь геп (mitigation by close,
    100% fill). upto -- обмежити сканування (включно) для рішення без lookahead."""
    start = fvg["pos"] + 1
    end = len(c) if upto is None else min(upto + 1, len(c))
    for j in range(start, end):
        if fvg["kind"] == "bull" and c[j] < fvg["gap_low"]:
            return j
        if fvg["kind"] == "bear" and c[j] > fvg["gap_high"]:
            return j
    return None


def in_window(et_min, w_start_min, w_end_min, require_all3):
    """форм-свічка у вікні; require_all3 => i-2 теж >= старту (тобто +10 хв)."""
    lo = w_start_min + 10 if require_all3 else w_start_min
    return lo <= et_min < w_end_min


# ----------------------------- BACKTEST -------------------------------------
def run():
    df = load_data()
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
    entry_min = om_e - 5          # close форм-вікна = момент входу (10:00)
    exit_min  = se_m - 5          # close останньої RTH-свічки (16:00)

    desc = {"open": {"bull": [0, 0], "bear": [0, 0]},      # [unmitig, total]
            "ctrl": {"bull": [0, 0], "bear": [0, 0]}}
    trades = []
    session_drift = []            # безумовний 10:00->16:00 return (бета)
    n_days = n_conflict = n_no_signal = 0

    for date, day in df.groupby("et_date"):
        if WEEKDAYS_ONLY and day["et_dow"].iloc[0] >= 5:
            continue
        sess = day[(day.et_min >= om_s) & (day.et_min < se_m)].sort_index()
        if len(sess) < (se_m - om_s) // 5 - 3:   # неповна сесія
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
        session_drift.append(exit_px / entry_px - 1.0)

        fvgs = detect_fvgs(o, h, l, c, em, n)

        # ---- DESCRIPTIVE: full-session unmitigated rate (lookahead, опис) ----
        for f in fvgs:
            if in_window(f["et_min"], om_s, om_e, REQUIRE_ALL3_IN_WINDOW):
                bucket = desc["open"][f["kind"]]
            elif in_window(f["et_min"], cm_s, cm_e, REQUIRE_ALL3_IN_WINDOW):
                bucket = desc["ctrl"][f["kind"]]
            else:
                continue
            bucket[1] += 1
            if mitig_pos(f, c, upto=None) is None:   # не інвертований до 16:00
                bucket[0] += 1

        # ---- TRADABLE: рішення о 10:00, лише доступна інформація --------------
        win = [f for f in fvgs
               if in_window(f["et_min"], om_s, om_e, REQUIRE_ALL3_IN_WINDOW)
               and f["pos"] <= ent_pos]
        alive_bull = [f for f in win if f["kind"] == "bull"
                      and mitig_pos(f, c, upto=ent_pos) is None]
        alive_bear = [f for f in win if f["kind"] == "bear"
                      and mitig_pos(f, c, upto=ent_pos) is None]

        if alive_bull and alive_bear:
            n_conflict += 1; direction = 0          # суперечливий напрямок
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

    report(desc, trades, session_drift, n_days, n_conflict, n_no_signal)


# ----------------------------- REPORT ---------------------------------------
def pct(x):
    return f"{100*x:5.2f}%"


def rate(pair):
    u, t = pair
    return (u / t if t else float("nan")), t


def boot_ci(x, iters=10000, alpha=0.05):
    if len(x) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(SEED)
    means = x[rng.integers(0, len(x), size=(iters, len(x)))].mean(axis=1)
    return np.percentile(means, [100*alpha/2, 100*(1-alpha/2)])


def report(desc, trades, drift, n_days, n_conflict, n_no_signal):
    print("\n" + "=" * 66)
    print(f"OPENING-RANGE FVG BACKTEST  {SYMBOL}  {START}..{END}")
    print(f"вікно {OPEN_START[0]:02d}:{OPEN_START[1]:02d}-"
          f"{OPEN_END[0]:02d}:{OPEN_END[1]:02d} ET | вихід "
          f"{SESSION_END[0]:02d}:{SESSION_END[1]:02d} ET | cost "
          f"{ROUND_TRIP_COST*100:.2f}% | сесій={n_days}")
    print("=" * 66)

    print("\n[1] DESCRIPTIVE  (lookahead, ОПИС -- не торгівля)")
    print("    реплікація стату edgeful '% unmitigated':")
    for win_name, label in (("open", "ВІКНО ВІДКРИТТЯ"), ("ctrl", "КОНТРОЛЬНЕ ВІКНО")):
        print(f"    {label}:")
        for kind in ("bull", "bear"):
            r, t = rate(desc[win_name][kind])
            print(f"        {kind}: unmitigated {pct(r)}  (n={t})")
    print("    -> якщо open ~= ctrl, то '80%' не є властивістю відкриття,")
    print("       а base rate автокореляції BTC. Це головний baseline.")

    print("\n[2] БЕТА  (безумовний дрейф сесії 10:00->16:00)")
    d = np.array(drift)
    print(f"    mean={pct(d.mean())}  днів={len(d)}  "
          f"частка up-днів={pct((d>0).mean())}")
    print(f"    always-LONG нетто костів: {pct(d.mean()-ROUND_TRIP_COST)}")
    print("    -> якщо стратегія long-biased і це > 0, 'edge' = бета.")

    print("\n[3] TRADABLE  (рішення о 10:00 без lookahead, нетто костів)")
    print(f"    конфліктних днів (bull+bear живі): {n_conflict}")
    print(f"    днів без сигналу:                  {n_no_signal}")
    if not trades:
        print("    угод немає.")
        print("=" * 66); return
    tdf = pd.DataFrame(trades)
    net = tdf["net"].values
    gross = tdf["gross"].values
    n = len(net)
    t_stat = net.mean() / (net.std(ddof=1) / np.sqrt(n)) if n > 1 and net.std() else float("nan")
    lo, hi = boot_ci(net)
    longs = (tdf["dir"] == 1).sum()
    print(f"    угод:           {n}  (long={longs}, short={n-longs})")
    print(f"    win rate:       {pct((net>0).mean())}")
    print(f"    gross/угоду:    {pct(gross.mean())}")
    print(f"    net/угоду:      {pct(net.mean())}")
    print(f"    t-stat (net):   {t_stat:.2f}")
    print(f"    95% boot CI net:[{pct(lo)}, {pct(hi)}]")
    print(f"    сума net:       {pct(net.sum())}")

    if n >= 20:   # грубий train/test за датою (n замалий -- лише орієнтир)
        k = int(n * 0.6)
        tr, te = net[:k], net[k:]
        print(f"    train net/угоду:{pct(tr.mean())} (n={len(tr)})  "
              f"test net/угоду:{pct(te.mean())} (n={len(te)})")
    print(f"\n    n={n}: gate n>=30 {'OK' if n>=30 else 'НЕ ПРОЙДЕНО -- стат недостовірний'}")
    print(f"    деталі -> {TRADES_OUT}")
    tdf.to_csv(TRADES_OUT, index=False)
    print("=" * 66)


if __name__ == "__main__":
    run()
