from app.db import engine
from sqlalchemy import text

indexes_sql = [
    "CREATE INDEX IF NOT EXISTS idx_materials_length ON materials (length_weight_nos);",
    "CREATE INDEX IF NOT EXISTS idx_po_created_at ON purchase_orders (created_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_po_supplier_id ON purchase_orders (supplier_id);",
    "CREATE INDEX IF NOT EXISTS idx_fg_dates ON finished_goods (completion_date DESC, created_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_fg_qty ON finished_goods (quantity_in_stock);",
    "CREATE INDEX IF NOT EXISTS idx_wo_status ON work_orders (status);"
]

with engine.connect() as conn:
    for sql in indexes_sql:
        try:
            conn.execute(text(sql))
            print(f"Executed: {sql}")
        except Exception as e:
            print(f"Failed: {sql} - Error: {e}")
    conn.commit()
    print("Indexes applied successfully.")
