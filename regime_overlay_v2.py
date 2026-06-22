#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
regime_overlay_v2.py
====================
Чесна оцінка режимних/ризикових оверлеїв ПРОТИ buy-and-hold, по кожному активу.

Філософія (без перефіту):
  * параметри ЗАФІКСОВАНІ НАПЕРЕД і круглі (SMA=200, vol_target=20%, vol_win=30).
    Нічого не оптимізуємо -> увесь період out-of-sample за побудовою.
  * перевіряємо ПЛАТО: сусідні параметри (SMA 150/200/250, vt 15/20/25%) мають
    давати схожий результат. Якщо «працює» лише на одному значенні — це шум.
  * суворо без look-ahead: позиція на день t вирішується з даних до t-1
    (signal.shift(1)), потім заробляє ретурн дня t.
  * кости на ОБОРОТ: щоденний ребаланс вола-таргета чесно штрафується
    (vol-target не безкоштовний — це його головна прихована ціна).
  * long-only, без плеча: вола-таргет лише ЗНИЖУЄ експозицію (cap=1.0).

Оверлеї:
  BH      — buy & hold (бенчмарк)
  TREND   — лонг коли close>SMA(N), інакше флет (різак лівого хвоста, TSMOM)
  VT      — вола-таргет: позиція = min(1, vt_daily / realized_vol)
  TREND+VT— комбінація

Метрики проти BH: CAGR, Vol, Sharpe, MaxDD, Calmar, %InMarket, Turnover/рік.

Запуск:
  python regime_overlay_v2.py --files btc_daily.csv eth_daily.csv sol_daily.csv \
      xrp_daily.csv trx_daily.csv paxg_daily.csv --cost-bps 13
