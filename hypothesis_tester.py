import asyncio
import ccxt.async_support as ccxt
from datetime import datetime, timezone

# ═══════════════════════════════════════════════════════════
#  HYPOTHESIS TESTER: Чистий SMC Sweep & Reclaim (Без фільтрів)
# ═══════════════════════════════════════════════════════════

SYMBOLS           = ['BTC/USDT:USDT', 'SOL/USDT:USDT']
DAYS_TO_FETCH     = 180         # Півроку історії для величезної вибірки
RR_TARGET         = 2.0         # Тейк-профіт 2R
RISK_PER_TRADE    = 20          # $20 ризику
FEE_AND_SLIPPAGE  = 0.0007      # 0.05% taker + 0.02% слипаж (за кожен вхід і вихід)

async def fetch_historical_data(exchange, symbol, days):
    print(f"\n📥 Завантаження 1m OHLCV для {symbol} за {days} днів...")
    now = exchange.milliseconds()
    since = now - (days * 24 * 60 * 60 * 1000)
    all_candles = []

    while since < now:
        try:
            candles = await exchange.fetch_ohlcv(symbol, '1m', since, limit=1000)
            if not candles: break
            all_candles.extend(candles)
            since = candles[-1][0] + 60000
            print(f"   Завантажено {len(all_candles)} свічок...", end='\r')
            await asyncio.sleep(0.1)
        except Exception as e:
            await asyncio.sleep(2)

    print(f"\n✅ {symbol} завантажено: {len(all_candles)} свічок.")
    return all_candles

def apply_costs(pnl, entry_price, stop_dist_price):
    """Вираховуємо реальні комісії та слипаж"""
    if stop_dist_price <= 0: return pnl
    position_size_usd = RISK_PER_TRADE / (stop_dist_price / entry_price)
    cost = position_size_usd * (FEE_AND_SLIPPAGE * 2) # Вхід і вихід
    return pnl - cost

