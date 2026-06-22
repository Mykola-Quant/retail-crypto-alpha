import asyncio
import logging
import time
from datetime import datetime
import ccxt.pro as ccxtpro
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import pytz
import config
import pandas as pd
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("DataCollector")

# --- Telegram токен тепер береться з config (додай: TELEGRAM_TOKEN = "...") ---
TG_TOKEN = getattr(config, "TELEGRAM_TOKEN", None)
if not TG_TOKEN:
    raise SystemExit("❌ Додай рядок  TELEGRAM_TOKEN = \"твій_токен\"  у config.py")

bot = Bot(token=TG_TOKEN)
dp = Dispatcher()

exchange = ccxtpro.binanceusdm({
    'apiKey': config.BINANCE_API_KEY,
    'secret': config.BINANCE_SECRET_KEY,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

SYMBOL = "BTCUSDT"
CCXT_SYMBOL = "BTC/USDT:USDT"
DATA_DIR = "btc_tick_data"
KYIV = pytz.timezone('Europe/Kyiv')

OI_FUNDING_POLL_SECONDS = 60   # як часто опитувати OI та funding (Binance оновлює OI ~раз/хв)
FLUSH_EVERY_SECONDS = 300      # скидати буфери на диск раз на 5 хв

os.makedirs(DATA_DIR, exist_ok=True)

# --- Три окремі буфери (різна схема/частота) ---
tick_buffer = []        # [timestamp_ms, price, amount, side, is_buyer_maker]
liq_buffer = []         # [timestamp_ms, side, price, amount, value_usd]
oi_funding_buffer = []  # [timestamp_ms, open_interest, oi_value_usd, funding_rate, mark_price]


# ============================================================
# УНІВЕРСАЛЬНИЙ ЗАПИС БУФЕРА НА ДИСК
# ============================================================
def flush_one(buffer, columns, prefix):
    """Скидає буфер у датований parquet. Чистить буфер на місці (.clear())."""
    if not buffer:
        return
    try:
        current_date = datetime.now(KYIV).strftime('%Y-%m-%d')
        file_path = os.path.join(DATA_DIR, f"{prefix}_{current_date}.parquet")
        df_new = pd.DataFrame(buffer, columns=columns)

        if os.path.exists(file_path):
            df_old = pd.read_parquet(file_path)
            df_combined = pd.concat([df_old, df_new], ignore_index=True)
        else:
            df_combined = df_new

        df_combined.to_parquet(file_path, engine='pyarrow', compression='snappy')
        logger.info(f"💾 [{prefix}] збережено {len(buffer)} записів → {file_path}")
        buffer.clear()
    except Exception as e:
        logger.error(f"Помилка запису {prefix}: {e}")


def flush_all():
    flush_one(tick_buffer, ['timestamp_ms', 'price', 'amount', 'side', 'is_buyer_maker'], "btc_ticks")
    flush_one(liq_buffer, ['timestamp_ms', 'side', 'price', 'amount', 'value_usd'], "btc_liquidations")
    flush_one(oi_funding_buffer, ['timestamp_ms', 'open_interest', 'oi_value_usd', 'funding_rate', 'mark_price'], "btc_oi_funding")


# ============================================================
# РАДАР (для живого /status та простих алертів) — без змін по суті
# ============================================================
class FlowRadar:
    def __init__(self):
        self.bins = {}
        self.last_alert_ts = 0

    def update_tick(self, price, amount, side, ts):
        if price <= 0 or amount <= 0:
            return
        minute_ts = int(ts / 60000) * 60000
        vol = price * amount
        if minute_ts not in self.bins:
            self.bins[minute_ts] = {'o': price, 'h': price, 'l': price, 'c': price, 'buy_v': 0.0, 'sell_v': 0.0}
        b = self.bins[minute_ts]
        b['c'] = price
        b['h'] = max(b['h'], price)
        b['l'] = min(b['l'], price)
        if side == 'buy':
            b['buy_v'] += vol
        elif side == 'sell':
            b['sell_v'] += vol
        old_keys = [k for k in self.bins.keys() if k < minute_ts - 3600000]
        for k in old_keys:
            del self.bins[k]

    def get_stats(self, minutes_window=5):
        now_ts = int(time.time() * 1000)
        start_ts = int(now_ts / 60000) * 60000 - (minutes_window * 60000)
        valid_bins = [b for k, b in sorted(self.bins.items()) if k >= start_ts]
        if not valid_bins:
            return None
        open_p, close_p = valid_bins[0]['o'], valid_bins[-1]['c']
        price_change = (close_p - open_p) / open_p if open_p > 0 else 0
        buy_v = sum(b['buy_v'] for b in valid_bins)
        sell_v = sum(b['sell_v'] for b in valid_bins)
        total_v = buy_v + sell_v
        delta = buy_v - sell_v
        delta_pct = delta / total_v if total_v > 0 else 0
        return price_change, delta_pct, total_v, close_p, delta


radar = FlowRadar()

# Останні відомі значення OI/funding (для /status)
last_oi = {"oi": 0.0, "funding": 0.0, "mark": 0.0}


def is_ny_open_zone():
    now = datetime.now(KYIV)
    minutes = now.hour * 60 + now.minute
    return (15 * 60 + 30) <= minutes <= (17 * 60 + 30)


# ============================================================
# ПАРСЕРИ (винесені окремо, щоб були стійкі до різних версій ccxt)
# ============================================================
def parse_liquidation(liq):
    """Повертає [ts_ms, side, price, amount, value_usd] або None."""
    info = liq.get('info', {}) or {}
    o = info.get('o', info)  # Binance forceOrder загорнутий у 'o'
    try:
        ts = liq.get('timestamp') or info.get('E') or o.get('T')
        side = liq.get('side') or o.get('S')
        price = liq.get('price') or o.get('ap') or o.get('p')
        amount = liq.get('amount') or liq.get('contracts') or o.get('q') or o.get('z')
        if ts is None or price is None or amount is None:
            return None
        price = float(price)
        amount = float(amount)
        value = liq.get('quoteValue')
        value = float(value) if value is not None else price * amount
        return [int(ts), str(side).upper(), price, amount, value]
    except (TypeError, ValueError):
        return None


def parse_oi_funding(oi_data, fr_data):
    """Повертає [ts_ms, open_interest, oi_value_usd, funding_rate, mark_price] або None."""
    try:
        ts = int(time.time() * 1000)
        oi_amt = oi_data.get('openInterestAmount')
        if oi_amt is None:
            oi_amt = (oi_data.get('info', {}) or {}).get('openInterest')
        oi_amt = float(oi_amt) if oi_amt is not None else 0.0

        oi_val = oi_data.get('openInterestValue')
        oi_val = float(oi_val) if oi_val is not None else 0.0

        funding = fr_data.get('fundingRate')
        funding = float(funding) if funding is not None else 0.0

        mark = fr_data.get('markPrice')
        mark = float(mark) if mark is not None else 0.0

        if oi_val == 0.0 and mark > 0:
            oi_val = oi_amt * mark
        return [ts, oi_amt, oi_val, funding, mark]
    except (TypeError, ValueError):
        return None


# ============================================================
# СЛУХАЧ 1: ТІКИ (угоди)
# ============================================================
async def btc_trades_listener():
    logger.info("🎧 [TRADES] Підключення до WebSocket угод...")
    while True:
        try:
            trades = await exchange.watch_trades(CCXT_SYMBOL)
            for t in trades:
                p = float(t['price'])
                a = float(t['amount'])
                side = t['side']
                ts = t['timestamp']
                is_maker = t['info'].get('m', False)
                radar.update_tick(p, a, side, ts)
                tick_buffer.append([ts, p, a, side, is_maker])
        except Exception as e:
            logger.error(f"Збій стріму угод: {e}")
            await asyncio.sleep(5)


# ============================================================
# СЛУХАЧ 2: ЛІКВІДАЦІЇ (forceOrder)
# ============================================================
async def btc_liquidations_listener():
    logger.info("💥 [LIQ] Підключення до WebSocket ліквідацій...")
    while True:
        try:
            liqs = await exchange.watch_liquidations(CCXT_SYMBOL)
            for liq in liqs:
                row = parse_liquidation(liq)
                if row:
                    liq_buffer.append(row)
                    logger.info(f"💥 Ліквідація {row[1]} {row[3]:.4f} @ {row[2]:.2f} (${row[4]:,.0f})")
        except AttributeError:
            logger.error("⚠️ Твоя версія ccxt.pro не має watch_liquidations. "
                         "Онови: pip install -U ccxt. Слухач ліквідацій вимкнено.")
            return
        except Exception as e:
            logger.error(f"Збій стріму ліквідацій: {e}")
            await asyncio.sleep(5)


# ============================================================
# ПОЛЛЕР 3: OPEN INTEREST + FUNDING (REST, раз на хвилину)
# ============================================================
async def oi_funding_poller():
    logger.info("📈 [OI/FUNDING] Поллер запущено (REST, раз на хвилину).")
    while True:
        try:
            oi_data = await exchange.fetch_open_interest(CCXT_SYMBOL)
            fr_data = await exchange.fetch_funding_rate(CCXT_SYMBOL)
            row = parse_oi_funding(oi_data, fr_data)
            if row:
                oi_funding_buffer.append(row)
                last_oi["oi"] = row[1]
                last_oi["funding"] = row[3]
                last_oi["mark"] = row[4]
        except Exception as e:
            logger.error(f"Помилка опитування OI/funding: {e}")
        await asyncio.sleep(OI_FUNDING_POLL_SECONDS)


# ============================================================
# ЦИКЛ: ЗАПИС НА ДИСК + АЛЕРТИ
# ============================================================
async def flush_and_alert_loop():
    logger.info("🔬 Цикл запису на диск та алертів активовано.")
    last_flush = time.time()
    while True:
        await asyncio.sleep(30)

        if time.time() - last_flush >= FLUSH_EVERY_SECONDS:
            flush_all()
            last_flush = time.time()

        stats_5m = radar.get_stats(5)
        if not stats_5m or (time.time() - radar.last_alert_ts < 900):
            continue
        p_chg, d_pct, vol, price, delta_usd = stats_5m
        if vol < 5_000_000:
            continue

        alert_type, emoji = None, ""
        if p_chg >= 0.003 and d_pct <= -0.15:
            alert_type, emoji = "BEARISH DIVERGENCE (Фейковий Памп)", "🚨"
        elif p_chg <= -0.003 and d_pct >= 0.15:
            alert_type, emoji = "BULLISH DIVERGENCE (Фейковий Дамп)", "🟢"
        elif abs(p_chg) <= 0.001 and d_pct >= 0.25:
            alert_type, emoji = "BEARISH ABSORPTION (Стіна Продавця)", "🧱📉"
        elif abs(p_chg) <= 0.001 and d_pct <= -0.25:
            alert_type, emoji = "BULLISH ABSORPTION (Стіна Покупця)", "🧱📈"

        if alert_type:
            ny_tag = "🇺🇸 **[NY OPEN]**" if is_ny_open_zone() else "🌍 [Global]"
            msg = (f"{emoji} **Ордерфлоу Аномалія: BTCUSDT**\n"
                   f"Тип: `{alert_type}`\nСесія: {ny_tag}\n\n"
                   f"📊 **Метрики (5 хв):**\n"
                   f"• Зміна ціни: `{p_chg*100:+.2f}%`\n"
                   f"• Дельта: `{d_pct*100:+.1f}%`\n"
                   f"• Тиск: `${delta_usd/1_000_000:+.2f}M`\n"
                   f"• Об'єм: `${vol/1_000_000:.2f}M`\n"
                   f"• Funding: `{last_oi['funding']*100:+.4f}%`")
            try:
                await bot.send_message(getattr(config, "TELEGRAM_CHAT_ID", None), msg, parse_mode="Markdown")
                radar.last_alert_ts = time.time()
            except Exception:
                pass


# ============================================================
# TELEGRAM КОМАНДИ
# ============================================================
@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    stats = radar.get_stats(5)
    ny_status = "🔥 АКТИВНА" if is_ny_open_zone() else "💤 Очікування"
    response = "📡 **BTC Data Collector**\n\n"
    if stats:
        p_chg, d_pct, tot_v, price, delta = stats
        trend = "🟢 Вгору" if p_chg > 0 else "🔴 Вниз"
        response += f"**BTCUSDT** (`{price:.2f}`)\n"
        response += f"├ Рух 5m: {p_chg*100:+.2f}% ({trend})\n"
        response += f"├ Об'єм 5m: ${tot_v/1_000_000:.2f}M\n"
        response += f"└ Дельта 5m: {d_pct*100:+.1f}%\n\n"
    else:
        response += "Накопичую перші тіки...\n\n"
    response += f"📈 OI: `{last_oi['oi']:,.0f}` BTC | Funding: `{last_oi['funding']*100:+.4f}%`\n\n"
    response += f"🇺🇸 **NY:** {ny_status}\n"
    response += (f"📦 **Буфери RAM:** тіки `{len(tick_buffer)}` | "
                 f"ліквід. `{len(liq_buffer)}` | OI `{len(oi_funding_buffer)}`")
    await message.answer(response, parse_mode="Markdown")


# ============================================================
# MAIN
# ============================================================
async def main():
    try:
        await bot.send_message(getattr(config, "TELEGRAM_CHAT_ID", None),
                               "🚀 **Збирач даних активовано!**\nПишу: тіки + ліквідації + OI + funding.",
                               parse_mode="Markdown")
    except Exception:
        pass

    tasks = [
        asyncio.create_task(btc_trades_listener()),
        asyncio.create_task(btc_liquidations_listener()),
        asyncio.create_task(oi_funding_poller()),
        asyncio.create_task(flush_and_alert_loop()),
        asyncio.create_task(dp.start_polling(bot)),
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        flush_all()  # скидаємо все, що лишилось у буферах
        await exchange.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        flush_all()
