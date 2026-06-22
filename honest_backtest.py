#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
honest_backtest.py  -  Чесний валідатор переваги (edge) для tick-by-tick даних.

Що він робить інакше, ніж попередні скрипти:
  1. БЕЗ ЗАЗИРАННЯ В МАЙБУТНЄ. Усі ознаки (z-score дельти, середній об'єм)
     рахуються ТІЛЬКИ по минулих барах через .shift(1). Це головна умова чесності.
  2. FORWARD-RETURN. Для кожного сигналу міряємо, що сталося з ціною ПІСЛЯ нього
     (через 1/3/5/10 барів), а не до нього.
  3. BASELINE (нульова гіпотеза). Порівнюємо сигнал із середнім рухом усіх барів.
     Сигнал має БИТИ дрейф ринку, а не повторювати його.
  4. РЕАЛЬНІ ВИТРАТИ. Віднімаємо комісію taker + прослизання з обох боків.
  5. OUT-OF-SAMPLE. Ділимо дані за часом (70% / 30%) і дивимось, чи edge
     зберігається на даних, яких "стратегія" не бачила.

Використання:
  python honest_backtest.py                      # читає всю папку btc_tick_data/
  python honest_backtest.py btc_tick_data/btc_ticks_2026-06-13.parquet
  python honest_backtest.py btc_tick_data/       # папка з кількома днями

Залежності: pandas, numpy, pyarrow (у тебе вже є, бо бот пише parquet).
"""

import sys
import os
import glob
import numpy as np
import pandas as pd

# ============================================================
# НАЛАШТУВАННЯ (правь тут)
# ============================================================
BAR_SECONDS   = 60        # розмір бара в секундах. Спробуй 30, 60, 300.
LOOKBACK_BARS = 50        # вікно для розрахунку z-score та середнього об'єму
Z_THRESHOLD   = 2.0       # поріг аномалії дельти (z-score)
HORIZONS      = [1, 3, 5, 10]   # горизонти forward-return (у барах)

# Реальні витрати на КОЖЕН бік угоди (Binance USDT-M futures, taker):
TAKER_FEE   = 0.00045     # 0.045% комісія
SLIPPAGE    = 0.00020     # 0.02% прослизання/спред (консервативно)
# Повний round-trip коштує 2*(TAKER_FEE + SLIPPAGE):
ROUND_TRIP_COST = 2 * (TAKER_FEE + SLIPPAGE)

OOS_SPLIT = 0.70          # перші 70% часу = train, останні 30% = test (OOS)


# ============================================================
# 1. ЗАВАНТАЖЕННЯ ТІКІВ
# ============================================================
def load_ticks(path):
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "*.parquet")))
    elif os.path.isfile(path):
        files = [path]
    else:
        print(f"❌ Шлях не знайдено: {path}")
        sys.exit(1)

    if not files:
        print(f"❌ Parquet-файлів не знайдено у {path}")
        sys.exit(1)

    print(f"📥 Завантажую {len(files)} файл(ів)...")
    frames = [pd.read_parquet(f) for f in files]
    df = pd.concat(frames, ignore_index=True)

    # Очікувана схема з твого бота: timestamp_ms, price, amount, side, is_buyer_maker
    df = df.sort_values("timestamp_ms").reset_index(drop=True)
    df = df.drop_duplicates(subset=["timestamp_ms", "price", "amount", "side"])

    # 🛡️ Викидаємо биті дані (нулі / від'ємні)
    df = df[(df["price"] > 0) & (df["amount"] > 0)].copy()

    # Агресор-покупець: is_buyer_maker == False  =>  ринкова покупка (taker buy)
    if "is_buyer_maker" in df.columns:
        df["is_buy"] = ~df["is_buyer_maker"].astype(bool)
    else:
        df["is_buy"] = (df["side"] == "buy")

    df["volume_usd"] = df["price"] * df["amount"]
    df["dt"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    print(f"   Тіків після очищення: {len(df):,}")
    print(f"   Період: {df['dt'].min()}  →  {df['dt'].max()}")
    return df


# ============================================================
# 2. ПОБУДОВА БАРІВ
# ============================================================
def build_bars(df, bar_seconds):
    g = df.set_index("dt").resample(f"{bar_seconds}s")

    bars = pd.DataFrame({
        "open":   g["price"].first(),
        "high":   g["price"].max(),
        "low":    g["price"].min(),
        "close":  g["price"].last(),
        "buy_vol":  g.apply(lambda x: x.loc[x["is_buy"], "volume_usd"].sum()),
        "sell_vol": g.apply(lambda x: x.loc[~x["is_buy"], "volume_usd"].sum()),
        "trades": g["price"].count(),
    })
    bars = bars.dropna(subset=["close"])           # викидаємо порожні бари
    bars["total_vol"] = bars["buy_vol"] + bars["sell_vol"]
    bars["delta"] = bars["buy_vol"] - bars["sell_vol"]
    bars["delta_pct"] = np.where(bars["total_vol"] > 0,
                                 bars["delta"] / bars["total_vol"], 0.0)
    bars["bar_ret"] = bars["close"].pct_change()
    print(f"🧱 Побудовано {len(bars):,} барів по {bar_seconds}с "
          f"(непорожніх). Сер. тіків/бар: {bars['trades'].mean():.0f}")
    return bars.reset_index()


# ============================================================
# 3. ОЗНАКИ БЕЗ ЗАЗИРАННЯ В МАЙБУТНЄ
# ============================================================
def add_features(bars, lookback):
    # rolling по МИНУЛИХ барах: shift(1) гарантує, що поточний бар не входить
    d = bars["delta_pct"]
    roll_mean = d.shift(1).rolling(lookback).mean()
    roll_std  = d.shift(1).rolling(lookback).std()
    bars["z_delta"] = (d - roll_mean) / roll_std

    v = bars["total_vol"]
    bars["vol_ma"] = v.shift(1).rolling(lookback).mean()
    bars["vol_ratio"] = v / bars["vol_ma"]
    return bars


# ============================================================
# 4. ВИЗНАЧЕННЯ СИГНАЛІВ (boolean-маски)
# ============================================================
def define_signals(bars, z):
    sig = {}
    high_vol = bars["vol_ratio"] >= 1.0

    # Агресивна купівля (момент): сильна додатна дельта + підвищений об'єм -> очікуємо рух ВГОРУ
    sig["AGGR_BUY (momentum long)"]  = {"mask": (bars["z_delta"] >= z) & high_vol, "dir": +1}
    # Агресивний продаж (момент): сильна від'ємна дельта -> очікуємо рух ВНИЗ
    sig["AGGR_SELL (momentum short)"] = {"mask": (bars["z_delta"] <= -z) & high_vol, "dir": -1}

    # Поглинання покупця (mean-reversion): купують агресивно, але ціна стоїть -> стіна продавця -> ВНИЗ
    flat = bars["bar_ret"].abs() <= 0.0005
    sig["BUY ABSORPTION (revert short)"]  = {"mask": (bars["z_delta"] >= z) & high_vol & flat, "dir": -1}
    # Поглинання продавця: продають агресивно, ціна стоїть -> стіна покупця -> ВГОРУ
    sig["SELL ABSORPTION (revert long)"] = {"mask": (bars["z_delta"] <= -z) & high_vol & flat, "dir": +1}
    return sig


# ============================================================
# 5. ОЦІНКА ПЕРЕВАГИ (forward-return + витрати + статистика)
# ============================================================
def forward_returns(bars, horizon):
    # відсотковий рух ціни ВПЕРЕД на horizon барів
    return (bars["close"].shift(-horizon) - bars["close"]) / bars["close"]

def evaluate(bars, signals, label=""):
    print("\n" + "=" * 68)
    print(f"  РЕЗУЛЬТАТИ {label}".rstrip())
    print("=" * 68)

    # Baseline: середній forward-return усіх барів (дрейф ринку)
    print("\n📊 BASELINE (середній рух ринку, усі бари) — це планка, яку треба бити:")
    for h in HORIZONS:
        fr = forward_returns(bars, h).dropna()
        print(f"   +{h:>2} бар: середній рух {fr.mean()*100:+.4f}%  "
              f"(|рух| медіана {fr.abs().median()*100:.4f}%)")

    print(f"\n💸 Поріг витрат (round-trip) = {ROUND_TRIP_COST*100:.3f}%. "
          f"Чистий edge має бути СТІЙКО більший за 0 ПІСЛЯ цих витрат.\n")

    for name, s in signals.items():
        mask = s["mask"].fillna(False)
        direction = s["dir"]
        n = int(mask.sum())
        print("-" * 68)
        print(f"🎯 {name}   |   сигналів: {n}")
        if n < 20:
            print("   ⚠️  Замало спрацювань (<20) для будь-яких висновків. Пропускаю.")
            continue

        for h in HORIZONS:
            fr = forward_returns(bars, h)
            # edge у напрямку сигналу, мінус витрати round-trip
            edge = direction * fr[mask]
            edge = edge.dropna()
            if len(edge) < 20:
                continue
            net = edge - ROUND_TRIP_COST
            mean_net = net.mean()
            hit = (net > 0).mean()
            # простий t-стат проти нуля
            tstat = mean_net / (net.std(ddof=1) / np.sqrt(len(net))) if net.std(ddof=1) > 0 else 0.0
            verdict = "✅ є сигнал" if (mean_net > 0 and abs(tstat) >= 2.0) else \
                      ("≈ шум" if abs(tstat) < 2.0 else "❌ проти тебе")
            print(f"   +{h:>2} бар | чистий сер. {mean_net*100:+.4f}% | "
                  f"win {hit*100:4.1f}% | t={tstat:+.2f} | {verdict}")


def split_oos(bars, frac):
    cut = int(len(bars) * frac)
    return bars.iloc[:cut].copy(), bars.iloc[cut:].copy()


# ============================================================
# MAIN
# ============================================================
def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "btc_tick_data/"
    df = load_ticks(path)
    bars = build_bars(df, BAR_SECONDS)
    bars = add_features(bars, LOOKBACK_BARS)

    if len(bars) < LOOKBACK_BARS + max(HORIZONS) + 50:
        print("\n⚠️  УВАГА: барів дуже мало. Будь-який 'edge' тут — це випадковість.")
        print("    Для серйозних висновків треба тижні даних, а не один день.\n")

    train, test = split_oos(bars, OOS_SPLIT)

    sig_train = define_signals(train, Z_THRESHOLD)
    sig_test  = define_signals(test,  Z_THRESHOLD)

    evaluate(train, sig_train, label="TRAIN (перші 70% часу)")
    evaluate(test,  sig_test,  label="TEST / OUT-OF-SAMPLE (останні 30%)")

    print("\n" + "=" * 68)
    print("  ЯК ЧИТАТИ:")
    print("  • Якщо edge є на TRAIN, але зникає на TEST — це перенавчання, не edge.")
    print("  • 'є сигнал' має з'явитись на ОБОХ вибірках і на кількох горизонтах.")
    print("  • t<2 = статистично невідрізнити від нуля = шум.")
    print("  • Один день даних майже завжди дасть тільки шум. Це нормально.")
    print("=" * 68)


if __name__ == "__main__":
    main()
