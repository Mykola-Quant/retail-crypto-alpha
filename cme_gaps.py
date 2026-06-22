import asyncio
import ccxt.async_support as ccxt
from datetime import datetime, timezone
import pytz

# ═══════════════════════════════════════════════════════════
#  CME GAP CONTINUATION (ДЗЕРКАЛЬНА СТРАТЕГІЯ)
#  - Торгуємо за напрямком гепу (Моментум).
#  - Стоп-Лос: Закриття гепу (ціна п'ятниці).
#  - Тейк-Профіт: 1.5x від розміру гепу в сторону тренду.
#  - Реальний R:R = 1 : 1.5 (Прибуток більший за ризик)
# ═══════════════════════════════════════════════════════════

SYMBOL         = 'BTC/USDT'      # Spot Binance
DAYS           = 365 * 3         # 3 роки: 2023-2026
MIN_GAP_PCT    = 0.005           # Мінімальний gap 0.5%
RISK_PER_TRADE = 20              # $ ризик на угоду
SL_MULTIPLIER  = 1.5             # Тейк-профіт = розмір гепу * 1.5
COMMISSION     = 0.001           # 0.1% (spot комісія Binance)

chicago_tz = pytz.timezone('America/Chicago')

async def fetch_data(exchange, symbol, days):
    print(f"📥 Завантаження 1h OHLCV {symbol} за {days} днів...")
    now   = exchange.milliseconds()
    since = now - (days * 24 * 60 * 60 * 1000)
    all_candles = []

    while since < now:
        try:
            candles = await exchange.fetch_ohlcv(symbol, '1h', since, limit=1000)
            if not candles:
                break
            all_candles.extend(candles)
            since = candles[-1][0] + 3_600_000
            print(f"   Завантажено: {len(all_candles)} свічок...", end='\r')
            await asyncio.sleep(0.1)
        except Exception as e:
            print(f"\n⚠️  {e}")
            await asyncio.sleep(3)

    print(f"\n✅ Завантажено: {len(all_candles)} годинних свічок")
    return all_candles

def analyze_cme_gaps(candles_1h):
    candles_by_ts = {c[0]: c for c in candles_1h}
    sorted_ts     = sorted(candles_by_ts.keys())

    gaps    = []
    trades  = []

    print("\n🔬 Аналіз CME gaps (Часовий пояс: Чикаго)...")

    friday_close = None
    friday_ts = None

    for i, ts in enumerate(sorted_ts):
        dt_utc = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        dt_ct = dt_utc.astimezone(chicago_tz)

        # 1. ШУКАЄМО ЗАКРИТТЯ П'ЯТНИЦІ (16:00 CT)
        if dt_ct.weekday() == 4 and dt_ct.hour == 15:
            friday_close = candles_by_ts[ts][4]  
            friday_ts = ts
            continue

        # 2. ШУКАЄМО ВІДКРИТТЯ НЕДІЛІ (17:00 CT)
        if dt_ct.weekday() == 6 and dt_ct.hour == 17:
            if friday_close is None:
                continue

            monday_open = candles_by_ts[ts][1]  
            sunday_open_ts = ts
            
            if (sunday_open_ts - friday_ts) > 55 * 3_600_000:
                friday_close = None
                continue

            # 3. РАХУЄМО РОЗМІР ГЕПУ
            gap_pct = (monday_open - friday_close) / friday_close

            if abs(gap_pct) >= MIN_GAP_PCT:
                gap_direction = 'UP' if gap_pct > 0 else 'DOWN'
                
                week_end_ts = sunday_open_ts + 120 * 3_600_000
                week_candles = [
                    candles_by_ts[t] for t in sorted_ts
                    if sunday_open_ts <= t <= week_end_ts and t in candles_by_ts
                ]

                if not week_candles:
                    continue

                gap_closed    = False
                close_candle_i = None

                for j, wc in enumerate(week_candles):
                    if gap_direction == 'UP':
                        if wc[3] <= friday_close:  
                            gap_closed = True
                            close_candle_i = j
                            break
                    else:
                        if wc[2] >= friday_close:  
                            gap_closed = True
                            close_candle_i = j
                            break

                gaps.append({
                    'date': dt_ct.strftime('%Y-%m-%d'),
                    'friday_close': friday_close,
                    'monday_open': monday_open,
                    'gap_pct': gap_pct * 100,
                    'direction': gap_direction,
                    'closed': gap_closed,
                    'hours': close_candle_i,
                })

                # 4. ДЗЕРКАЛЬНА ТОРГОВА СИМУЛЯЦІЯ (GAP CONTINUATION)
                entry = monday_open
                gap_size_abs = abs(monday_open - friday_close)
                tp_dist = gap_size_abs * SL_MULTIPLIER  # Тейк-Профіт = 1.5x гепу
                
                if gap_direction == 'UP':
                    sl = friday_close       # Стоп: ціна повернулася закривати геп
                    tp = entry + tp_dist    # Тейк: ціна пішла далі вгору
                else:
                    sl = friday_close       
                    tp = entry - tp_dist    
                    
                position_size_usd = RISK_PER_TRADE / (gap_size_abs / entry) 
                comm_cost = position_size_usd * COMMISSION * 2 

                reached_tp = False
                reached_sl = False

                # Песимістичний пошук виконання ордерів
                for j, wc in enumerate(week_candles):
                    if gap_direction == 'UP':
                        if wc[3] <= sl: # Спочатку перевіряємо стоп (песимізм)
                            reached_sl = True
                            break
                        if wc[2] >= tp: # Потім тейк
                            reached_tp = True
                            break
                    else:
                        if wc[2] >= sl: 
                            reached_sl = True
                            break
                        if wc[3] <= tp: 
                            reached_tp = True
                            break

                if reached_tp:
                    raw_profit = RISK_PER_TRADE * SL_MULTIPLIER # Прибуток 1.5x
                    pnl = raw_profit - comm_cost
                    trades.append({'pnl': pnl, 'res': 'TP', 'dir': 'LONG' if gap_direction=='UP' else 'SHORT'})
                elif reached_sl:
                    pnl = -RISK_PER_TRADE - comm_cost # Збиток 1x
                    trades.append({'pnl': pnl, 'res': 'SL', 'dir': 'LONG' if gap_direction=='UP' else 'SHORT'})
                else:
                    trades.append({'pnl': 0, 'res': 'OPEN', 'dir': 'OPEN'})
            
            friday_close = None

    return gaps, trades


