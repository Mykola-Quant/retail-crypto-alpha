import pandas as pd, glob

files = sorted(glob.glob("perp_parquet/BTCUSDT-aggTrades-*.parquet"))
df = pd.concat([pd.read_parquet(f) for f in files])
df.to_parquet("perp_2026Q1Q2.parquet", index=False)
print(f"Об'єднано {len(files)} файлів, рядків: {len(df)}")

