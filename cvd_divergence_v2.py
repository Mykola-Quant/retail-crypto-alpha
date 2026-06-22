#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cvd_divergence_v2.py
====================
Той самий чесний бектест гіпотези #4 (CVD-дивергенція спот vs perp), але з
ВБУДОВАНИМ робастним інжестом сирих Binance aggTrades CSV. Прибирає весь клас
багів, на яких ти застряг: рядок-заголовок, зсув колонок, мкс-vs-мс,
переповнення памʼяті на 180М рядків.

Чому це працює надійно:
  * читання ЗА ПОЗИЦІЄЮ колонки (price=1, qty=2, timestamp=5, is_buyer_maker=6)
    — однаково для спот (8 колонок) і perp (7 колонок), тож одна логіка на обидва;
  * рядок-заголовок викидається автоматично (нечислові ts -> NaN -> drop);
  * мкс/мс визначається автоматично по величині (>1e14 => мікросекунди => //1000);
  * НЕ тримає тіки в памʼяті: кожен денний файл -> бари -> кеш (крихітний parquet).

ВИКОРИСТАННЯ
-----------
1) Зібрати бари з CSV-папок (повільно один раз, далі кешується):
   python cvd_divergence_v2.py \
       --spot-dir spot_downloads --perp-dir perp_downloads \
       --bar 1h --horizon 24 --quantile 0.90

2) Перезапуск зі зміненими параметрами — миттєвий (читає кеш барів):
   python cvd_divergence_v2.py --bar 1h --horizon 12 --quantile 0.85

3) Форсувати перезбір барів (якщо змінив --bar або дані):
   ... додай --rebuild

ПРИМІТКА ПРО ГОРИЗОНТ І n
-------------------------
112 днів даних. На --bar 1D квантиль 0.90 дасть ~11 подій усього -> на test
лишиться ~4 -> НИЖЧЕ n>=30, висновок неможливий. Щоб мати статистику з 3.5
місяців, тримай бар годинним (1h/2h): 1h => ~2700 барів => досить подій.
АЛЕ памʼятай: на хвилинах це мертвий flow; 1h-divergence — це вже про
акумуляцію/позиціонування, легітимно, але читай результат критично.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

# Позиції колонок у Binance aggTrades CSV (спот і perp однакові на 1,2,5,6):
#   0 agg_trade_id | 1 price | 2 qty | 3 first_id | 4 last_id |
#   5 transact_time | 6 is_buyer_maker | (7 is_best_match — лише спот)
USECOLS = [1, 2, 5, 6]
NAMES = ["price", "qty", "ts", "ibm"]
US_THRESHOLD = 1e14   # ts > цього => мікросекунди (2026 рік: мс~1.77e12, мкс~1.77e15)


# ============================================================================
# ІНЖЕСТ
# ============================================================================
def _norm_freq(bar: str) -> str:
    from pandas.tseries.frequencies import to_offset
    for cand in (bar, bar.replace("H", "h").replace("T", "min"),
                 bar.replace("h", "H").replace("min", "T")):
        try:
            to_offset(cand); return cand
        except (ValueError, KeyError):
            continue
    return bar


def read_aggtrades_csv(path: str) -> pd.DataFrame:
    """
    Один денний CSV -> DataFrame[index=час, price, qty, signed_qty].
    Робастно до заголовка, зсуву колонок і одиниць часу.
    """
    raw = pd.read_csv(path, header=None, usecols=USECOLS, names=NAMES,
                      low_memory=False)
    ts = pd.to_numeric(raw["ts"], errors="coerce")      # заголовок -> NaN
    ok = ts.notna()
    if ok.sum() == 0:
        raise ValueError(f"{path}: жодного числового timestamp у колонці 5 "
                         f"(перевір роздільник/позиції колонок)")
    raw, ts = raw[ok], ts[ok].astype("int64")

    # автодетект одиниць часу по медіані (стійко до сміття/заголовка).
    # 2026: секунди~1.77e9 (10 цифр), мс~1.77e12 (13), мкс~1.77e15 (16).
    med = float(ts.median())
    if med > 1e14:        # мікросекунди -> мс
        ts = ts // 1000
    elif med < 1e11:      # секунди -> мс  (саме цей кейс зламав твій perp)
        ts = ts * 1000
    # інакше вже мілісекунди — лишаємо як є

    price = pd.to_numeric(raw["price"], errors="coerce").to_numpy()
    qty = pd.to_numeric(raw["qty"], errors="coerce").to_numpy()
    ibm = raw["ibm"].astype(str).str.strip().str.lower().eq("true").to_numpy()
    # is_buyer_maker == True => агресор ПРОДАВЕЦЬ => -qty
    signed = qty * np.where(ibm, -1.0, 1.0)

    df = pd.DataFrame({"price": price, "qty": qty, "signed_qty": signed},
                      index=pd.to_datetime(ts.to_numpy(), unit="ms"))
    df = df[(df["price"] > 0) & np.isfinite(df["qty"])]
    return df.sort_index()


