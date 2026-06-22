#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_funding.py
================
Тягне з Binance USDM (через ccxt, який у тебе вже стоїть):
  1) історію funding rate BTC/USDT:USDT
  2) 8h klines (для forward-returns, вирівняних з моментами funding)
і зберігає у funding_btc.parquet та klines_8h_btc.parquet.

Дані крихітні: ~2700 рядків на 2.5 роки. Качається за хвилину.

Запуск:
    python fetch_funding.py                 # від 2023-01-01 до зараз
    python fetch_funding.py --since 2022-01-01
"""
from __future__ import annotations
import argparse, time
import ccxt
import pandas as pd

SYMBOL = "BTC/USDT:USDT"   # USDM perp у нотації ccxt


def _paginate(fetch, since, step_from_last, limit, label, now_ms):
    """Пагінує до 'зараз'. Обривається ТІЛЬКИ на порожній партії або
    відсутності прогресу — не на недоборі (Binance max 1000/виклик != кінець)."""
    out, cur, last_seen = [], since, None
    while cur < now_ms:
        batch = fetch(cur, limit)
        if not batch:
            break
        out += batch
        last = step_from_last(batch[-1])
        print(f"  {label}: +{len(batch)} (до {pd.to_datetime(last, unit='ms')})", flush=True)
        if last_seen is not None and last <= last_seen:
            break  # немає прогресу — дійшли до краю
        last_seen = last
        cur = last + 1
        time.sleep(0.25)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2023-01-01")
    a = ap.parse_args(argv)

    ex = ccxt.binanceusdm({"enableRateLimit": True})
    since = ex.parse8601(a.since + "T00:00:00Z")
    now_ms = ex.milliseconds()

    print("Тягну funding history...")
    fr = _paginate(
        lambda s, l: ex.fetch_funding_rate_history(SYMBOL, since=s, limit=l),
        since, lambda r: r["timestamp"], 1000, "funding", now_ms)
    fund = (pd.DataFrame([{"ts": r["timestamp"], "fundingRate": float(r["fundingRate"])}
                          for r in fr])
            .drop_duplicates("ts").sort_values("ts").reset_index(drop=True))
    fund.to_parquet("funding_btc.parquet")
    print(f"-> funding_btc.parquet: {len(fund)} рядків "
          f"({pd.to_datetime(fund.ts.min(),unit='ms')}..{pd.to_datetime(fund.ts.max(),unit='ms')})")

    print("Тягну 8h klines...")
    kl = _paginate(
        lambda s, l: ex.fetch_ohlcv(SYMBOL, "8h", since=s, limit=l),
        since, lambda r: r[0], 1000, "klines", now_ms)
    k = (pd.DataFrame(kl, columns=["ts", "open", "high", "low", "close", "volume"])
         .drop_duplicates("ts").sort_values("ts").reset_index(drop=True))
    k.to_parquet("klines_8h_btc.parquet")
    print(f"-> klines_8h_btc.parquet: {len(k)} рядків")


if __name__ == "__main__":
    main()
