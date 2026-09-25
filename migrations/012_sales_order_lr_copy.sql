-- Third sales-order document type: LR copy (Lorry Receipt — the transporter's
-- proof-of-consignment for the shipment), alongside invoice_document and
-- eway_bill_document (migration 011). Same shape, same rules: JSONB identity
-- metadata only (public_id/resource_type/format), never a URL — see
-- migration 011 and app/cloudinary_service.py for why.
ALTER TABLE sales_orders
    ADD COLUMN IF NOT EXISTS lr_copy_document JSONB;
