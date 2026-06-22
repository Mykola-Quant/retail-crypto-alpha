import logging
import asyncio
import time
import config

logger = logging.getLogger("PositionManager")

class PositionManager:
    def __init__(self, exchange, bot, db):
        self.exchange = exchange
        self.bot = bot
        self.db = db
        self.active_positions = {}
        self.pending_orders = {}

    async def place_limit_order(self, symbol, side, limit_price, stop_loss, take_profit, qty):
        order_id = f"ORDER_{int(time.time())}"
        self.pending_orders[symbol] = {
            'id': order_id, 
            'symbol': symbol, 
            'side': side,
            'price': limit_price, 
            'sl': stop_loss, 
            'tp': take_profit,
            'qty': qty, 
            'sl_initial': stop_loss, 
            'is_trailing': False # ✨ Прапорець Трейлінгу v6.0
        }
        logger.info(f"📝 [{symbol}] Виставлено ліміт {side} по {limit_price:.2f}")
        await self._simulate_fill(symbol)

    async def _simulate_fill(self, symbol):
        if symbol in self.pending_orders:
            order = self.pending_orders.pop(symbol)
            self.active_positions[symbol] = {
                'side': order['side'], 
                'entry': order['price'],
                'sl': order['sl'], 
                'tp': order['tp'],
                'sl_initial': order['sl_initial'], 
                'is_trailing': order['is_trailing'],
                'qty': order['qty']
            }
            logger.info(f"✅ [{symbol}] Угода ВІДКРИТА. Вхід: {order['price']:.2f}, SL: {order['sl']:.2f}, TP: {order['tp']:.2f}")
            await self._notify_telegram(f"🟢 **Відкрито {order['side']}** по {symbol}\nВхід: `{order['price']:.2f}`\nSL: `{order['sl']:.2f}`\nTP: `{order['tp']:.2f}`")

    async def monitor_and_manage_positions(self, current_prices):
        for symbol, pos in list(self.active_positions.items()):
            current_price = current_prices.get(symbol)
            if not current_price: continue

            side = pos['side']
            entry = pos['entry']
            tp = pos['tp']
            sl_initial = pos.get('sl_initial', pos['sl'])
            is_trailing = pos.get('is_trailing', False)

            risk = abs(entry - sl_initial)

            if side == 'LONG':
                # ✨ Trailing Stop v6.0: Прибуток досяг +1.5R, переносимо стоп на +0.5R
                if not is_trailing and current_price >= entry + (risk * 1.5):
                    pos['is_trailing'] = True
                    pos['sl'] = entry + (risk * 0.5)
                    logger.info(f"🛡️ [{symbol}] Трейлінг-стоп активовано (+0.5R). SL = {pos['sl']:.2f}")
                    await self._notify_telegram(f"🛡️ **{symbol}** Трейлінг-стоп активовано (+0.5R).")

                if current_price <= pos['sl']:
                    reason = "TRAIL_PROFIT" if pos['is_trailing'] else "SL"
                    await self.close_position(symbol, pos['sl'], reason)
                elif current_price >= tp:
                    await self.close_position(symbol, tp, "TP")

            elif side == 'SHORT':
                # ✨ Trailing Stop v6.0: Прибуток досяг +1.5R, переносимо стоп на +0.5R
                if not is_trailing and current_price <= entry - (risk * 1.5):
                    pos['is_trailing'] = True
                    pos['sl'] = entry - (risk * 0.5)
                    logger.info(f"🛡️ [{symbol}] Трейлінг-стоп активовано (+0.5R). SL = {pos['sl']:.2f}")
                    await self._notify_telegram(f"🛡️ **{symbol}** Трейлінг-стоп активовано (+0.5R).")

                if current_price >= pos['sl']:
                    reason = "TRAIL_PROFIT" if pos['is_trailing'] else "SL"
                    await self.close_position(symbol, pos['sl'], reason)
                elif current_price <= tp:
                    await self.close_position(symbol, tp, "TP")

    async def close_position(self, symbol, close_price, reason):
        if symbol not in self.active_positions: return
        pos = self.active_positions.pop(symbol)
        side = pos['side']
        entry = pos['entry']
        
        if side == 'LONG':
            pnl = (close_price - entry) * pos['qty']
        else:
            pnl = (entry - close_price) * pos['qty']

        icon = "✅" if pnl > 0 else ("🛡️" if reason == "TRAIL_PROFIT" else "❌")
        
        # ✨ Захист від тільту: фіксація збитку для ліміту на день (Max 2 Daily Losses)
        if reason == "SL":
            config.daily_stops[symbol]['count'] += 1
            
        logger.info(f"{icon} [{symbol}] Угоду ЗАКРИТО ({reason}). PnL: {pnl:.2f} USD")
        await self._notify_telegram(f"{icon} **Угоду ЗАКРИТО** по {symbol}\nПричина: `{reason}`\nPnL: `{pnl:.2f} USD`")
        
        try:
            await self.db.save_trade(symbol, side, entry, close_price, pnl, reason)
        except Exception as e: 
            logger.error(f"Помилка збереження в БД: {e}")

    async def _notify_telegram(self, text):
        chat_id = getattr(config, "TELEGRAM_CHAT_ID", None)
        if chat_id:
            try:
                await self.bot.send_message(chat_id, text, parse_mode="Markdown")
            except Exception: pass
