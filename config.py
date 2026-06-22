import os
from dotenv import load_dotenv

# Читаємо файл .env і кладемо значення в оточення
load_dotenv()  

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Отримуємо Chat ID і перетворюємо його на число (int) для надійності
chat_id_raw = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_CHAT_ID = int(chat_id_raw) if chat_id_raw else None

# SYMBOLS залишаємо тут, якщо він потрібен для інших скриптів
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

# Жорсткий запобіжник: якщо хоч одного ключа немає — миттєва зупинка
if not all([BINANCE_API_KEY, BINANCE_SECRET_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID]):
    raise SystemExit("❌ КРИТИЧНА ПОМИЛКА: Не знайдено ключі. Перевір файл .env поруч із config.py")
