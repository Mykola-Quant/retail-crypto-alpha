#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_risk_signal.py
====================
ДЕННИЙ інструмент контролю експозиції (НЕ сигнал входу, НЕ альфа).
Рахує «яку частку активу тримати» за фіксованими правилами:
  VT       = min(1, target_vol / realized_vol)   (вола-таргет, без плеча)
  TREND    = 1 якщо close > SMA(200), інакше 0
  VT+TREND = VT * TREND

Три режими використання:
  1) Ціль на сьогодні (друк у консоль):
       python daily_risk_signal.py --file btc_daily.csv
  2) Експорт історії для накладання на графік:
       ... --export hist_btc.csv   (+ --chart hist_btc.png)
  3) Раз на добу слати в телеграм (через cron/launchd о 00:05 UTC):
       ... --telegram --token <BOT_TOKEN> --chat <CHAT_ID>

Каденція: запускати ПІСЛЯ закриття денної свічки (00:00 UTC). Сигнал — для
наступного дня. Інтрадей не оновлюється. Сесії/відкриття США нерелевантні.
"""
from __future__ import annotations
import argparse, os, sys
import numpy as np
import pandas as pd

ANN = 365  # крипта торгує щодня


# --- читач денних CSV (робастний, як у regime_overlay_v2) -------------------
def load_daily(path: str) -> pd.Series:
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    date_col = next((cols[k] for k in ("date", "timestamp", "time", "open_time", "datetime")
                     if k in cols), df.columns[0])
    close_col = next((cols[k] for k in ("close", "close_price", "c", "adj_close")
                      if k in cols), None)
    if close_col is None:
        raise ValueError(f"{path}: нема колонки close (є: {list(df.columns)})")
    ts = df[date_col]
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
        raise ValueError(f"{path}: рік виходить {yr} — перевір колонку дати/одиницю")
    s = pd.Series(pd.to_numeric(df[close_col], errors="coerce").values,
                  index=idx).sort_index()
    return s[s > 0].dropna()


# --- розрахунок позицій (причинний) -----------------------------------------
def compute(close: pd.Series, target_vol: float, vol_win: int, sma_n: int):
    ret = close.pct_change()
    rv = ret.rolling(vol_win, min_periods=vol_win).std() * np.sqrt(ANN)
    vt = np.minimum(1.0, target_vol / rv.replace(0.0, np.nan))
    sma = close.rolling(sma_n, min_periods=sma_n).mean()
    trend = (close > sma).astype(float)
    df = pd.DataFrame({"close": close, "rv": rv, "sma": sma,
                       "pos_vt": vt, "pos_trend": trend,
                       "pos_combo": vt * trend, "ret": ret})
    return df


def equity_curves(df: pd.DataFrame, cost_bps: float):
    c_side = (cost_bps / 2) / 1e4
    out = {}
    for col, lab in [("pos_vt", "VT"), ("pos_combo", "VT+TREND")]:
        pos = df[col].shift(1).fillna(0.0)        # причинно
        turn = pos.diff().abs().fillna(pos.abs())
        strat = (pos * df["ret"] - turn * c_side).fillna(0.0)
        out[lab] = (1 + strat).cumprod()
    out["BH"] = (1 + df["ret"].fillna(0.0)).cumprod()
    return out


def today_line(df: pd.DataFrame) -> str:
    last = df.dropna(subset=["pos_vt"]).iloc[-1]
    d = df.index[-1].date()
    trend_txt = "вище" if last["pos_trend"] > 0 else "НИЖЧЕ"
    return (f"📅 Дані до {d} (close {last['close']:.0f})\n"
            f"Реалізована вола 30д (річна): {last['rv']*100:.0f}%\n"
            f"Ціна {trend_txt} 200d SMA\n"
            f"——\n"
            f"Ціль на наступний день:\n"
            f"  VT:        тримати {last['pos_vt']*100:.0f}%  (решта в кеші)\n"
            f"  VT+TREND:  тримати {last['pos_combo']*100:.0f}%")


def make_chart(df, eq, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(3, 1, figsize=(13, 9), sharex=True,
                           gridspec_kw={"height_ratios": [3, 1, 1.5]})
    ax[0].plot(df.index, df["close"], color="#222", lw=0.9)
    ax[0].set_yscale("log"); ax[0].set_ylabel("Ціна (log)")
    ax[0].set_title("Ціна + цільова позиція VT+TREND (зелене=в ринку, біле=кеш)")
    # фон за позицією
    pos = df["pos_combo"].fillna(0)
    ax[0].fill_between(df.index, df["close"].min(), df["close"].max(),
                       where=pos > 0, color="green", alpha=0.06)
    ax[1].fill_between(df.index, 0, df["pos_combo"]*100, color="teal", alpha=0.5)
    ax[1].set_ylabel("Позиція %"); ax[1].set_ylim(0, 105)
    for lab, col in [("BH", "#999"), ("VT+TREND", "teal")]:
        e = eq[lab]; dd = (e/e.cummax() - 1)*100
        ax[2].plot(df.index, dd, label=lab, color=col, lw=1)
    ax[2].set_ylabel("Просадка %"); ax[2].legend(loc="lower left")
    plt.tight_layout(); plt.savefig(path, dpi=110); plt.close()


def send_telegram(token, chat, text):
    import urllib.request, urllib.parse, json
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    with urllib.request.urlopen(url, data=data, timeout=15) as r:
        return json.loads(r.read())


def read_env(key):
    if key in os.environ:
        return os.environ[key]
    if os.path.exists(".env"):
        for line in open(".env"):
            if line.strip().startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="btc_daily.csv")
    ap.add_argument("--target-vol", type=float, default=0.20)
    ap.add_argument("--vol-win", type=int, default=30)
    ap.add_argument("--sma", type=int, default=200)
    ap.add_argument("--cost-bps", type=float, default=13.0)
    ap.add_argument("--export")            # шлях для CSV історії
    ap.add_argument("--chart")             # шлях для PNG
    ap.add_argument("--telegram", action="store_true")
    ap.add_argument("--token"); ap.add_argument("--chat")
    a = ap.parse_args(argv)

    close = load_daily(a.file)
    df = compute(close, a.target_vol, a.vol_win, a.sma)
    eq = equity_curves(df, a.cost_bps)
    line = today_line(df)
    print(line)

    if a.export:
        exp = pd.DataFrame({
            "date": df.index, "close": df["close"].values,
            "vol_annual": df["rv"].values, "pos_vt": df["pos_vt"].values,
            "pos_combo": df["pos_combo"].values,
            "equity_bh": eq["BH"].values, "equity_vt": eq["VT"].values,
            "equity_combo": eq["VT+TREND"].values,
            "dd_combo": (eq["VT+TREND"]/eq["VT+TREND"].cummax()-1).values,
        })
        exp.to_csv(a.export, index=False)
        print(f"\n💾 Історія → {a.export} ({len(exp)} рядків) — накладай на графік")

    if a.chart:
        make_chart(df, eq, a.chart)
        print(f"🖼  Графік → {a.chart}")

    if a.telegram:
        tok = a.token or read_env("TELEGRAM_BOT_TOKEN")
        chat = a.chat or read_env("TELEGRAM_CHAT_ID")
        if not tok or not chat:
            print("!! нема TELEGRAM_BOT_TOKEN/CHAT_ID (у .env або --token/--chat)")
        else:
            try:
                send_telegram(tok, chat, line)
                print("✅ Надіслано в телеграм")
            except Exception as e:
                print(f"!! телеграм не вийшов: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