def build_bars(csv_dir: str, bar: str) -> pd.DataFrame:
    """Зводить усі денні CSV у папці в бари. Кожен файл = один UTC-день,
    тож бари, що ділять добу (1h..1D), не перетинають межі файлів."""
    files = sorted(glob.glob(os.path.join(csv_dir, "*.csv")))
    if not files:
        raise FileNotFoundError(f"нема *.csv у {csv_dir}")
    freq = _norm_freq(bar)
    parts = []
    for i, f in enumerate(files, 1):
        d = read_aggtrades_csv(f)
        g = d.resample(freq)
        b = pd.DataFrame({
            "open": g["price"].first(), "high": g["price"].max(),
            "low": g["price"].min(), "close": g["price"].last(),
            "volume": g["qty"].sum(), "delta": g["signed_qty"].sum(),
        }).dropna(subset=["close"])
        parts.append(b)
        print(f"  [{i}/{len(files)}] {os.path.basename(f)}: {len(b)} барів", flush=True)
    bars = pd.concat(parts).sort_index()
    bars = bars[~bars.index.duplicated(keep="first")]
    return bars


def get_bars(csv_dir: str | None, cache: str, bar: str, rebuild: bool) -> pd.DataFrame:
    if (not rebuild) and os.path.exists(cache):
        b = pd.read_parquet(cache)
        print(f"  кеш: {cache} ({len(b)} барів)")
        return b
    if not csv_dir:
        raise FileNotFoundError(f"нема кешу {cache} і не задано --*-dir для збору")
    print(f"  збираю бари з {csv_dir} (bar={bar})...")
    b = build_bars(csv_dir, bar)
    b.to_parquet(cache)
    print(f"  -> збережено {cache} ({len(b)} барів)")
    return b


# ============================================================================
# МАТЕМАТИКА БЕКТЕСТУ (портовано з валідованого v1)
# ============================================================================
def causal_zscore(s: pd.Series, window: int) -> pd.Series:
    mu = s.rolling(window, min_periods=window).mean()
    sd = s.rolling(window, min_periods=window).std(ddof=0)
    return (s - mu) / sd.replace(0.0, np.nan)


@dataclass
class BTResult:
    n: int; mean_ret: float; median_ret: float; hit_rate: float
    sharpe_annual: float; boot_lo: float; boot_hi: float; perm_p: float


def evaluate(r: np.ndarray, ppy: float, n_boot=5000, seed=7) -> BTResult:
    r = r[~np.isnan(r)]
    n = len(r)
    if n == 0:
        return BTResult(0, *(np.nan,)*7)
    rng = np.random.default_rng(seed)
    mean, med, hit = float(np.mean(r)), float(np.median(r)), float(np.mean(r > 0))
    sd = np.std(r, ddof=1) if n > 1 else np.nan
    sharpe = (mean/sd)*np.sqrt(ppy) if sd and sd > 0 and np.isfinite(ppy) else np.nan
    boot = np.array([np.mean(rng.choice(r, n, replace=True)) for _ in range(n_boot)])
    lo, hi = np.percentile(boot, [2.5, 97.5])
    signs = rng.choice([-1.0, 1.0], size=(n_boot, n))
    perm_p = float(np.mean((signs*np.abs(r)).mean(axis=1) >= mean))
    return BTResult(n, mean, med, hit, sharpe, float(lo), float(hi), perm_p)


def run_backtest(spot_bars, perp_bars, horizon, z_window, quantile,
                 train_frac, fees_bps, slip_bps) -> dict:
    idx = spot_bars.index.intersection(perp_bars.index)
    df = pd.DataFrame(index=idx)
    df["spot_dz"] = causal_zscore(spot_bars.loc[idx, "delta"], z_window)
    df["perp_dz"] = causal_zscore(perp_bars.loc[idx, "delta"], z_window)
    df["div"] = df["spot_dz"] - df["perp_dz"]
    df["px"] = perp_bars.loc[idx, "close"]
    cost = (fees_bps + slip_bps)/1e4
    df["fwd"] = df["px"].shift(-horizon)/df["px"] - 1.0 - cost
    df = df.dropna(subset=["div", "fwd"])
    if len(df) < 50:
        return {"error": f"замало барів після очистки: {len(df)} "
                f"(спільних барів spot∩perp={len(idx)} — якщо мало, час не вирівняний)"}
    cut = int(len(df)*train_frac)
    train, test = df.iloc[:cut], df.iloc[cut:]
    thr = float(train["div"].quantile(quantile))
    bar_dt = df.index.to_series().diff().median()

    def ev(sub):
        e = sub.loc[sub["div"] > thr, "fwd"].to_numpy()
        yrs = (sub.index[-1]-sub.index[0])/pd.Timedelta("365D")
        return e, (len(e)/yrs if yrs > 0 else np.nan)

    tr_e, tr_p = ev(train); te_e, te_p = ev(test)
    return {"threshold": thr, "n_bars": len(df), "bar_dt": bar_dt,
            "n_common": len(idx), "baseline": float(df["fwd"].mean()),
            "train": evaluate(tr_e, tr_p), "test": evaluate(te_e, te_p)}


