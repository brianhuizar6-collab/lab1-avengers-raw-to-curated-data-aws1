import pandas as pd
import numpy as np

df = pd.read_csv("C:/Users/GENAIMXGDL4USR7/Desktop/lab1-avengers/store_data.csv")

# 1. Normalize country
country_map = {
    "colombia": "Colombia", "col": "Colombia",
    "mx": "Mexico", "mexico": "Mexico", "méxico": "Mexico",
    "peru": "Peru", "perú": "Peru",
    "argentina": "Argentina", "arg": "Argentina",
    "chile": "Chile",
    "espana": "Spain", "españa": "Spain", "esp": "Spain",
}
df["pais_clean"] = df["pais"].str.strip().str.lower().map(country_map)

# 2. Normalize categorical text
df["categoria_clean"] = (
    df["categoria"].str.replace(r"\?+", "", regex=True).str.strip().str.title()
)
df["canal_venta_clean"] = df["canal_venta"].str.strip().str.title()
df["estado_pedido_clean"] = df["estado_pedido"].str.strip().str.title()
df["metodo_pago_clean"] = df["metodo_pago"].str.strip().str.title()

# 3. Clean price -> float, drop sentinel -1
price_stripped = df["precio_unitario"].astype(str).str.replace(r"[^\d.]", "", regex=True)
df["precio_unitario_clean"] = pd.to_numeric(price_stripped, errors="coerce")
df.loc[df["precio_unitario_clean"] <= 0, "precio_unitario_clean"] = np.nan

# 4. Parse multi-format dates
def parse_date(val):
    if pd.isna(val) or val == "0000-00-00":
        return pd.NaT
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%m-%d-%Y",
                "%Y/%m/%d %H:%M", "%y-%m-%d", "%Y-%m-%d"):
        try:
            return pd.to_datetime(val, format=fmt)
        except (ValueError, TypeError):
            continue
    return pd.NaT

# 1. Create the column first
df["fecha_pedido_clean"] = df["fecha_pedido"].apply(parse_date)

# 2. THEN convert it to date-only (this line must come second)
df["fecha_pedido_clean"] = df["fecha_pedido_clean"].dt.date

# 5. Dedupe on id_pedido
df = df.drop_duplicates(subset="id_pedido", keep="first")

# 6. Compute total
df["monto_total"] = df["cantidad"] * df["precio_unitario_clean"]

# 7. Select final curated columns (drop PII)
curated = df[[
    "id_pedido", "fecha_pedido_clean", "id_cliente", "ciudad",
    "pais_clean", "canal_venta_clean", "categoria_clean", "marca",
    "producto", "sku", "cantidad", "precio_unitario_clean",
    "metodo_pago_clean", "estado_pedido_clean", "monto_total"
]]

curated.to_parquet("curated_sample.parquet", index=False)
print(curated.shape)
print(curated.dtypes)