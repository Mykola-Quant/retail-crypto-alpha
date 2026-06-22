#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cascade_study.py — Екстрактор подій-ліквідацій (каскадів) + вимір форвардних рухів.
Версія 3 (додано baseline-надлишок).

ЩО РОБИТЬ:
  • Знаходить "каскади": момент, коли сумарний обсяг ОДНОСТОРОННІХ ліквідацій
    у вікні WINDOW_SEC уперше перевищує THRESHOLD_USD. ЯКІР = цей момент тригера.
  • Контекст НА МОМЕНТ тригера (тільки минуле): органічний CVD, зміна OI, funding,
    відносний обсяг (z-score) і швидкість каскаду.
  • ФОРВАРДНІ рухи ціни на фіксованих горизонтах СУВОРО ПІСЛЯ якоря, мінус кости.
  • НАДЛИШОК над дрейфом ринку: exc = сирий рух − baseline(горизонт).
  • Розклад: BUY (вибивають шортів) / SELL (вибивають лонгів) + за напрямком OI.

ВЕРСІЇ:
  v2: де-контамінований CVD; мікро-VWAP ціна; rel_vol_z; speed_usd_s.
  v3: baseline-дрейф по сітці + колонки exc_{H}m; t рахується на НАДЛИШКУ (тест vs дрейф,
      а не vs нуль). net-LONG/net-SHORT лишаються на СИРОМУ русі (реальний P&L).

ЧОГО НЕ РОБИТЬ:
  • Не призначає "правила входу". Лише міряє. Більшість гіпотез має змитися після костів.
  • Не тюнь THRESHOLD_USD / вікна під гарний t — це перенавчання.
  • Один in-sample t — це привід ПРИДИВИТИСЬ, а не вмикати бота.

Запуск:  python cascade_study.py   (безпечно паралельно зі збирачем — тільки читає)
"""

import os
import glob
import numpy as np
import pandas as pd

# ----------------- CONFIG (стартові значення, НЕ тюнити під результат) -----------------
DATA_DIR             = "btc_tick_data"
WINDOW_SEC           = 60          # вікно накопичення обсягу ліквідацій
THRESHOLD_USD        = 250_000     # поріг спрацювання каскаду (одна сторона)
COOLDOWN_SEC         = 300         # щоб один каскад рахувався як ОДНА подія
PRE_CVD_SEC          = 60          # вікно CVD перед якорем
PRE_OI_SEC           = 300         # вікно зміни OI перед якорем
HORIZONS_MIN         = [1, 5, 15, 30, 60]      # форвардні горизонти, хвилини
FWD_AGG_SEC          = 10          # мікро-вікно для VWAP ціни (якір і форвард)
BASELINE_STEP_SEC    = 60          # крок сітки контрольних точок для дрейфу
REL_VOL_LOOKBACK_SEC = 6 * 3600    # трейлінг для відносного обсягу
REL_VOL_MIN_BUCKETS  = 30          # мін. хвилин у трейлінгу, щоб рахувати z
TAKER_FEE            = 0.00045
SLIPPAGE             = 0.0002
ROUND_TRIP_COST      = (TAKER_FEE + SLIPPAGE) * 2   # = 0.130%
ANCHOR_TOL_SEC       = 5           # тік-якір має бути не далі цього ДО точки
FWD_TOL_SEC          = 90          # форвардний тік не далі цього від цілі (захист від дір)
MIN_N                = 30          # менше подій — жодних висновків
# ---------------------------------------------------------------------------------------


def load_parquets(prefix):
    files = sorted(glob.glob(os.path.join(DATA_DIR, f"{prefix}_*.parquet")))
    if not files:
        return pd.DataFrame()
    parts = []
    for f in files:
        try:
            parts.append(pd.read_parquet(f))
        except Exception as e:
            print(f"   ⚠️  пропускаю {os.path.basename(f)}: {e}")
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    df = df.dropna(subset=["timestamp_ms"]).drop_duplicates()
    return df.sort_values("timestamp_ms").reset_index(drop=True)


def asof_back(ts_arr, val_arr, target, tol_ms):
    """Останнє значення з ts <= target (у межах tol_ms), інакше np.nan."""
    i = np.searchsorted(ts_arr, target, side="right") - 1
    if i < 0 or (target - ts_arr[i]) > tol_ms:
        return np.nan
    return val_arr[i]


def vwap(t_ts, t_price, t_amt, lo, hi):
    """Об'ємо-зважена ціна на [lo, hi]; якщо ваг нема — медіана; якщо тіків нема — nan."""
    l = np.searchsorted(t_ts, lo, side="left")
    r = np.searchsorted(t_ts, hi, side="right")
    if r <= l:
        return np.nan
    p = t_price[l:r]
    w = t_amt[l:r]
    sw = w.sum()
    return float((p * w).sum() / sw) if sw > 0 else float(np.median(p))


