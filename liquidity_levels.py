import logging
import time

logger = logging.getLogger("LiquidityLevels")

class LiquidityLevels:
    def __init__(self, symbol, max_candles=5):
        self.symbol = symbol
        self.max_candles = max_candles
        self.candle_history = [] 
        
        self.levels = {
            "swing_high": 0.0,
            "swing_low": 0.0,
            "session_high": 0.0,
            "session_low": 0.0,
            "poc_24h": 0.0
        }

        self.high_swept = False
        self.low_swept = False
        self.sweep_high_triggered = False
        self.sweep_low_triggered = False
        self.sweep_high_ts = 0.0
        self.sweep_low_ts = 0.0
        self.level_high_formed_ts = 0.0
        self.level_low_formed_ts = 0.0

    def update_from_closed_candle(self, closed_candle: dict):
        self.candle_history.append(closed_candle)
        if len(self.candle_history) > self.max_candles:
            self.candle_history.pop(0)

        self._update_session_extremes(closed_candle['high'], closed_candle['low'])
        self._detect_structural_swings()

    def _update_session_extremes(self, high, low):
        if self.levels["session_high"] == 0.0 or high > self.levels["session_high"]:
            self.levels["session_high"] = high
        if self.levels["session_low"] == 0.0 or low < self.levels["session_low"]:
            self.levels["session_low"] = low

    def _detect_structural_swings(self):
        if len(self.candle_history) < 3:
            return

        prev_candle = self.candle_history[-3]
        target_candle = self.candle_history[-2]
        current_candle = self.candle_history[-1]

        if target_candle['high'] > prev_candle['high'] and target_candle['high'] > current_candle['high']:
            new_swing_high = target_candle['high']
            if new_swing_high != self.levels["swing_high"]:
                self.levels["swing_high"] = new_swing_high
                self.level_high_formed_ts = time.time()
                self.high_swept = False  
                self.sweep_high_triggered = False
                logger.info(f"🎯 [{self.symbol}] Новий Swing High: {new_swing_high:.2f}")

        if target_candle['low'] < prev_candle['low'] and target_candle['low'] < current_candle['low']:
            new_swing_low = target_candle['low']
            if new_swing_low != self.levels["swing_low"]:
                self.levels["swing_low"] = new_swing_low
                self.level_low_formed_ts = time.time()
                self.low_swept = False
                self.sweep_low_triggered = False
                logger.info(f"🎯 [{self.symbol}] Новий Swing Low: {new_swing_low:.2f}")

    def update_tick_state(self, current_price: float):
        now = time.time()
        max_level_age = 28800  

        if self.levels["swing_high"] > 0.0 and (now - self.level_high_formed_ts <= max_level_age):
            if current_price > self.levels["swing_high"]:
                self.high_swept = True  
            elif current_price <= self.levels["swing_high"] and self.high_swept:
                self.sweep_high_triggered = True  
                self.sweep_high_ts = now
                self.high_swept = False

        if self.levels["swing_low"] > 0.0 and (now - self.level_low_formed_ts <= max_level_age):
            if current_price < self.levels["swing_low"]:
                self.low_swept = True   
            elif current_price >= self.levels["swing_low"] and self.low_swept:
                self.sweep_low_triggered = True  
                self.sweep_low_ts = now
                self.low_swept = False

    def check_and_consume_sweep(self, adaptive_max_age_ms: float = 1800000.0) -> str:
        now = time.time()
        max_age_seconds = adaptive_max_age_ms / 1000.0

        valid_high = self.sweep_high_triggered and (now - self.sweep_high_ts <= max_age_seconds)
        valid_low = self.sweep_low_triggered and (now - self.sweep_low_ts <= max_age_seconds)

        self.sweep_high_triggered = False
        self.sweep_low_triggered = False

        if valid_high and valid_low:
            if self.sweep_high_ts > self.sweep_low_ts:
                return "SWEEP_HIGH"
            else:
                return "SWEEP_LOW"
        elif valid_high:
            return "SWEEP_HIGH"
        elif valid_low:
            return "SWEEP_LOW"

        return None

    def reset_session_levels(self, high, low):
        self.levels["session_high"] = high
        self.levels["session_low"] = low