def print_results(gaps, trades):
    print("\n" + "═" * 55)
    print("  📊 РЕЗУЛЬТАТИ: CME GAP CONTINUATION (Моментум)")
    print("═" * 55)

    if not gaps:
        print("  ❌ Gaps не знайдено. Перевір дані.")
        return

    total      = len(gaps)
    closed     = [g for g in gaps if g['closed']]
    close_rate = len(closed) / total * 100

    print(f"\n  📅 Всього CME gaps > {MIN_GAP_PCT*100:.1f}%:  {total}")
    print(f"  🎯 Відсоток закриття гепів: {close_rate:.1f}%")

    real_trades = [t for t in trades if t['res'] != 'OPEN']
    if real_trades:
        wins      = [t for t in real_trades if t['res'] == 'TP']
        losses    = [t for t in real_trades if t['res'] == 'SL']
        total_pnl = sum(t['pnl'] for t in real_trades)
        winrate   = len(wins) / len(real_trades) * 100

        actual_rr = SL_MULTIPLIER / 1.0  # Змінено: Прибуток / Ризик

        print(f"\n  💹 ЧЕСНА ТОРГОВА СИМУЛЯЦІЯ (Комісії 0.1% Spot):")
        print(f"  Ризик на угоду:    ${RISK_PER_TRADE}")
        print(f"  Реальний R:R:      {actual_rr:.2f} : 1 (Прибуток більший за Стоп)")
        print(f"  Угод завершено:    {len(real_trades)}")
        print(f"  ✅ TP (Продовження): {len(wins)}")
        print(f"  ❌ SL (Закриття):    {len(losses)}")
        print(f"  🎯 Winrate:        {winrate:.1f}%")
        print(f"  💰 Чистий PnL:     ${total_pnl:.2f}")

    print(f"\n  {'═'*45}")
    if total_pnl > 0:
        print(f"  ✅ ВЕРДИКТ: Стратегія математично прибуткова!")
    else:
        print(f"  ❌ ВЕРДИКТ: Моментуму не вистачає для покриття збитків.")

async def main():
    print("╔══════════════════════════════════════════════════════╗")
    print("║      CME GAP CONTINUATION — ДЗЕРКАЛЬНИЙ ТЕСТЕР       ║")
    print("║      Торгуємо за трендом гепу. Тейк 1.5R.            ║")
    print("╚══════════════════════════════════════════════════════╝\n")

    exchange = ccxt.binance()
    try:
        candles = await fetch_data(exchange, SYMBOL, DAYS)
        gaps, trades = analyze_cme_gaps(candles)
        print_results(gaps, trades)
    finally:
        await exchange.close()

if __name__ == "__main__":
    import sys
    try:
        import pytz
    except ImportError:
        print("Будь ласка, встанови pytz: pip install pytz")
        sys.exit(1)
        
    asyncio.run(main())
