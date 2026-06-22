import pandas as pd
import zipfile
import os
import glob

os.makedirs("spot_parquet", exist_ok=True)

for fpath in sorted(glob.glob("spot_downloads/*")):
    # Пропускаємо не-ZIP та контрольні суми
    if not fpath.endswith(".zip") or not zipfile.is_zipfile(fpath):
        print(f"Пропущено (не ZIP): {fpath}")
        continue

    out = fpath.replace("spot_downloads/", "spot_parquet/").replace(".zip", ".parquet")
    if os.path.exists(out):
        print(f"Вже є: {out}")
        continue

    try:
        with zipfile.ZipFile(fpath) as z:
            csv_name = z.namelist()[0]
            with z.open(csv_name) as f:
                df = pd.read_csv(f, header=None,
                                 names=['trade_id', 'price', 'qty', 'quote_qty',
                                        'timestamp_ms', 'is_buyer_maker'])
        df.to_parquet(out)
        print(f"OK -> {out}")
    except Exception as e:
        print(f"Помилка з {fpath}: {e}")
