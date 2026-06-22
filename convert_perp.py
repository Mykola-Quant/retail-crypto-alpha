import pandas as pd, os, glob

os.makedirs("perp_parquet", exist_ok=True)

col_names = ['trade_id', 'price', 'qty', 'first_id', 'last_id',
             'timestamp_us', 'is_buyer_maker', 'is_best_match']

for fpath in sorted(glob.glob("perp_downloads/BTCUSDT-aggTrades-*.csv")):
    out = fpath.replace("perp_downloads/", "perp_parquet/").replace(".csv", ".parquet")
    if os.path.exists(out):
        print(f"Вже є: {out}")
        continue
    
    # Читаємо CSV, пропускаючи перший рядок із заголовками
    df = pd.read_csv(fpath, header=None, names=col_names, skiprows=1)
    
    # Перетворюємо типи примусово
    df['trade_id'] = pd.to_numeric(df['trade_id'], errors='coerce').astype('int64')
    df['price'] = pd.to_numeric(df['price'], errors='coerce')
    df['qty'] = pd.to_numeric(df['qty'], errors='coerce')
    df['first_id'] = pd.to_numeric(df['first_id'], errors='coerce').astype('int64')
    df['last_id'] = pd.to_numeric(df['last_id'], errors='coerce').astype('int64')
    df['timestamp_us'] = pd.to_numeric(df['timestamp_us'], errors='coerce').astype('int64')
    df['is_buyer_maker'] = df['is_buyer_maker'].apply(lambda x: True if x == 'True' else False)
    
    # Переводимо мікросекунди в мілісекунди
    df['timestamp_ms'] = df['timestamp_us'] // 1000
    
    # Залишаємо тільки необхідні колонки
    df = df[['price', 'qty', 'timestamp_ms', 'is_buyer_maker']]
    
    # Прибираємо рядки з некоректними даними (наприклад, якщо щось не розпарсилось)
    df = df.dropna()
    
    df.to_parquet(out)
    print(f"OK -> {out}  ({len(df)} рядків)")
