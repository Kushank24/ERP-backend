-- Sales invoice and e-way bill attachments for sales orders, uploaded to
-- Cloudinary under authenticated (signed-URL-only) delivery.
--
-- Deliberately NOT storing a URL: an authenticated Cloudinary asset has no
-- permanent one — every view requires a freshly-signed, time-limited URL
-- generated on demand (app/cloudinary_service.py:get_signed_url). What's
-- stored here is just enough identity to regenerate that URL later:
-- public_id, resource_type, format, and the original filename for display.
--
-- Shape written by app/cloudinary_service.py:upload_document():
--   {"public_id": "...", "resource_type": "image"|"raw", "format": "pdf",
--    "original_filename": "invoice.pdf", "bytes": 123456}
--
-- NULL means no document has been attached yet — both are optional at
-- order creation and can be added later.
ALTER TABLE sales_orders
    ADD COLUMN IF NOT EXISTS invoice_document  JSONB,
    ADD COLUMN IF NOT EXISTS eway_bill_document JSONB;
