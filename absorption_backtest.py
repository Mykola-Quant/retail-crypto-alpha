#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
absorption_backtest.py
======================
Гіпотеза #5: АБСОРБЦІЯ як контекст-фільтр (не таймінг входу).
Теза: важкий агресивний продаж, ПІСЛЯ якого ціна НЕ падає, = великий гравець
поглинає тиск -> зміщення вгору в наступні бари.

Чесність:
  * абсорбція визначена ВСЕРЕДИНІ бару t (без look-ahead):
      - sell-обсяг у верхньому хвості (causal z-score > поріг), І
      - ціна вистояла: close >= open;
    форвардне вікно — строго після t.
  * значущість — проти ДРИФТУ: перестановка проти випадкових барів тієї ж
    кількості (не проти нуля), бо BTC дрейфує вгору і будь-який лонг-фільтр
    виглядав би плюсовим. Питання: абсорбція краща за випадковий бар?
  * поріг тільки з TRAIN, n>=30 гейт, bootstrap-CI, свіп костів.

Це СКРИНІНГ: чи має клас подій додатнє зміщення понад baseline. Навіть якщо
так — фільтр цінний лише як умова поверх базової стратегії (це окремий,
складніший тест на conditional improvement). Standalone-плюс тут — необхідна,
але не достатня умова.

Дані: бар-кеш від cvd_divergence_v2.py (open/high/low/close/volume/delta):
  perp_bars_1h.parquet  (або spot_bars_1h.parquet)

Запуск:
  python absorption_backtest.py --bars perp_bars_1h.parquet --horizon 24 \
      --zwindow 48 --quantile 0.90
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
    sharpe_annual: float; boot_lo: float; boot_hi: float
    baseline: float; excess: float; rand_p: float


def evaluate(event_ret: np.ndarray, pool_ret: np.ndarray, ppy: float,
             n_boot=5000, seed=7) -> BTResult:
    """event_ret — форвардні ретурни подій; pool_ret — форвардні ретурни ВСІХ
    барів підвибірки (для drift-aware перестановки)."""
    r = event_ret[~np.isnan(event_ret)]
    pool = pool_ret[~np.isnan(pool_ret)]
    n = len(r)
    if n == 0:
        return BTResult(0, *(np.nan,)*8)
    rng = np.random.default_rng(seed)
    mean, med, hit = float(np.mean(r)), float(np.median(r)), float(np.mean(r > 0))
    sd = np.std(r, ddof=1) if n > 1 else np.nan
    sharpe = (mean/sd)*np.sqrt(ppy) if sd and sd > 0 and np.isfinite(ppy) else np.nan
    boot = np.array([np.mean(rng.choice(r, n, replace=True)) for _ in range(n_boot)])
    lo, hi = np.percentile(boot, [2.5, 97.5])
    baseline = float(np.mean(pool))
    # drift-aware: чи краще за випадковий набір n барів?
    rand_means = np.array([np.mean(rng.choice(pool, n, replace=True))
                           for _ in range(n_boot)])
    rand_p = float(np.mean(rand_means >= mean))
    return BTResult(n, mean, med, hit, sharpe, float(lo), float(hi),
                    baseline, mean - baseline, rand_p)


