# merge_spot.py
import pandas as pd, glob

files = sorted(glob.glob("spot_parquet/BTCUSDT-aggTrades-*.parquet"))
df = pd.concat([pd.read_parquet(f) for f in files])
df.to_parquet("spot_2026Q1Q2.parquet", index=False)
print(f"Об'єднано {len(files)} файлів, рядків: {len(df)}")