"""
from __future__ import annotations
import argparse, sys, os
import numpy as np
import pandas as pd

ANN = 252  # торгових днів (крипта 365, але для порівнянь беремо 252-конвенцію;
           # для крипти можна --ann 365, на ранжування це не впливає)


# ----------------------------------------------------------------------------
def load_daily(path: str) -> pd.Series:
    """Повертає Series close, індекс — дата. Авто-детект колонок."""
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    # дата
    date_col = next((cols[k] for k in ("date", "timestamp", "time", "open_time", "datetime")
                     if k in cols), df.columns[0])
    # close
    close_col = next((cols[k] for k in ("close", "close_price", "c", "adj_close")
                      if k in cols), None)
    if close_col is None:
        raise ValueError(f"{path}: не знайшов колонку close (є: {list(df.columns)})")
    ts = df[date_col]
    # дата може бути epoch(s/ms/us/ns) або рядок
    if pd.api.types.is_numeric_dtype(ts):
        med = float(pd.to_numeric(ts, errors="coerce").median())
        unit = ("ns" if med > 1e17 else "us" if med > 1e14
                else "ms" if med > 1e11 else "s")
        idx = pd.to_datetime(ts, unit=unit)
    else:
        idx = pd.to_datetime(ts)
    idx = pd.DatetimeIndex(idx)
    yr = int(idx[len(idx)//2].year)
    if not (2005 <= yr <= 2035):
        raise ValueError(f"{path}: дата парситься у рік {yr} — перевір колонку "
                         f"'{date_col}' / одиницю epoch (медіана року має бути ~2020+)")
    s = pd.Series(pd.to_numeric(df[close_col], errors="coerce").values,
                  index=idx).sort_index()
    return s[s > 0].dropna()


# ----------------------------------------------------------------------------
def positions(close: pd.Series, sma_n: int, vt_annual: float, vol_win: int):
    """Будує позиційні серії для кожного оверлея. Усі — причинні (рішення на t
    з даних до t, далі shift(1) при застосуванні до ретурну)."""
    ret = close.pct_change()
    sma = close.rolling(sma_n, min_periods=sma_n).mean()
    trend = (close > sma).astype(float)                    # 1/0
    rv = ret.rolling(vol_win, min_periods=vol_win).std()   # денна реалізована вола
    vt_daily = vt_annual / np.sqrt(ANN)
    vt = np.minimum(1.0, vt_daily / rv.replace(0.0, np.nan))  # cap 1.0, без плеча
    pos = {
        "BH": pd.Series(1.0, index=close.index),
        "TREND": trend,
        "VT": vt,
        "TREND+VT": trend * vt,
    }
    return ret, pos


def equity_and_metrics(ret: pd.Series, raw_pos: pd.Series, cost_bps: float, ann: int):
    """Причинне застосування (shift 1), кости на оборот, метрики."""
    pos = raw_pos.shift(1).fillna(0.0)            # рішення вчора -> ретурн сьогодні
    c_side = (cost_bps / 2.0) / 1e4
    turn = pos.diff().abs().fillna(pos.abs())     # частка обороту
    strat = pos * ret - turn * c_side
    strat = strat.dropna()
    if len(strat) < 30:
        return None
    eq = (1 + strat).cumprod()
    yrs = (strat.index[-1] - strat.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else np.nan
    vol = strat.std() * np.sqrt(ann)
    sharpe = (strat.mean() * ann) / vol if vol > 0 else np.nan
    dd = (eq / eq.cummax() - 1)
    maxdd = dd.min()
    calmar = cagr / abs(maxdd) if maxdd < 0 else np.nan
    in_mkt = float((pos.abs() > 1e-9).mean())
    turnover_yr = turn.sum() / yrs if yrs > 0 else np.nan
    return {"CAGR": cagr, "Vol": vol, "Sharpe": sharpe, "MaxDD": maxdd,
            "Calmar": calmar, "InMkt": in_mkt, "Turn/yr": turnover_yr}


def run_asset(close, cost_bps, sma_n, vt_annual, vol_win, ann):
    ret, pos = positions(close, sma_n, vt_annual, vol_win)
    out = {}
    for name, p in pos.items():
        m = equity_and_metrics(ret, p, cost_bps, ann)
        if m:
            out[name] = m
    return out


def fmt_table(res: dict) -> str:
    df = pd.DataFrame(res).T
    df = df[["CAGR", "Vol", "Sharpe", "MaxDD", "Calmar", "InMkt", "Turn/yr"]]
    for c in ("CAGR", "Vol", "MaxDD", "InMkt"):
        df[c] = (df[c] * 100).map(lambda x: f"{x:+.1f}%")
    for c in ("Sharpe", "Calmar", "Turn/yr"):
        df[c] = df[c].map(lambda x: f"{x:.2f}")
    return df.to_string()


def plateau(close, cost_bps, vol_win, ann):
    """Перевірка плато: Sharpe і MaxDD комбо TREND+VT по сітці параметрів.
    Шукаємо стабільність, а не пік."""
    rows = []
    for sma_n in (150, 200, 250):
        for vt in (0.15, 0.20, 0.25):
            ret, pos = positions(close, sma_n, vt, vol_win)
            m = equity_and_metrics(ret, pos["TREND+VT"], cost_bps, ann)
            if m:
                rows.append({"SMA": sma_n, "VT": f"{int(vt*100)}%",
                             "Sharpe": round(m["Sharpe"], 2),
                             "MaxDD": f"{m['MaxDD']*100:+.0f}%",
                             "Calmar": round(m["Calmar"], 2)})
    return pd.DataFrame(rows)


def vt_plateau(close, cost_bps, ann):
    """Робастність VT наодинці: сітка vt_annual × vol_win."""
    rows = []
    for vt in (0.15, 0.20, 0.25):
        for win in (20, 30, 40):
            ret, pos = positions(close, sma_n=200, vt_annual=vt, vol_win=win)
            m = equity_and_metrics(ret, pos["VT"], cost_bps, ann)
            if m:
                rows.append({"VT": f"{int(vt*100)}%", "Win": win,
                             "Sharpe": round(m["Sharpe"], 2),
                             "MaxDD": f"{m['MaxDD']*100:+.0f}%",
                             "Calmar": round(m["Calmar"], 2)})
    return pd.DataFrame(rows)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", nargs="+", required=True)
    ap.add_argument("--cost-bps", type=float, default=13.0, help="round-trip bps")
    ap.add_argument("--sma", type=int, default=200)
    ap.add_argument("--vt", type=float, default=0.20, help="ціль річної воли")
    ap.add_argument("--vol-win", type=int, default=30)
    ap.add_argument("--ann", type=int, default=252)
    a = ap.parse_args(argv)

    for path in a.files:
        if not os.path.exists(path):
            print(f"!! нема {path}, пропускаю"); continue
        name = os.path.basename(path).replace("_daily.csv", "").upper()
        try:
            close = load_daily(path)
        except Exception as e:
            print(f"!! {path}: {e}"); continue
        print("\n" + "=" * 78)
        print(f"{name}  | {close.index.min().date()}..{close.index.max().date()} "
              f"| {len(close)} днів | cost={a.cost_bps}bps round-trip")
        res = run_asset(close, a.cost_bps, a.sma, a.vt, a.vol_win, a.ann)
        if not res:
            print("замало даних"); continue
        print(fmt_table(res))
        # читаємо проти BH
        print("-" * 78)
        print(f"Параметри: SMA={a.sma}, vol_target={int(a.vt*100)}%, vol_win={a.vol_win}")
        print("ПЛАТО (TREND+VT по сітці — шукай стабільність, не пік):")
        print(plateau(close, a.cost_bps, a.vol_win, a.ann).to_string(index=False))
        print("\nПЛАТО VT (тільки вола-таргет) — перевірка робастності:")
        print(vt_plateau(close, a.cost_bps, a.ann).to_string(index=False))
    print("\n" + "=" * 78)
    print("ЯК ЧИТАТИ: оверлей вартий холду лише якщо покращує те, що тобі треба,")
    print("РОБАСТНО по плато. 'Плавніше' -> дивись Sharpe/Calmar. 'Виживання' ->")
    print("MaxDD. Якщо TREND просто знижує і CAGR, і ризик пропорційно (Sharpe як у")
    print("BH) — він нічого не дав, лише зменшив експозицію. Це не покращення.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
