#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_daily.py — Завантажує денні бари кількох активів із Binance через ccxt
і зберігає кожен у окремий <тікер>_daily.csv (стовпці: date, open, high, low, close, volume).

API-ключ НЕ потрібен. Запуск:  python fetch_daily.py
Активи з коротшою історією (напр. SOL) почнуться з дати лістингу автоматично.
"""

import time
import pandas as pd
import ccxt

EXCHANGE  = "binance"        # якщо не відкриється — "kraken" (і зміни /USDT на /USD)
SYMBOLS   = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "TRX/USDT", "PAXG/USDT"]
START     = "2017-08-01T00:00:00Z"
TIMEFRAME = "1d"
LIMIT     = 1000


def fetch_one(ex, symbol):
    since = ex.parse8601(START)
    now = ex.milliseconds()
    rows = []
    while since < now:
        try:
            batch = ex.fetch_ohlcv(symbol, TIMEFRAME, since=since, limit=LIMIT)
        except Exception as e:
            print(f"     ⚠️  {e}; пауза 5с і повтор")
            time.sleep(5)
            continue
        if not batch:
            break
        rows += batch
        since = batch[-1][0] + 24 * 3600 * 1000
        time.sleep(ex.rateLimit / 1000)
        if len(batch) < LIMIT:
            break
    return rows


def main():
    ex = getattr(ccxt, EXCHANGE)({"enableRateLimit": True})
    for symbol in SYMBOLS:
        base = symbol.split("/")[0].lower()
        out = f"{base}_daily.csv"
        print(f"📥 {symbol} ...")
        rows = fetch_one(ex, symbol)
        if not rows:
            print(f"   ⚠️  нічого не отримав для {symbol}, пропускаю")
            continue
        df = pd.DataFrame(rows, columns=["timestamp_ms", "open", "high", "low", "close", "volume"])
        df = df.drop_duplicates("timestamp_ms").sort_values("timestamp_ms")
        df["date"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
        df[["date", "open", "high", "low", "close", "volume"]].to_csv(out, index=False)
        print(f"   💾 {out} — {len(df)} барів: {df['date'].iloc[0].date()} → {df['date'].iloc[-1].date()}")
        time.sleep(1)
    print("\nГотово. Тепер:  python regime_overlay.py")


if __name__ == "__main__":
    main()
