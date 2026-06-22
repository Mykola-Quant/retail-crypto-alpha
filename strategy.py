import logging
import math
from datetime import datetime
import config

logger = logging.getLogger("MarketStrategy")

class MarketStrategy:
    def __init__(self, symbol, exchange, db_module):
        self.symbol = symbol
        self.ccxt_sym = symbol.replace("USDT", "/USDT:USDT")
        self.exchange = exchange
        self.db = db_module
        
        self.levels = {"pdh": 0.0, "pdl": 0.0, "pwh": 0.0, "pwl": 0.0, "vwap": 0.0, "vwap_upper": 0.0, "vwap_lower": 0.0, "local_high": 0.0, "local_low": 0.0}
        self.atr_value = 0.0
        self.atr_1h_value = 0.0 
        self.avg_atr_24h = 0.0  # ✨ Додано для фільтру новин
        self.history_5m = []
        self.htf_trend = "FLAT" 

    def calculate_ema(self, prices, period):
        if len(prices) < period: return prices[-1]
        ema = prices[0]
        multiplier = 2 / (period + 1)
        for price in prices[1:]:
            ema = (price - ema) * multiplier + ema
        return ema

    async def update_htf_trend(self):
        try:
            candles_4h = await self.exchange.fetch_ohlcv(self.ccxt_sym, timeframe='4h', limit=100)
            if len(candles_4h) >= 50:
                closes = [float(c[4]) for c in candles_4h]
                
                # ✨ Dual EMA Filter v6.0
                ema_21 = self.calculate_ema(closes[-21:], 21) if len(closes) >= 21 else closes[-1]
                ema_50 = self.calculate_ema(closes, 50)
                
                if ema_21 > ema_50: self.htf_trend = "UP"
                elif ema_21 < ema_50: self.htf_trend = "DOWN"
                else: self.htf_trend = "CONFLICT"
                
                logger.info(f"🧭 [{self.symbol}] 4H Тренд: {self.htf_trend} (EMA21: {ema_21:.2f}, EMA50: {ema_50:.2f})")
        except Exception as e:
            logger.error(f"Помилка тренду {self.symbol}: {e}")

    async def _calculate_atr(self):
        try:
            daily_candles = await self.exchange.fetch_ohlcv(self.ccxt_sym, timeframe='1d', limit=15)
            tr_sum = 0.0
            for i in range(1, len(daily_candles)):
                tr = max(daily_candles[i][2] - daily_candles[i][3], 
                         abs(daily_candles[i][2] - daily_candles[i-1][4]), 
                         abs(daily_candles[i][3] - daily_candles[i-1][4]))
                tr_sum += tr
            self.atr_value = float(tr_sum / (len(daily_candles) - 1))
        except Exception as e: pass

    async def _calculate_1h_atr(self):
        try:
            hourly_candles = await self.exchange.fetch_ohlcv(self.ccxt_sym, timeframe='1h', limit=30)
            trs = []
            for i in range(1, len(hourly_candles)):
                tr = max(hourly_candles[i][2] - hourly_candles[i][3], 
                         abs(hourly_candles[i][2] - hourly_candles[i-1][4]), 
                         abs(hourly_candles[i][3] - hourly_candles[i-1][4]))
                trs.append(tr)
            
            if len(trs) >= 24:
                self.atr_1h_value = float(sum(trs[-10:]) / 10)
                self.avg_atr_24h = float(sum(trs[-24:]) / 24)
            logger.info(f"📏 [{self.symbol}] 1H ATR: {self.atr_1h_value:.2f} | 24H ATR: {self.avg_atr_24h:.2f}")
        except Exception as e: 
            logger.error(f"⚠️ Помилка розрахунку 1H ATR {self.symbol}: {e}. Використовуємо Fallback.")
            self.atr_1h_value = self.atr_value / 6.0
            self.avg_atr_24h = self.atr_value / 6.0

    def update_5m_history(self, closed_candle):
        self.history_5m.append(closed_candle)
        if len(self.history_5m) > 288:
            self.history_5m.pop(0)

    async def update_intraday_context(self):
        if len(self.history_5m) < 12: return
        try:
            current_utc_day = datetime.utcnow().date()
            today_candles = [c for c in self.history_5m if datetime.utcfromtimestamp(c['timestamp']/1000).date() == current_utc_day]

            if not today_candles: return 

            total_vol = sum([c['volume'] for c in today_candles]) 
            if total_vol > 0:
                vwap = sum([((c['high'] + c['low'] + c['close']) / 3) * c['volume'] for c in today_candles]) / total_vol
                self.levels['vwap'] = vwap
                
                variance = sum([c['volume'] * ((((c['high'] + c['low'] + c['close']) / 3) - vwap)**2) for c in today_candles]) / total_vol
                std_dev = math.sqrt(variance)
                
                self.levels['vwap_upper'] = vwap + (std_dev * 2.5)
                self.levels['vwap_lower'] = vwap - (std_dev * 2.5)
                
            self.levels['local_high'] = max([c['high'] for c in self.history_5m[-12:]])
            self.levels['local_low'] = min([c['low'] for c in self.history_5m[-12:]])
        except Exception as e:
            logger.error(f"Помилка контексту {self.symbol}: {e}")

    async def fetch_and_sync_levels(self):
        try:
            await self._calculate_atr()
            await self._calculate_1h_atr()
            await self.update_htf_trend()
            
            daily_candles = await self.exchange.fetch_ohlcv(self.ccxt_sym, timeframe='1d', limit=10)
            if len(daily_candles) >= 8:
                yesterday = daily_candles[-2]
                self.levels["pdh"], self.levels["pdl"] = float(yesterday[2]), float(yesterday[3])
                
                week_high = max([float(c[2]) for c in daily_candles[-8:-1]])
                week_low = min([float(c[3]) for c in daily_candles[-8:-1]])
                self.levels["pwh"], self.levels["pwl"] = week_high, week_low
                
                logger.info(f"🎯 [{self.symbol}] Рівні: PDH={self.levels['pdh']}, PDL={self.levels['pdl']}")
        except Exception as e:
            logger.error(f"Помилка ініціалізації {self.symbol}: {e}")
