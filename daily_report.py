#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_report.py — Щоденний ОПИСОВИЙ звіт + порівняння днів між собою.

ВАЖЛИВО, чесно про межі:
  Це інструмент ОПИСУ (EDA) і контролю якості даних, а НЕ пошуку торгової
  переваги. Він каже "що сталося" за день і "чи дані цілі", але НЕ доводить,
  що якийсь патерн прибутковий. Порівняння 2-3 днів — це характеристика,
  а не статистика. Пошук edge лишається за honest_backtest.py і потребує тижнів.

Використання:
  python daily_report.py 2026-06-14      # детальний профіль одного дня
  python daily_report.py                 # порівняльна таблиця ВСІХ днів у папці

Залежності: pandas, pyarrow (вже є).
"""

import sys
import os
import glob
import pandas as pd
import numpy as np

DATA_DIR = "btc_tick_data"
KYIV = "Europe/Kyiv"


# ============================================================
# ЗАВАНТАЖЕННЯ
# ============================================================
def _read(prefix, date):
    path = os.path.join(DATA_DIR, f"{prefix}_{date}.parquet")
    if not os.path.exists(path):
        return None
    try:
        return pd.read_parquet(path)
    except Exception as e:
        print(f"⚠️  Не вдалось прочитати {path}: {e}")
        return None


def load_day(date):
    ticks = _read("btc_ticks", date)
    liqs = _read("btc_liquidations", date)
    oi = _read("btc_oi_funding", date)
    return ticks, liqs, oi


def list_dates():
    files = glob.glob(os.path.join(DATA_DIR, "btc_ticks_*.parquet"))
    dates = sorted(os.path.basename(f).replace("btc_ticks_", "").replace(".parquet", "") for f in files)
    return dates


# ============================================================
# ПРОФІЛЬ ОДНОГО ДНЯ (працює з готовими DataFrame)
# ============================================================
def profile_from_frames(ticks, liqs, oi):
    p = {}
    if ticks is None or len(ticks) == 0:
        return None

    t = ticks.copy()
    t = t[(t["price"] > 0) & (t["amount"] > 0)]
    t["dt"] = pd.to_datetime(t["timestamp_ms"], unit="ms", utc=True).dt.tz_convert(KYIV)
    t = t.sort_values("dt")
    t["is_buy"] = (~t["is_buyer_maker"].astype(bool)) if "is_buyer_maker" in t else (t["side"] == "buy")
    t["vol"] = t["price"] * t["amount"]

    # --- Ціна ---
    p["open"] = float(t["price"].iloc[0])
    p["close"] = float(t["price"].iloc[-1])
    p["high"] = float(t["price"].max())
    p["low"] = float(t["price"].min())
    p["range_pct"] = (p["high"] - p["low"]) / p["open"] * 100
    p["change_pct"] = (p["close"] - p["open"]) / p["open"] * 100

    # --- Активність / покриття ---
    p["ticks"] = int(len(t))
    p["start"] = t["dt"].min()
    p["end"] = t["dt"].max()
    # хвилинні бари -> рахуємо діри (хвилини без жодного тіку всередині покритого діапазону)
    per_min = t.set_index("dt")["price"].resample("1min").count()
    p["minutes_covered"] = int((per_min > 0).sum())
    p["minutes_gap"] = int((per_min == 0).sum())

    # --- Об'єм / CVD ---
    buy_v = float(t.loc[t["is_buy"], "vol"].sum())
    sell_v = float(t.loc[~t["is_buy"], "vol"].sum())
    p["volume_usd"] = buy_v + sell_v
    p["cvd_usd"] = buy_v - sell_v            # накопичена дельта за день
    p["cvd_pct"] = (p["cvd_usd"] / p["volume_usd"] * 100) if p["volume_usd"] > 0 else 0.0

    # --- Ліквідації ---
    if liqs is not None and len(liqs) > 0:
        lq = liqs.copy()
        lq["dt"] = pd.to_datetime(lq["timestamp_ms"], unit="ms", utc=True).dt.tz_convert(KYIV)
        side_up = lq["side"].astype(str).str.upper()
        p["liq_count"] = int(len(lq))
        p["liq_usd"] = float(lq["value_usd"].sum())
        p["liq_buy_usd"] = float(lq.loc[side_up == "BUY", "value_usd"].sum())   # шорти вибито
        p["liq_sell_usd"] = float(lq.loc[side_up == "SELL", "value_usd"].sum()) # лонги вибито
        big = lq.loc[lq["value_usd"].idxmax()]
        p["liq_biggest_usd"] = float(big["value_usd"])
        p["liq_biggest_side"] = str(big["side"]).upper()
        p["liq_biggest_time"] = big["dt"].strftime("%H:%M")
        # найбільший 60-секундний каскад
        per_min_liq = lq.set_index("dt")["value_usd"].resample("1min").sum()
        if len(per_min_liq) and per_min_liq.max() > 0:
            p["liq_cascade_usd"] = float(per_min_liq.max())
            p["liq_cascade_time"] = per_min_liq.idxmax().strftime("%H:%M")
        else:
            p["liq_cascade_usd"], p["liq_cascade_time"] = 0.0, "—"

        # контроль якості: ціни ліквідацій, що відірвані від ринку (>0.5% від найближчого тіку)
        lq_s = lq.sort_values("dt")
        merged = pd.merge_asof(lq_s, t[["dt", "price"]].rename(columns={"price": "mkt"}).sort_values("dt"),
                               on="dt", direction="nearest")
        merged["dev"] = (merged["price"] / merged["mkt"] - 1).abs()
        p["liq_price_outliers"] = int((merged["dev"] > 0.005).sum())
    else:
        for k in ["liq_count", "liq_usd", "liq_buy_usd", "liq_sell_usd",
                  "liq_biggest_usd", "liq_price_outliers"]:
            p[k] = 0
        p["liq_biggest_side"] = "—"; p["liq_biggest_time"] = "—"
        p["liq_cascade_usd"] = 0.0; p["liq_cascade_time"] = "—"

    # --- OI / Funding ---
    if oi is not None and len(oi) > 0:
        o = oi.sort_values("timestamp_ms")
        p["oi_start"] = float(o["open_interest"].iloc[0])
        p["oi_end"] = float(o["open_interest"].iloc[-1])
        p["oi_change_pct"] = ((p["oi_end"] - p["oi_start"]) / p["oi_start"] * 100) if p["oi_start"] else 0.0
        p["funding_avg"] = float(o["funding_rate"].mean())
    else:
        p["oi_start"] = p["oi_end"] = p["oi_change_pct"] = p["funding_avg"] = 0.0

    return p


def print_profile(date, p):
    if p is None:
        print(f"❌ Немає даних за {date}")
        return
    print("=" * 60)
    print(f"  📅 ПРОФІЛЬ ДНЯ: {date}")
    print("=" * 60)
    print(f" Покриття: {p['start'].strftime('%H:%M')}–{p['end'].strftime('%H:%M')} "
          f"| хвилин з даними: {p['minutes_covered']} | діри: {p['minutes_gap']}")
    print(f" Тіків: {p['ticks']:,}")
    print()
    print(" ЦІНА")
    print(f"   O {p['open']:.1f}  H {p['high']:.1f}  L {p['low']:.1f}  C {p['close']:.1f}")
    print(f"   Денний діапазон: {p['range_pct']:.2f}%   Зміна: {p['change_pct']:+.2f}%")
    print()
    print(" ПОТІК / CVD")
    print(f"   Об'єм: ${p['volume_usd']/1e9:.2f}B")
    print(f"   CVD за день: ${p['cvd_usd']/1e6:+.1f}M  ({p['cvd_pct']:+.2f}% від об'єму)")
    print()
    print(" ЛІКВІДАЦІЇ")
    print(f"   Всього: {p['liq_count']}  на ${p['liq_usd']/1e6:.2f}M")
    print(f"   Шорти вибито (BUY): ${p['liq_buy_usd']/1e6:.2f}M  |  "
          f"Лонги вибито (SELL): ${p['liq_sell_usd']/1e6:.2f}M")
    if p['liq_count']:
        print(f"   Найбільша: ${p['liq_biggest_usd']/1e3:.0f}K ({p['liq_biggest_side']}) о {p['liq_biggest_time']}")
        print(f"   Найбільший каскад (1 хв): ${p['liq_cascade_usd']/1e6:.2f}M о {p['liq_cascade_time']}")
    print()
    print(" OI / FUNDING")
    print(f"   OI: {p['oi_start']:,.0f} → {p['oi_end']:,.0f} BTC ({p['oi_change_pct']:+.2f}%)")
    print(f"   Сер. funding: {p['funding_avg']*100:+.4f}%")
    print()
    print(" 🔎 ЯКІСТЬ ДАНИХ")
    flags = []
    if p['minutes_gap'] > 0:
        flags.append(f"діри в тіках: {p['minutes_gap']} хв")
    if p['liq_price_outliers'] > 0:
        flags.append(f"викиди цін ліквідацій: {p['liq_price_outliers']}")
    print("   " + (" | ".join(flags) if flags else "✅ проблем не виявлено"))
    print("=" * 60)


# ============================================================
# ПОРІВНЯННЯ ДНІВ
# ============================================================
def compare_days(dates):
    rows = []
    for d in dates:
        p = profile_from_frames(*load_day(d))
        if p is None:
            continue
        rows.append({
            "Дата": d,
            "Тіків": p["ticks"],
            "Діапаз.%": round(p["range_pct"], 2),
            "Зміна%": round(p["change_pct"], 2),
            "Об'єм$B": round(p["volume_usd"]/1e9, 2),
            "CVD%": round(p["cvd_pct"], 2),
            "Лікв.": p["liq_count"],
            "Лікв.$M": round(p["liq_usd"]/1e6, 2),
            "Шорт/Лонг": f"{p['liq_buy_usd']/1e6:.1f}/{p['liq_sell_usd']/1e6:.1f}",
            "OI%": round(p["oi_change_pct"], 2),
            "Fund%": round(p["funding_avg"]*100, 4),
            "Діри": p["minutes_gap"],
        })
    if not rows:
        print("❌ Немає днів для порівняння.")
        return
    df = pd.DataFrame(rows)
    print("\n📊 ПОРІВНЯННЯ ДНІВ (описове — НЕ сигнал до торгівлі)\n")
    print(df.to_string(index=False))
    print("\nНагадування: 2-3 дні — замало для висновків про закономірності.")
    print("Це профіль даних і контроль якості, а не доведена перевага.\n")


# ============================================================
# MAIN
# ============================================================
def main():
    if len(sys.argv) > 1:
        date = sys.argv[1]
        print_profile(date, profile_from_frames(*load_day(date)))
    else:
        dates = list_dates()
        if not dates:
            print(f"❌ У {DATA_DIR}/ не знайдено файлів btc_ticks_*.parquet")
            return
        compare_days(dates)


if __name__ == "__main__":
    main()
