import pandas as pd
import os

FILE_PATH = "btc_tick_data/btc_ticks_2026-06-12.parquet"

if not os.path.exists(FILE_PATH):
    print(f"❌ Файл {FILE_PATH} не знайдено. Перевір назву або почекай, поки бот запише дані.")
    exit()

print("📥 Завантаження Parquet файлу...")
df = pd.read_parquet(FILE_PATH)

# Перетворюємо timestamp в читаємий формат для аналізу
df['datetime'] = pd.to_datetime(df['timestamp_ms'], unit='ms', utc=True)
df['datetime'] = df['datetime'].dt.tz_convert('Europe/Kyiv')

print("\n" + "═"*50)
print("       📊 АУДИТ ДАТАСЕТУ TICK-BY-TICK (BTC)")
print("═"*50)
print(f" Загальна кількість угод (тіків): {len(df):,}")
print(f" Початок запису (Київ):          {df['datetime'].min()}")
print(f" Кінець запису (Київ):           {df['datetime'].max()}")

# Рахуємо загальний об'єм в USD (ціна * кількість)
df['volume_usd'] = df['price'] * df['amount']
total_vol = df['volume_usd'].sum()
print(f" Загальний проторгований об'єм:  ${total_vol/1_000_000:.2f}M")

# Справжній поділ на ринкові покупки та продажі
# is_buyer_maker == False означає, що ринковий ордер був BUY (Taker)
df['is_buy'] = df['is_buyer_maker'] == False
buys = df[df['is_buy']]['volume_usd'].sum()
sells = df[~df['is_buy']]['volume_usd'].sum()
net_delta = buys - sells

print(f" 🟢 Ринкові покупки (Taker Buys):  ${buys/1_000_000:.2f}M")
print(f" 🔴 Ринкові продажі (Taker Sells): ${sells/1_000_000:.2f}M")
print(f" 📊 Кумулятивна дельта сесії:     ${net_delta/1_000_000:+.2f}M")

print("\n" + "🔬 АНАЛІЗ МІКРО-КЛАСТЕРІВ (Агрегація по 30 секунд)")
print(" Шукаємо приховані лімітні стіни на наднизькому таймфреймі...")

# Групуємо дані по 30 секунд
df.set_index('datetime', inplace=True)
group_30s = df.resample('30s')

stats_30s = []
for name, group in group_30s:
    if group.empty: continue
    
    o = group['price'].iloc[0]
    c = group['price'].iloc[-1]
    chg = (c - o) / o
    
    b_vol = group[group['is_buy']]['volume_usd'].sum()
    s_vol = group[~group['is_buy']]['volume_usd'].sum()
    tot_vol = b_vol + s_vol
    delta = b_vol - s_vol
    delta_p = delta / tot_vol if tot_vol > 0 else 0
    
    stats_30s.append({
        'time': name.strftime('%H:%M:%S'),
        'chg_pct': chg * 100,
        'vol_usd': tot_vol,
        'delta_pct': delta_p * 100,
        'trades': len(group)
    })

df_30s = pd.DataFrame(stats_30s)

# Шукаємо жорстке поглинання (величезний об'єм + дельта, але ціна стоїть)
# Сортуємо за об'ємом, де зміна ціни була мінімальною
absorption = df_30s[df_30s['chg_pct'].abs() <= 0.02].sort_values(by='vol_usd', ascending=False).head(3)

print("\n🔥 ТОП-3 моменти можливого лімітного поглинання (за 30 сек):")
for idx, row in absorption.iterrows():
    print(f"⏱ Час: {row['time']} | Об'єм: ${row['vol_usd']/1_000_000:.2f}M | Дельта: {row['delta_pct']:.1f}% | Рух ціни: {row['chg_pct']:+.3f}% | Угод: {int(row['trades'])}")

print("═"*50)