def test_pure_smc_hypothesis(symbol, candles_1m):
    print(f"\n🔬 Тестування гіпотези для {symbol}...")

    trades = []
    active_trade = None

    # Побудова 15m структури
    c_15m = {'open': 0, 'high': 0, 'low': float('inf'), 'close': 0, 'ts': 0}
    history_15m = []
    
    swing_high = 0.0
    swing_low = 0.0
    
    # Стан свіпу
    sweeping_low = False
    sweeping_high = False
    absolute_low_of_sweep = float('inf')
    absolute_high_of_sweep = 0.0

    for row in candles_1m:
        ts, o, h, l, c, v = row[0], row[1], row[2], row[3], row[4], row[5]

        # 1. Менеджмент відкритої угоди (тільки 2R або SL)
        if active_trade:
            # Щоб не закрити на тій самій свічці
            if ts != active_trade['entry_ts']:
                risk = abs(active_trade['entry'] - active_trade['sl'])
                
                if active_trade['side'] == 'LONG':
                    if l <= active_trade['sl']:
                        pnl = apply_costs(-RISK_PER_TRADE, active_trade['entry'], risk)
                        trades.append({'side': 'LONG', 'pnl': pnl, 'res': 'SL'})
                        active_trade = None
                    elif h >= active_trade['tp']:
                        pnl = apply_costs(RISK_PER_TRADE * RR_TARGET, active_trade['entry'], risk)
                        trades.append({'side': 'LONG', 'pnl': pnl, 'res': 'TP'})
                        active_trade = None
                        
                elif active_trade['side'] == 'SHORT':
                    if h >= active_trade['sl']:
                        pnl = apply_costs(-RISK_PER_TRADE, active_trade['entry'], risk)
                        trades.append({'side': 'SHORT', 'pnl': pnl, 'res': 'SL'})
                        active_trade = None
                    elif l <= active_trade['tp']:
                        pnl = apply_costs(RISK_PER_TRADE * RR_TARGET, active_trade['entry'], risk)
                        trades.append({'side': 'SHORT', 'pnl': pnl, 'res': 'TP'})
                        active_trade = None

        # 2. Формування 15m свічок і пошук Свінгів
        if c_15m['ts'] == 0: c_15m['ts'] = ts
        c_15m['high'] = max(c_15m['high'], h)
        c_15m['low'] = min(c_15m['low'], l)
        c_15m['close'] = c

        if (ts + 60000) % 900000 == 0:
            history_15m.append(dict(c_15m))
            if len(history_15m) > 10: history_15m.pop(0)

            # Простий 3-свічний фрактал
            if len(history_15m) >= 3:
                p, m, n = history_15m[-3], history_15m[-2], history_15m[-1]
                if m['high'] > p['high'] and m['high'] > n['high']:
                    swing_high = m['high']
                    sweeping_high = False 
                if m['low'] < p['low'] and m['low'] < n['low']:
                    swing_low = m['low']
                    sweeping_low = False

            c_15m = {'open': c, 'high': c, 'low': float('inf'), 'close': c, 'ts': ts + 60000}

        # 3. Детектор Свіпу та Повернення (1m логіка)
        if not active_trade:
            # --- LONG ЛОГІКА (Свіп Swing Low) ---
            if swing_low > 0:
                # Ціна пішла нижче рівня ліквідності
                if l < swing_low:
                    sweeping_low = True
                    absolute_low_of_sweep = min(absolute_low_of_sweep, l)
                
                # Повернення: якщо ми в стані свіпу, і свічка закрилась ВИЩЕ рівня
                if sweeping_low and c > swing_low:
                    # Фільтр шуму: стоп має бути мінімум 0.15% (захист від комісій)
                    dist = c - absolute_low_of_sweep
                    if dist > (c * 0.0015):
                        active_trade = {
                            'side': 'LONG',
                            'entry': c,
                            'sl': absolute_low_of_sweep,
                            'tp': c + (dist * RR_TARGET),
                            'entry_ts': ts
                        }
                    sweeping_low = False # Скидаємо стан
                    absolute_low_of_sweep = float('inf')

            # --- SHORT ЛОГІКА (Свіп Swing High) ---
            if swing_high > 0:
                if h > swing_high:
                    sweeping_high = True
                    absolute_high_of_sweep = max(absolute_high_of_sweep, h)
                
                if sweeping_high and c < swing_high:
                    dist = absolute_high_of_sweep - c
                    if dist > (c * 0.0015):
                        active_trade = {
                            'side': 'SHORT',
                            'entry': c,
                            'sl': absolute_high_of_sweep,
                            'tp': c - (dist * RR_TARGET),
                            'entry_ts': ts
                        }
                    sweeping_high = False
                    absolute_high_of_sweep = 0.0

    # Підсумки
    total = len(trades)
    if total == 0:
        return 0, 0, 0
    
    wins = len([t for t in trades if t['res'] == 'TP'])
    losses = len([t for t in trades if t['res'] == 'SL'])
    winrate = (wins / total) * 100
    
    total_pnl = sum(t['pnl'] for t in trades)
    
    print(f"  {'═'*40}")
    print(f"  Всього угод: {total}")
    print(f"  ✅ TP (2R):  {wins}")
    print(f"  ❌ SL (-1R): {losses}")
    print(f"  🎯 Winrate:  {winrate:.1f}%")
    print(f"  💰 PnL:      ${total_pnl:.2f} (з комісіями)")
    print(f"  {'═'*40}")
    
    return total, winrate, total_pnl

async def main():
    print("╔══════════════════════════════════════════════════════════╗")
    print("║     НАУКОВИЙ ТЕСТ: ЧИСТИЙ SMC SWEEP (180 ДНІВ)           ║")
    print("╚══════════════════════════════════════════════════════════╝")
    
    exchange = ccxt.binanceusdm({'options': {'defaultType': 'future'}})
    try:
        for sym in SYMBOLS:
            candles = await fetch_historical_data(exchange, sym, DAYS_TO_FETCH)
            test_pure_smc_hypothesis(sym, candles)
    finally:
        await exchange.close()

if __name__ == "__main__":
    asyncio.run(main())
