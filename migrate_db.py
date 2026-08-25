from app.db import engine
from sqlalchemy import text

with engine.connect() as conn:
    conn.execute(text("ALTER TABLE purchase_order_lines ADD COLUMN IF NOT EXISTS delivered_qty DOUBLE PRECISION NOT NULL DEFAULT 0;"))
    conn.commit()
    print("Migration successful")
