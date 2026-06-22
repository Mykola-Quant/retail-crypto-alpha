import asyncio
import ccxt.async_support as ccxt
from datetime import datetime, timezone
import math

# ═══════════════════════════════════════════════════════════
#  HYPOTHESIS 1: SESSION OPEN BIAS (NEW YORK OPEN)
# ═══════════════════════════════════════════════════════════

SYMBOLS           = ['BTC/USDT:USDT', 'SOL/USDT:USDT']
DAYS_TO_FETCH     = 180         # Півроку
MIN_PRE_NY_MOVE   = 0.004       # 0.4% мінімальний рух до відкриття NY (щоб відсіяти флет)
RISK_PER_TRADE    = 100         # $100 фіксованого розміру позиції для чистоти PnL
FEE_AND_SLIPPAGE  = 0.0007      # 0.07% загальні втрати на 1 транзакцію (0.14% round trip)

async def fetch_historical_data(exchange, symbol, days):
    print(f"📥 Завантаження 15m OHLCV для {symbol} за {days} днів...")
    now = exchange.milliseconds()
    since = now - (days * 24 * 60 * 60 * 1000)
    all_candles = []

    while since < now:
        try:
            # Беремо 15m свічки для швидкості та точності сесій
            candles = await exchange.fetch_ohlcv(symbol, '15m', since, limit=1000)
            if not candles: break
            all_candles.extend(candles)
            since = candles[-1][0] + (15 * 60 * 1000)
            await asyncio.sleep(0.05)
        except Exception as e:
            await asyncio.sleep(1)

    print(f"✅ {symbol}: завантажено {len(all_candles)} свічок.")
    return all_candles

def test_session_bias(symbol, candles):
    print(f"\n🔬 Аналіз Часового Арбітражу для {symbol}...")

    daily_data = {}
    
    # Парсимо свічки по днях і годинах
    for row in candles:
        ts, o, h, l, c, v = row[0], row[1], row[2], row[3], row[4], row[5]
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        date_str = dt.strftime('%Y-%m-%d')
        time_str = dt.strftime('%H:%M')

        if date_str not in daily_data:
            daily_data[date_str] = {'pre_open': None, 'ny_open': None, 'ny_close': None}

        # 09:30 UTC - Початок активного Лондону / Pre-NY
        if time_str == '09:30': daily_data[date_str]['pre_open'] = o
        
        # 13:30 UTC - Відкриття Нью-Йорка (US Equities Open)
        if time_str == '13:30': daily_data[date_str]['ny_open'] = o
        
        # 15:30 UTC - Кінець перших двох годин Нью-Йорка (закриття нашої угоди)
        if time_str == '15:30': daily_data[date_str]['ny_close'] = o

    valid_days = 0
    reversals = 0
    continuations = 0
    
    reversal_pnl = 0.0
    continuation_pnl = 0.0

    # Аналізуємо кожен день
    for date, prices in daily_data.items():
        if prices['pre_open'] is None or prices['ny_open'] is None or prices['ny_close'] is None:
            continue  # Пропускаємо дні з неповними даними

        pre_ny_return = (prices['ny_open'] - prices['pre_open']) / prices['pre_open']
        ny_open_return = (prices['ny_close'] - prices['ny_open']) / prices['ny_open']

        # Якщо Лондон не зрушив ціну достатньо сильно, пропускаємо день
        if abs(pre_ny_return) < MIN_PRE_NY_MOVE:
            continue

        valid_days += 1
        
        # Розрахунок PnL (за розміру позиції $100)
        # Вираховуємо відсоток руху мінус 0.14% на комісії/слипаж туди-назад
        raw_pct_move_reversal = -ny_open_return if pre_ny_return > 0 else ny_open_return
        raw_pct_move_continuation = ny_open_return if pre_ny_return > 0 else -ny_open_return
        
        pnl_rev = RISK_PER_TRADE * (raw_pct_move_reversal - (FEE_AND_SLIPPAGE * 2))
        pnl_cont = RISK_PER_TRADE * (raw_pct_move_continuation - (FEE_AND_SLIPPAGE * 2))

        reversal_pnl += pnl_rev
        continuation_pnl += pnl_cont

        # Статистика ймовірностей
        if (pre_ny_return > 0 and ny_open_return < 0) or (pre_ny_return < 0 and ny_open_return > 0):
            reversals += 1
        else:
            continuations += 1

    # Виведення результатів
    if valid_days == 0:
        print("  Не знайдено жодного валідного дня.")
        return

    rev_winrate = (reversals / valid_days) * 100
    cont_winrate = (continuations / valid_days) * 100

    print(f"  {'═'*50}")
    print(f"  📅 Всього проаналізовано днів: {len(daily_data)}")
    print(f"  🎯 Днів з волатильним Pre-NY (>0.4%): {valid_days}")
    print(f"  {'─'*50}")
    print(f"  🔄 СТРАТЕГІЯ: MEAN REVERSAL (Відіграти проти Лондону)")
    print(f"  Winrate (розворотів): {rev_winrate:.1f}% ({reversals} днів)")
    print(f"  Чистий PnL (позиція $100): ${reversal_pnl:.2f}")
    print(f"  {'─'*50}")
    print(f"  🚀 СТРАТЕГІЯ: CONTINUATION (Піти за трендом Лондону)")
    print(f"  Winrate (продовжень): {cont_winrate:.1f}% ({continuations} днів)")
    print(f"  Чистий PnL (позиція $100): ${continuation_pnl:.2f}")
    print(f"  {'═'*50}")

async def main():
    print("╔══════════════════════════════════════════════════════════╗")
    print("║     НАУКОВИЙ ТЕСТ: SESSION OPEN BIAS (180 ДНІВ)          ║")
    print("╚══════════════════════════════════════════════════════════╝")
    
    exchange = ccxt.binanceusdm({'options': {'defaultType': 'future'}})
    try:
        for sym in SYMBOLS:
            candles = await fetch_historical_data(exchange, sym, DAYS_TO_FETCH)
            test_session_bias(sym, candles)
    finally:
        await exchange.close()

if __name__ == "__main__":
    asyncio.run(main())
