class SignalTracker:
    def __init__(self):
        self.active_signals = {}

    def register_signal(self, symbol, direction, price):
        self.active_signals[symbol] = {
            'direction': direction,
            'entry_price': price
        }
        print(f"✅ Трекер: Сигнал {direction} по {symbol} взято на супровід.")

    def remove_signal(self, symbol):
        if symbol in self.active_signals:
            del self.active_signals[symbol]

    def check_exit_conditions(self, symbol, current_delta, matrix_status):
        if symbol not in self.active_signals:
            return None

        trade = self.active_signals[symbol]
        direction = trade['direction']

        if direction == 'LONG':
            if matrix_status in ['🔴 Shorts Opening (Справжній шорт)', '🟡 Longs Closing (Вихід з позицій)']:
                return f"Матричний розворот: {matrix_status}"
            if current_delta < -1000: 
                return "Згасання покупця (Сильна негативна дельта)"

        elif direction == 'SHORT':
            if matrix_status in ['🟢 Longs Opening (Справжній тренд)', '🟠 Shorts Closing (Шорт-сквіз)']:
                return f"Матричний розворот: {matrix_status}"
            if current_delta > 1000:
                return "Згасання продавця (Сильна позитивна дельта)"

        return None