def run(bars, horizon, z_window, quantile, train_frac, fees_bps, slip_bps):
    d = bars.copy()
    # відновлюємо агресивний sell-обсяг із delta та volume:
    # delta = buy - sell, volume = buy + sell  =>  sell = (volume - delta)/2
    d["sell_vol"] = (d["volume"] - d["delta"]) / 2.0
    d["sell_z"] = causal_zscore(d["sell_vol"], z_window)
    d["resilient"] = d["close"] >= d["open"]          # ціна вистояла
    cost = (fees_bps + slip_bps) / 1e4
    d["fwd"] = d["close"].shift(-horizon)/d["close"] - 1.0 - cost  # лонг-зміщення
    d = d.dropna(subset=["sell_z", "fwd"])
    if len(d) < 50:
        return {"error": f"замало барів: {len(d)}"}

    cut = int(len(d)*train_frac)
    train, test = d.iloc[:cut], d.iloc[cut:]
    thr = float(train["sell_z"].quantile(quantile))   # поріг з TRAIN

    def ev(sub):
        mask = (sub["sell_z"] > thr) & sub["resilient"]
        er = sub.loc[mask, "fwd"].to_numpy()
        pool = sub["fwd"].to_numpy()
        yrs = (sub.index[-1]-sub.index[0])/pd.Timedelta("365D")
        ppy = (mask.sum()/yrs) if yrs > 0 else np.nan
        return er, pool, ppy

    tr_e, tr_pool, tr_p = ev(train)
    te_e, te_pool, te_p = ev(test)
    return {"threshold": thr, "n_bars": len(d),
            "train": evaluate(tr_e, tr_pool, tr_p),
            "test": evaluate(te_e, te_pool, te_p)}


def cost_sweep(bars, **kw):
    rows = []
    for c in [2, 5, 10, 20]:
        k = dict(kw); k["fees_bps"] = c; k["slip_bps"] = 0.0
        res = run(bars, **k)
        if "error" in res:
            continue
        t = res["test"]
        rows.append({"rt_bps": c, "n_test": t.n, "mean": t.mean_ret,
                     "excess": t.excess, "hit": t.hit_rate, "rand_p": t.rand_p})
    return pd.DataFrame(rows)


def report(res, sweep):
    if "error" in res:
        print("ПОМИЛКА:", res["error"]); return
    print("="*70)
    print(f"Поріг sell_z (з TRAIN): {res['threshold']:.3f} | барів: {res['n_bars']}")
    for nm in ("train", "test"):
        r = res[nm]
        flag = "  <-- n<30, ВИСНОВОК НЕМОЖЛИВИЙ" if 0 < r.n < 30 else ""
        print("-"*70); print(f"[{nm.upper()}] n={r.n}{flag}")
        if r.n == 0:
            print("  нуль подій"); continue
        print(f"  mean {r.mean_ret*100:+.3f}%  baseline {r.baseline*100:+.3f}%  "
              f"EXCESS {r.excess*100:+.3f}%")
        print(f"  median {r.median_ret*100:+.3f}%  hit {r.hit_rate*100:.1f}%  "
              f"Sharpe {r.sharpe_annual:.2f}")
        print(f"  CI95(mean) [{r.boot_lo*100:+.3f}%, {r.boot_hi*100:+.3f}%]  "
              f"rand_p(краще за випадк. бар) {r.rand_p:.4f}")
    print("-"*70); print("COST SENSITIVITY (test):")
    print("  (мало подій)" if sweep.empty
          else sweep.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("="*70)
    print("Фільтр корисний ЛИШЕ якщо TEST: n>=30, EXCESS>0 помітно,")
    print("rand_p<~0.05 (краще за випадковий бар), і це тримається на костах.")
    print("Інакше — це просто дрифт, а не абсорбція.")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", default="perp_bars_1h.parquet")
    ap.add_argument("--horizon", type=int, default=24)
    ap.add_argument("--zwindow", type=int, default=48)
    ap.add_argument("--quantile", type=float, default=0.90)
    ap.add_argument("--train", type=float, default=0.6)
    ap.add_argument("--fees", type=float, default=4.0)
    ap.add_argument("--slip", type=float, default=2.0)
    a = ap.parse_args(argv)

    bars = pd.read_parquet(a.bars).sort_index()
    print(f"Барів: {len(bars)}  {bars.index.min()}..{bars.index.max()}")
    kw = dict(horizon=a.horizon, z_window=a.zwindow, quantile=a.quantile,
              train_frac=a.train, fees_bps=a.fees, slip_bps=a.slip)
    res = run(bars, **kw)
    sweep = cost_sweep(bars, horizon=a.horizon, z_window=a.zwindow,
                       quantile=a.quantile, train_frac=a.train)
    report(res, sweep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