def anchor_price(t_ts, t_price, t_amt, a):
    """Робастна ціна входу: мікро-VWAP за останні FWD_AGG_SEC до точки."""
    i = np.searchsorted(t_ts, a, side="right") - 1
    if i < 0 or (a - t_ts[i]) > ANCHOR_TOL_SEC * 1000:
        return np.nan
    return vwap(t_ts, t_price, t_amt, a - FWD_AGG_SEC * 1000, a)


def forward_price(t_ts, t_price, t_amt, target):
    """Робастна форвардна ціна: мікро-VWAP за FWD_AGG_SEC після цілі; nan якщо ціль у дірі."""
    i = np.searchsorted(t_ts, target, side="left")
    if i >= len(t_ts) or (t_ts[i] - target) > FWD_TOL_SEC * 1000:
        return np.nan
    return vwap(t_ts, t_price, t_amt, target, target + FWD_AGG_SEC * 1000)


def compute_baseline(t_ts, t_price, t_amt):
    """Дрейф ринку: сер. форвардний рух за кожен горизонт по сітці кожні BASELINE_STEP_SEC.
    Та сама VWAP/gap-логіка, що й для подій → віднімання яблуко-від-яблука."""
    step = BASELINE_STEP_SEC * 1000
    max_h = max(HORIZONS_MIN) * 60_000
    if int(t_ts[-1]) - int(t_ts[0]) <= max_h:
        return {H: np.nan for H in HORIZONS_MIN}
    grid = np.arange(int(t_ts[0]), int(t_ts[-1]) - max_h, step, dtype=np.int64)
    acc = {H: [] for H in HORIZONS_MIN}
    for g in grid:
        g = int(g)
        p0 = anchor_price(t_ts, t_price, t_amt, g)
        if np.isnan(p0):
            continue
        for H in HORIZONS_MIN:
            pf = forward_price(t_ts, t_price, t_amt, g + H * 60_000)
            if not np.isnan(pf):
                acc[H].append((pf / p0 - 1.0) * 100)
    return {H: (float(np.mean(acc[H])) if acc[H] else np.nan) for H in HORIZONS_MIN}


def detect_events(liqs):
    """Каскади окремо для кожної сторони (трейлінг-вікно + cooldown). + швидкість і к-сть принтів."""
    events = []
    win_ms = WINDOW_SEC * 1000
    cd_ms = COOLDOWN_SEC * 1000
    for side in ("BUY", "SELL"):
        s = liqs[liqs["side_u"] == side].sort_values("timestamp_ms")
        if s.empty:
            continue
        l_ts = s["timestamp_ms"].to_numpy(dtype=np.int64)
        l_usd = s["usd"].to_numpy(dtype=float)
        j, run, last_trig = 0, 0.0, -10**18
        for i in range(len(l_ts)):
            run += l_usd[i]
            while l_ts[j] < l_ts[i] - win_ms:
                run -= l_usd[j]
                j += 1
            if run >= THRESHOLD_USD and l_ts[i] > last_trig + cd_ms:
                last_trig = l_ts[i]
                span_s = max((l_ts[i] - l_ts[j]) / 1000.0, 1.0)
                events.append({"side": side, "anchor_ms": int(l_ts[i]),
                               "roll_usd": float(run),
                               "speed_usd_s": float(run / span_s),
                               "n_prints": int(i - j + 1)})
    return pd.DataFrame(events)


