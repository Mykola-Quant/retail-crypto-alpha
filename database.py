import aiosqlite
import logging

DB_NAME = "bot_data.db"

logger = logging.getLogger("BotDatabase")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS liquidity_levels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE,
                btc_pdh REAL, btc_pdl REAL, btc_pwh REAL, btc_pwl REAL,
                eth_pdh REAL, eth_pdl REAL, eth_pwh REAL, eth_pwl REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                symbol TEXT,
                side TEXT,
                entry_price REAL,
                exit_price REAL,
                pnl REAL,
                reason TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reason_logs (
                date TEXT PRIMARY KEY,
                symbol TEXT,
                skipped_by_atr_range INTEGER DEFAULT 0,
                skipped_by_cooldown INTEGER DEFAULT 0,
                skipped_by_time_window INTEGER DEFAULT 0,
                skipped_by_spread INTEGER DEFAULT 0
            )
        """)
        await db.commit()
    logger.info("Базу даних успішно ініціалізовано.")

async def save_liquidity_levels(date_str, btc_levels, eth_levels):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            INSERT INTO liquidity_levels (
                date, btc_pdh, btc_pdl, btc_pwh, btc_pwl, eth_pdh, eth_pdl, eth_pwh, eth_pwl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                btc_pdh=excluded.btc_pdh, btc_pdl=excluded.btc_pdl,
                btc_pwh=excluded.btc_pwh, btc_pwl=excluded.btc_pwl,
                eth_pdh=excluded.eth_pdh, eth_pdl=excluded.eth_pdl,
                eth_pwh=excluded.eth_pwh, eth_pwl=excluded.eth_pwl
        """, (
            date_str,
            btc_levels['pdh'], btc_levels['pdl'], btc_levels['pwh'], btc_levels['pwl'],
            eth_levels['pdh'], eth_levels['pdl'], eth_levels['pwh'], eth_levels['pwl']
        ))
        await db.commit()

async def get_liquidity_levels(date_str):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT btc_pdh, btc_pdl, btc_pwh, btc_pwl, eth_pdh, eth_pdl, eth_pwh, eth_pwl FROM liquidity_levels WHERE date = ?", 
            (date_str,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "BTCUSDT": {"pdh": row[0], "pdl": row[1], "pwh": row[2], "pwl": row[3]},
                    "ETHUSDT": {"pdh": row[4], "pdl": row[5], "pwh": row[6], "pwl": row[7]}
                }
    return None

async def log_trade(symbol, side, entry, exit_p, pnl, reason=""):
    from datetime import datetime
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            INSERT INTO trades (timestamp, symbol, side, entry_price, exit_price, pnl, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (datetime.utcnow().isoformat(), symbol, side, entry, exit_p, pnl, reason))
        await db.commit()
    logger.info(f"Угоду по {symbol} зафіксовано в БД. PnL: {pnl}")

# --- НОВА ФУНКЦІЯ ДЛЯ ЗВІТІВ ---
async def get_trade_statistics():
    """Рахує загальну статистику з усіх записаних угод."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*), SUM(pnl) FROM trades") as cursor:
            row = await cursor.fetchone()
            total_trades = row[0] if row[0] else 0
            total_pnl = row[1] if row[1] else 0.0
            
        async with db.execute("SELECT COUNT(*) FROM trades WHERE pnl > 0") as cursor:
            winning_trades = (await cursor.fetchone())[0] or 0
            
        async with db.execute("SELECT COUNT(*) FROM trades WHERE pnl <= 0") as cursor:
            losing_trades = (await cursor.fetchone())[0] or 0

    winrate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0
    
    return {
        "total": total_trades,
        "wins": winning_trades,
        "losses": losing_trades,
        "winrate": winrate,
        "pnl": total_pnl
    }
async def get_recent_trades(limit=5):
    """Повертає останні N угод з бази даних для Telegram-звіту."""
    # Тепер бот звертається до правильного файлу через DB_NAME
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT symbol, side, entry_price, exit_price, pnl, reason, timestamp FROM trades ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        rows = await cursor.fetchall()
        return rows
