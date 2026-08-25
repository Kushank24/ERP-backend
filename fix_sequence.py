from app.db import engine
from sqlalchemy import text

with engine.connect() as conn:
    tables = [
        "materials", "products", "suppliers", "work_orders",
        "finished_goods", "sales_orders", "purchase_orders", 
        "purchase_order_lines", "work_order_products"
    ]
    for table in tables:
        try:
            conn.execute(text(f"SELECT setval('{table}_id_seq', COALESCE((SELECT MAX(id)+1 FROM {table}), 1), false);"))
        except Exception as e:
            print(f"Skipped {table}: {e}")
    conn.commit()
    print("Sequences fixed")
