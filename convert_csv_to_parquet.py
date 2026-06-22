import pandas as pd
import os
import glob

os.makedirs("spot_parquet", exist_ok=True)

for fpath in sorted(glob.glob("spot_downloads/BTCUSDT-aggTrades-*.csv")):
    out = fpath.replace("spot_downloads/", "spot_parquet/").replace(".csv", ".parquet")
    if os.path.exists(out):
        print(f"Вже є: {out}")
        continue

    # 8 колонок, timestamp у мікросекундах
    df = pd.read_csv(fpath, header=None,
                     names=['trade_id', 'price', 'qty', 'first_id', 'last_id',
                            'timestamp_us', 'is_buyer_maker', 'is_best_match'])

    # Конвертуємо мікросекунди в мілісекунди для одноманітності з perp
    df['timestamp_ms'] = df['timestamp_us'] // 1000

    # Лишаємо лише потрібні колонки
    df = df[['price', 'qty', 'timestamp_ms', 'is_buyer_maker']]
    df.to_parquet(out)
    print(f"OK -> {out}  ({len(df)} рядків)")