def cost_sweep(spot_bars, perp_bars, **kw) -> pd.DataFrame:
    rows = []
    for c in [2, 5, 10, 20]:
        k = dict(kw); k["fees_bps"] = c; k["slip_bps"] = 0.0
        res = run_backtest(spot_bars, perp_bars, **k)
        if "error" in res:
            continue
        t = res["test"]
        rows.append({"rt_bps": c, "n_test": t.n, "mean_fwd": t.mean_ret,
                     "hit": t.hit_rate, "sharpe": t.sharpe_annual, "perm_p": t.perm_p})
    return pd.DataFrame(rows)


def report(res, sweep):
    if "error" in res:
        print("ПОМИЛКА:", res["error"]); return
    print("="*66)
    print(f"Спільних барів spot∩perp: {res['n_common']} | крок бару: {res['bar_dt']}")
    print(f"Поріг divergence_z (з TRAIN): {res['threshold']:.3f}")
    print(f"Барів у тесті: {res['n_bars']} | baseline fwd: {res['baseline']*100:+.3f}%")
    for nm in ("train", "test"):
        r = res[nm]
        flag = "  <-- n<30, ВИСНОВОК НЕМОЖЛИВИЙ" if 0 < r.n < 30 else ""
        print("-"*66); print(f"[{nm.upper()}] n={r.n}{flag}")
        if r.n == 0:
            print("  нуль подій (поріг не пробитий на цій підвибірці)"); continue
        print(f"  mean {r.mean_ret*100:+.3f}%  median {r.median_ret*100:+.3f}%  "
              f"hit {r.hit_rate*100:.1f}%")
        print(f"  Sharpe(annual) {r.sharpe_annual:.2f}  "
              f"CI95 [{r.boot_lo*100:+.3f}%, {r.boot_hi*100:+.3f}%]  "
              f"perm_p {r.perm_p:.4f}")
    print("-"*66); print("COST SENSITIVITY (test):")
    print("  (недостатньо подій)" if sweep.empty
          else sweep.to_string(index=False,
               float_format=lambda x: f"{x:.4f}"))
    print("="*66)
    print("edge реальний ЛИШЕ якщо на TEST: n>=30, CI не перетинає 0,")
    print("perm_p<~0.01, Sharpe тримається на 10+ bps. Інакше — викидай.")


# ============================================================================
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--spot-dir"); ap.add_argument("--perp-dir")
    ap.add_argument("--spot-cache", default="spot_bars.parquet")
    ap.add_argument("--perp-cache", default="perp_bars.parquet")
    ap.add_argument("--bar", default="1h")
    ap.add_argument("--horizon", type=int, default=24)
    ap.add_argument("--zwindow", type=int, default=48)
    ap.add_argument("--quantile", type=float, default=0.90)
    ap.add_argument("--train", type=float, default=0.6)
    ap.add_argument("--fees", type=float, default=4.0)
    ap.add_argument("--slip", type=float, default=2.0)
    ap.add_argument("--rebuild", action="store_true")
    a = ap.parse_args(argv)

    # бар у назву кешу, щоб різні бари не змішувались
    sc = a.spot_cache.replace(".parquet", f"_{a.bar}.parquet")
    pc = a.perp_cache.replace(".parquet", f"_{a.bar}.parquet")
    print("Збір/завантаження барів:")
    spot_bars = get_bars(a.spot_dir, sc, a.bar, a.rebuild)
    perp_bars = get_bars(a.perp_dir, pc, a.bar, a.rebuild)
    print(f"spot={len(spot_bars)} барів {spot_bars.index.min()}..{spot_bars.index.max()}")
    print(f"perp={len(perp_bars)} барів {perp_bars.index.min()}..{perp_bars.index.max()}")

    kw = dict(horizon=a.horizon, z_window=a.zwindow, quantile=a.quantile,
              train_frac=a.train, fees_bps=a.fees, slip_bps=a.slip)
    res = run_backtest(spot_bars, perp_bars, **kw)
    sweep = cost_sweep(spot_bars, perp_bars, horizon=a.horizon, z_window=a.zwindow,
                       quantile=a.quantile, train_frac=a.train)
    report(res, sweep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
