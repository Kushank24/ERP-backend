from app.db import engine
from sqlalchemy import text

sql = [
    "ALTER TABLE sales_order_items ADD COLUMN IF NOT EXISTS dispatched_qty DOUBLE PRECISION NOT NULL DEFAULT 0;",
    "ALTER TABLE sales_orders ADD COLUMN IF NOT EXISTS payment_received BOOLEAN NOT NULL DEFAULT FALSE;"
]

with engine.connect() as conn:
    for s in sql:
        try:
            conn.execute(text(s))
            print(f"Executed: {s}")
        except Exception as e:
            print(f"Failed: {s} - Error: {e}")
    conn.commit()
    print("SO Migrations applied successfully.")
