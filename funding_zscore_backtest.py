#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
funding_zscore_backtest.py
==========================
Гіпотеза #2: РІВЕНЬ funding як z-score -> предиктор розвороту (fade-the-crowd).

Напрям ЗАФІКСОВАНИЙ НАПЕРЕД економікою, не підбирається з test:
  funding_z >  +thr  => натовп перевантажений у лонг (платить) => СИГНАЛ SHORT (-1)
  funding_z <  -thr  => натовп перевантажений у шорт          => СИГНАЛ LONG  (+1)
Це принципово: ми не дивимось у test, куди торгувати. Якщо вийде мінус —
гіпотеза мертва, а не "інвертуй".

Чесність: каузальна z-нормалізація (без зазирання вперед), поріг тільки з TRAIN,
forward-return net of costs, n>=30 гейт, bootstrap-CI, permutation p, свіп костів.

Дані (з fetch_funding.py):
  funding_btc.parquet   -> [ts(ms), fundingRate]
  klines_8h_btc.parquet -> [ts(ms), open..close..volume]

Запуск:
  python funding_zscore_backtest.py --horizon 3 --zwindow 30 --quantile 0.90
  (horizon у періодах funding: 1=8h, 3=24h, 9=3доби)
"""
from __future__ import annotations
import argparse, sys
from dataclasses import dataclass
import numpy as np
import pandas as pd


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


def load_aligned(funding_path, klines_path) -> pd.DataFrame:
    f = pd.read_parquet(funding_path)
    k = pd.read_parquet(klines_path)
    f["t"] = pd.to_datetime(f["ts"], unit="ms")
    k["t"] = pd.to_datetime(k["ts"], unit="ms")
    f = f.set_index("t").sort_index()
    k = k.set_index("t").sort_index()
    # funding моменти (00/08/16 UTC) мають збігатися з 8h-клайнами;
    # merge_asof nearest у межах 1 год страхує від дрібних зсувів.
    df = pd.merge_asof(f[["fundingRate"]], k[["close"]],
                       left_index=True, right_index=True,
                       direction="nearest", tolerance=pd.Timedelta("1h"))
    return df.dropna()


def run_backtest(df, horizon, z_window, quantile, train_frac, fees_bps, slip_bps):
    d = df.copy()
    d["fz"] = causal_zscore(d["fundingRate"], z_window)
    cost = (fees_bps + slip_bps) / 1e4
    raw_fwd = d["close"].shift(-horizon) / d["close"] - 1.0
    d["raw_fwd"] = raw_fwd
    d = d.dropna(subset=["fz", "raw_fwd"])
    if len(d) < 50:
        return {"error": f"замало funding-періодів після очистки: {len(d)} "
                f"(q/window зʼїли вибірку — це знак, що історії мало)"}

    cut = int(len(d) * train_frac)
    train, test = d.iloc[:cut], d.iloc[cut:]
    thr = float(train["fz"].abs().quantile(quantile))  # поріг |z| тільки з TRAIN

    def events(sub):
        hi = sub["fz"] > thr      # натовп у лонг -> ми шортимо
        lo = sub["fz"] < -thr     # натовп у шорт -> ми лонгуємо
        sig = np.where(hi, -1.0, np.where(lo, 1.0, 0.0))
        m = sig != 0
        strat = sig[m] * sub["raw_fwd"].to_numpy()[m] - cost  # net of costs
        yrs = (sub.index[-1] - sub.index[0]) / pd.Timedelta("365D")
        ppy = (m.sum() / yrs) if yrs > 0 else np.nan
        return strat, ppy, int((sig == -1).sum()), int((sig == 1).sum())

    tr_s, tr_p, _, _ = events(train)
    te_s, te_p, n_short, n_long = events(test)
    return {"threshold": thr, "n_periods": len(d),
            "period_dt": d.index.to_series().diff().median(),
            "baseline": float(d["raw_fwd"].mean()),
            "test_n_short": n_short, "test_n_long": n_long,
            "train": evaluate(tr_s, tr_p), "test": evaluate(te_s, te_p)}


def cost_sweep(df, **kw):
    rows = []
    for c in [2, 5, 10, 20]:
        k = dict(kw); k["fees_bps"] = c; k["slip_bps"] = 0.0
        res = run_backtest(df, **k)
        if "error" in res:
            continue
        t = res["test"]
        rows.append({"rt_bps": c, "n_test": t.n, "mean": t.mean_ret,
                     "hit": t.hit_rate, "sharpe": t.sharpe_annual, "perm_p": t.perm_p})
    return pd.DataFrame(rows)


def report(res, sweep):
    if "error" in res:
        print("ПОМИЛКА:", res["error"]); return
    print("=" * 66)
    print(f"Funding-періодів: {res['n_periods']} | крок: {res['period_dt']}")
    print(f"Поріг |funding_z| (з TRAIN): {res['threshold']:.3f}")
    print(f"baseline (середній forward по всіх): {res['baseline']*100:+.3f}%")
    print(f"TEST події: short={res['test_n_short']}  long={res['test_n_long']}")
    for nm in ("train", "test"):
        r = res[nm]
        flag = "  <-- n<30, ВИСНОВОК НЕМОЖЛИВИЙ" if 0 < r.n < 30 else ""
        print("-" * 66); print(f"[{nm.upper()}] n={r.n}{flag}")
        if r.n == 0:
            print("  нуль подій"); continue
        print(f"  mean {r.mean_ret*100:+.3f}%  median {r.median_ret*100:+.3f}%  "
              f"hit {r.hit_rate*100:.1f}%")
        print(f"  Sharpe(annual) {r.sharpe_annual:.2f}  "
              f"CI95 [{r.boot_lo*100:+.3f}%, {r.boot_hi*100:+.3f}%]  "
              f"perm_p {r.perm_p:.4f}")
    print("-" * 66); print("COST SENSITIVITY (test):")
    print("  (мало подій)" if sweep.empty
          else sweep.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("=" * 66)
    print("edge реальний ЛИШЕ якщо TEST: n>=30, CI не перетинає 0, perm_p<~0.01,")
    print("Sharpe тримається на 10+ bps. Напрям зафіксований — інверсію не чіпаємо.")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--funding", default="funding_btc.parquet")
    ap.add_argument("--klines", default="klines_8h_btc.parquet")
    ap.add_argument("--horizon", type=int, default=3, help="періодів funding (3=24h)")
    ap.add_argument("--zwindow", type=int, default=30, help="вікно z-score у періодах")
    ap.add_argument("--quantile", type=float, default=0.90)
    ap.add_argument("--train", type=float, default=0.6)
    ap.add_argument("--fees", type=float, default=4.0)
    ap.add_argument("--slip", type=float, default=2.0)
    a = ap.parse_args(argv)

    df = load_aligned(a.funding, a.klines)
    print(f"Вирівняно funding+price: {len(df)} періодів "
          f"{df.index.min()}..{df.index.max()}")
    kw = dict(horizon=a.horizon, z_window=a.zwindow, quantile=a.quantile,
              train_frac=a.train, fees_bps=a.fees, slip_bps=a.slip)
    res = run_backtest(df, **kw)
    sweep = cost_sweep(df, horizon=a.horizon, z_window=a.zwindow,
                       quantile=a.quantile, train_frac=a.train)
    report(res, sweep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