def build_relvol_baseline(liqs, t0, t1):
    """Повна хвилинна сітка сумарного обсягу ліквідацій (порожні хвилини = 0)."""
    key = (liqs["timestamp_ms"].to_numpy(dtype=np.int64) // 60000) * 60000
    m = pd.Series(liqs["usd"].to_numpy(dtype=float), index=key).groupby(level=0).sum()
    first_min = (int(t0) // 60000) * 60000
    last_min = (int(t1) // 60000) * 60000
    full = np.arange(first_min, last_min + 60000, 60000, dtype=np.int64)
    m_full = m.reindex(full, fill_value=0.0)
    return full, m_full.to_numpy(dtype=float)


def main():
    print("📥 Завантажую дані...")
    ticks = load_parquets("btc_ticks")
    liqs = load_parquets("btc_liquidations")
    oi = load_parquets("btc_oi_funding")

    if ticks.empty or liqs.empty:
        print("❌ Немає тіків або ліквідацій — нема що аналізувати.")
        return

    ticks = ticks.sort_values("timestamp_ms").reset_index(drop=True)
    t_ts = ticks["timestamp_ms"].to_numpy(dtype=np.int64)
    t_price = ticks["price"].to_numpy(dtype=float)
    amount = ticks["amount"].to_numpy(dtype=float)
    if "is_buyer_maker" in ticks.columns:
        sign = np.where(ticks["is_buyer_maker"].to_numpy(dtype=bool), -1.0, 1.0)
    else:
        sign = np.where(ticks["side"].astype(str).str.lower().to_numpy() == "sell", -1.0, 1.0)
    cum_signed = np.concatenate([[0.0], np.cumsum(amount * sign)])

    print(f"   Тіків: {len(ticks):,}  |  Ліквідацій: {len(liqs):,}  |  OI-точок: {len(oi):,}")
    print(f"   Період: {pd.to_datetime(t_ts[0], unit='ms', utc=True)}  →  "
          f"{pd.to_datetime(t_ts[-1], unit='ms', utc=True)}")

    if not oi.empty and "open_interest" in oi.columns:
        oi = oi.sort_values("timestamp_ms").reset_index(drop=True)
        oi_ts = oi["timestamp_ms"].to_numpy(dtype=np.int64)
        oi_val = oi["open_interest"].to_numpy(dtype=float)
        fund_val = oi["funding_rate"].to_numpy(dtype=float) if "funding_rate" in oi.columns else None
    else:
        oi_ts = oi_val = fund_val = None

    liqs = liqs.copy()
    liqs["side_u"] = liqs["side"].astype(str).str.upper()
    if "value_usd" in liqs.columns:
        liqs["usd"] = liqs["value_usd"].astype(float)
    else:
        liqs["usd"] = liqs["price"].astype(float) * liqs["amount"].astype(float)
    liq_ts = liqs["timestamp_ms"].to_numpy(dtype=np.int64)
    liq_amt = liqs["amount"].to_numpy(dtype=float)
    liq_sign = np.where(liqs["side_u"].to_numpy() == "BUY", 1.0, -1.0)
    cum_liq = np.concatenate([[0.0], np.cumsum(liq_amt * liq_sign)])

    rv_ts, rv_val = build_relvol_baseline(liqs, t_ts[0], t_ts[-1])

    # --- дрейф ринку (baseline) ---
    print("📈 Рахую дрейф ринку (baseline) по сітці...")
    base = compute_baseline(t_ts, t_price, amount)
    for H in HORIZONS_MIN:
        bv = base[H]
        print(f"   +{H:>2}хв дрейф: {bv:+.4f}%" if not np.isnan(bv) else f"   +{H:>2}хв дрейф: н/д")

    ev = detect_events(liqs)
    if ev.empty:
        print("❌ Жодного каскаду за поточним порогом. Збирай далі "
              "(або обережно знизь THRESHOLD_USD).")
        return
    ev = ev.sort_values("anchor_ms").reset_index(drop=True)

    cvd_win = PRE_CVD_SEC * 1000
    oi_win = PRE_OI_SEC * 1000
    rv_look = REL_VOL_LOOKBACK_SEC * 1000

    rows = []
    for _, e in ev.iterrows():
        a = int(e["anchor_ms"])
        p0 = anchor_price(t_ts, t_price, amount, a)
        rec = {"side": e["side"], "time": pd.to_datetime(a, unit='ms', utc=True),
               "roll_usd": e["roll_usd"], "speed_usd_s": e["speed_usd_s"],
               "n_prints": e["n_prints"], "anchor_price": p0}

        t_lo = np.searchsorted(t_ts, a - cvd_win, side="left")
        t_hi = np.searchsorted(t_ts, a, side="right")
        cvd_raw = float(cum_signed[t_hi] - cum_signed[t_lo])
        l_lo = np.searchsorted(liq_ts, a - cvd_win, side="left")
        l_hi = np.searchsorted(liq_ts, a, side="right")
        cvd_liq = float(cum_liq[l_hi] - cum_liq[l_lo])
        rec["cvd_pre_raw"] = cvd_raw
        rec["cvd_liq"] = cvd_liq
        rec["cvd_pre_org"] = cvd_raw - cvd_liq

        rl = np.searchsorted(rv_ts, a - rv_look, side="left")
        rh = np.searchsorted(rv_ts, a, side="right")
        samp = rv_val[rl:rh]
        if len(samp) >= REL_VOL_MIN_BUCKETS and samp.std() > 0:
            rec["rel_vol_z"] = float((e["roll_usd"] - samp.mean()) / samp.std())
        else:
            rec["rel_vol_z"] = np.nan

        if oi_ts is not None:
            oi_now = asof_back(oi_ts, oi_val, a, 2 * oi_win)
            oi_prev = asof_back(oi_ts, oi_val, a - oi_win, 2 * oi_win)
            if not (np.isnan(oi_now) or np.isnan(oi_prev)) and oi_prev > 0:
                rec["oi_chg_pct"] = (oi_now / oi_prev - 1.0) * 100
            else:
                rec["oi_chg_pct"] = np.nan
            rec["funding"] = asof_back(oi_ts, fund_val, a, 2 * oi_win) if fund_val is not None else np.nan
        else:
            rec["oi_chg_pct"] = np.nan
            rec["funding"] = np.nan

        # форвардні рухи + надлишок над дрейфом
        for H in HORIZONS_MIN:
            pf = forward_price(t_ts, t_price, amount, a + H * 60_000)
            ret = np.nan if (np.isnan(p0) or np.isnan(pf)) else (pf / p0 - 1.0) * 100
            rec[f"ret_{H}m"] = ret
            rec[f"base_{H}m"] = base[H]
            rec[f"exc_{H}m"] = np.nan if (np.isnan(ret) or np.isnan(base[H])) else ret - base[H]
        rows.append(rec)

    res = pd.DataFrame(rows)
    res.to_csv("cascade_events.csv", index=False)
    print(f"\n💾 Таблицю подій збережено → cascade_events.csv  (рядків: {len(res)})")
    print("   Колонки для майбутніх розрізів: cvd_pre_org, rel_vol_z, speed_usd_s, exc_*m")

    def report(df, title):
        print("\n" + "=" * 84)
        print(f"  {title}   |   подій: {len(df)}")
        print("=" * 84)
        if len(df) == 0:
            print("  (порожньо)")
            return
        for H in HORIZONS_MIN:
            r = df[f"ret_{H}m"].dropna().to_numpy()
            n = len(r)
            if n == 0:
                print(f"  +{H:>2}хв: немає валідних вимірів (діри/край даних)")
                continue
            raw = r.mean()
            b = base[H]
            exc = raw - b if not np.isnan(b) else np.nan
            net_long = raw - ROUND_TRIP_COST * 100
            net_short = -raw - ROUND_TRIP_COST * 100
            std = r.std(ddof=1) if n > 1 else 0.0
            # t рахується на НАДЛИШКУ (тест проти дрейфу), а не проти нуля
            t = (raw - b) / (std / np.sqrt(n)) if (std > 0 and not np.isnan(b)) else 0.0
            flag = "" if n >= MIN_N else "  ⚠️замало"
            exc_s = f"{exc:+.4f}%" if not np.isnan(exc) else "  н/д  "
            print(f"  +{H:>2}хв | n={n:>3} | надл {exc_s} | "
                  f"net-LONG {net_long:+.4f}% | net-SHORT {net_short:+.4f}% | "
                  f"win(up) {(r > 0).mean() * 100:4.0f}% | t(надл)={t:+.2f}{flag}")

    for side in ("BUY", "SELL"):
        d = res[res["side"] == side]
        label = "вибивають ШОРТІВ (тиск угору)" if side == "BUY" else "вибивають ЛОНГІВ (тиск униз)"
        report(d, f"{side}-каскади — {label}")
        if d["oi_chg_pct"].notna().any():
            report(d[d["oi_chg_pct"] < 0], f"{side}-каскади + OI ВНИЗ (радше сквіз/ковер)")
            report(d[d["oi_chg_pct"] > 0], f"{side}-каскади + OI ВГОРУ (радше свіжі позиції)")

    print("\n" + "=" * 84)
    print("  ЯК ЧИТАТИ (і де пастка):")
    print("  • надл = рух ПОНАД дрейф ринку (наукове 'чи є ефект'); t рахується саме на ньому.")
    print("  • net-LONG/net-SHORT = на СИРОМУ русі мінус кости (реальний P&L; дрейф з нього не віднімеш).")
    print("  • t<2 = шум.  n<30 = взагалі не висновок.")
    print("  • OI ВНИЗ = закриття позицій (сквіз) — частіше виснаження; OI ВГОРУ = свіжий потік.")
    print("  • Розрізи за rel_vol_z / speed роби пізніше на CSV, коли подій стане ~100+.")
    print("  • 30+ клітинок t за запуск → одне |t|>2.5 на шумі — ЗАКОНОМІРНІСТЬ, не альфа.")
    print("    Віриш лише після: гіпотеза наперед + out-of-sample + поправка на множинність.")
    print("=" * 84)


if __name__ == "__main__":
    main()
