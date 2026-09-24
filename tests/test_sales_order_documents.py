"""
Sales-order document attachments (invoice, e-way bill) via Cloudinary.

Two things this design deliberately does NOT do, both worth pinning:

  1. Never stores a URL. Documents upload with type="authenticated" —
     Cloudinary's stronger access tier for these GST/compliance-sensitive
     files — and an authenticated asset has no permanent URL at all. What's
     persisted is public_id/resource_type/format; a signed URL is generated
     fresh on every view request and expires shortly after
     (cloudinary_service.SIGNED_URL_TTL_SECONDS).

  2. Never lets the doc_type path parameter reach raw SQL as free text.
     get_document_url() builds `SELECT {column} ...` from doc_type, which
     would be a real injection risk if doc_type could be arbitrary — it
     can't, because it's typed Literal["invoice", "eway_bill"] and FastAPI
     rejects anything else with a 422 before the handler body ever runs.

Cloudinary itself is never called in these tests — cloudinary.uploader.upload
and cloudinary.utils.private_download_url are both mocked, so these run with
no network access and no real credentials.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.cloudinary_service import get_signed_url, upload_document
from app.main import app


# ---------------------------------------------------------------------------
# cloudinary_service — unit level, real function, mocked SDK boundary
# ---------------------------------------------------------------------------


@patch("app.cloudinary_service.cloudinary.uploader.upload")
@patch("app.cloudinary_service._ensure_configured")
def test_upload_document_requests_authenticated_delivery(mock_configure, mock_upload):
    """The whole point of this design — must never fall back to public upload."""
    mock_upload.return_value = {"public_id": "abc123", "resource_type": "image", "format": "pdf", "bytes": 1000}
    upload_document(b"%PDF-fake", "invoice.pdf", folder="sales-order-documents")
    _, kwargs = mock_upload.call_args
    assert kwargs["type"] == "authenticated"


@patch("app.cloudinary_service.cloudinary.uploader.upload")
@patch("app.cloudinary_service._ensure_configured")
def test_upload_document_uses_auto_resource_type(mock_configure, mock_upload):
    """auto, not hardcoded image — a photographed e-way bill can be a jpg."""
    mock_upload.return_value = {"public_id": "abc123", "resource_type": "image", "format": "pdf", "bytes": 1000}
    upload_document(b"%PDF-fake", "invoice.pdf", folder="sales-order-documents")
    _, kwargs = mock_upload.call_args
    assert kwargs["resource_type"] == "auto"


@patch("app.cloudinary_service.cloudinary.uploader.upload")
@patch("app.cloudinary_service._ensure_configured")
def test_upload_document_returns_identity_not_a_url(mock_configure, mock_upload):
    mock_upload.return_value = {
        "public_id": "sales-order-documents/xyz", "resource_type": "image",
        "format": "pdf", "bytes": 2048, "secure_url": "https://res.cloudinary.com/should-not-be-used",
    }
    result = upload_document(b"%PDF-fake", "invoice.pdf", folder="sales-order-documents")
    assert result["public_id"] == "sales-order-documents/xyz"
    assert result["resource_type"] == "image"
    assert result["format"] == "pdf"
    assert result["original_filename"] == "invoice.pdf"
    assert "url" not in result
    assert "secure_url" not in result


@patch("app.cloudinary_service.cloudinary.uploader.upload")
@patch("app.cloudinary_service._ensure_configured")
def test_upload_failure_propagates_not_swallowed(mock_configure, mock_upload):
    mock_upload.side_effect = RuntimeError("Cloudinary 500")
    with pytest.raises(RuntimeError):
        upload_document(b"data", "x.pdf", folder="f")


def test_ensure_configured_raises_clearly_when_unset(monkeypatch):
    from app.config import settings
    import app.cloudinary_service as svc

    monkeypatch.setattr(settings, "cloudinary_cloud_name", "", raising=False)
    monkeypatch.setattr(svc, "_configured", False)
    with pytest.raises(RuntimeError, match="not configured"):
        svc._ensure_configured()


@patch("app.cloudinary_service.cloudinary.utils.private_download_url")
@patch("app.cloudinary_service._ensure_configured")
def test_get_signed_url_uses_authenticated_type(mock_configure, mock_private_url):
    mock_private_url.return_value = "https://signed.example/x"
    doc = {"public_id": "abc", "resource_type": "image", "format": "pdf"}
    get_signed_url(doc)
    args, kwargs = mock_private_url.call_args
    assert kwargs["type"] == "authenticated"


@patch("app.cloudinary_service.cloudinary.utils.private_download_url")
@patch("app.cloudinary_service._ensure_configured")
def test_get_signed_url_sets_an_expiry_in_the_future(mock_configure, mock_private_url):
    import time
    mock_private_url.return_value = "https://signed.example/x"
    doc = {"public_id": "abc", "resource_type": "image", "format": "pdf"}
    before = int(time.time())
    get_signed_url(doc, expires_in_seconds=300)
    _, kwargs = mock_private_url.call_args
    assert kwargs["expires_at"] >= before + 300


@patch("app.cloudinary_service.cloudinary.utils.private_download_url")
@patch("app.cloudinary_service._ensure_configured")
def test_get_signed_url_passes_through_public_id_and_format(mock_configure, mock_private_url):
    mock_private_url.return_value = "https://signed.example/x"
    doc = {"public_id": "sales-order-documents/xyz", "resource_type": "raw", "format": "pdf"}
    get_signed_url(doc)
    args, kwargs = mock_private_url.call_args
    assert args[0] == "sales-order-documents/xyz"
    assert args[1] == "pdf"
    assert kwargs["resource_type"] == "raw"


# ---------------------------------------------------------------------------
# Router: doc_type is a real, enforced constraint — not just a convention
# ---------------------------------------------------------------------------


def _client_with_auth(monkeypatch):
    """Bypass real auth for these router-level tests."""
    from app import deps

    async def fake_user():
        return {"id": "u1", "username": "test", "role": "admin", "allowed_modules": ["sales_orders"]}

    app.dependency_overrides[deps.get_current_user] = fake_user
    return TestClient(app)


def test_upload_document_rejects_unknown_doc_type(monkeypatch):
    client = _client_with_auth(monkeypatch)
    try:
        resp = client.post(
            "/api/v1/sales-orders/upload-document",
            params={"doc_type": "not_a_real_type"},
            files={"file": ("x.pdf", b"%PDF-fake", "application/pdf")},
        )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_get_document_url_rejects_unknown_doc_type(monkeypatch):
    client = _client_with_auth(monkeypatch)
    try:
        resp = client.get("/api/v1/sales-orders/1/documents/not_a_real_type/url")
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


@patch("app.routers.sales_orders.cloudinary_service.upload_document")
def test_upload_document_enforces_the_shared_size_limit(mock_upload, monkeypatch):
    """Reuses app.upload_limits.read_limited — same guard as the other 3 upload endpoints."""
    from app.upload_limits import MAX_UPLOAD_BYTES

    client = _client_with_auth(monkeypatch)
    try:
        oversized = b"x" * (MAX_UPLOAD_BYTES + 1)
        resp = client.post(
            "/api/v1/sales-orders/upload-document",
            params={"doc_type": "invoice"},
            files={"file": ("big.pdf", oversized, "application/pdf")},
        )
        assert resp.status_code == 413
        mock_upload.assert_not_called()
    finally:
        app.dependency_overrides.clear()


@patch("app.routers.sales_orders.cloudinary_service.upload_document")
def test_upload_document_returns_doc_type_and_metadata(mock_upload, monkeypatch):
    mock_upload.return_value = {
        "public_id": "sales-order-documents/abc", "resource_type": "image",
        "format": "pdf", "original_filename": "invoice.pdf", "bytes": 500,
    }
    client = _client_with_auth(monkeypatch)
    try:
        resp = client.post(
            "/api/v1/sales-orders/upload-document",
            params={"doc_type": "eway_bill"},
            files={"file": ("eway.pdf", b"%PDF-fake", "application/pdf")},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["doc_type"] == "eway_bill"
        assert body["document"]["public_id"] == "sales-order-documents/abc"
    finally:
        app.dependency_overrides.clear()


@patch("app.routers.sales_orders.cloudinary_service.upload_document")
def test_upload_document_translates_cloudinary_failure_to_502(mock_upload, monkeypatch):
    """A genuine Cloudinary API failure, distinct from a config problem."""
    mock_upload.side_effect = ConnectionError("Cloudinary API unreachable")
    client = _client_with_auth(monkeypatch)
    try:
        resp = client.post(
            "/api/v1/sales-orders/upload-document",
            params={"doc_type": "invoice"},
            files={"file": ("x.pdf", b"data", "application/pdf")},
        )
        assert resp.status_code == 502
    finally:
        app.dependency_overrides.clear()


@patch("app.routers.sales_orders.cloudinary_service.upload_document")
def test_upload_document_reports_503_when_not_configured(mock_upload, monkeypatch):
    """
    RuntimeError is reserved for the "Cloudinary env vars missing" case
    (cloudinary_service._ensure_configured) — must map to 503, not 502,
    since it's a deployment problem, not an upload-time failure.
    """
    mock_upload.side_effect = RuntimeError("Cloudinary is not configured")
    client = _client_with_auth(monkeypatch)
    try:
        resp = client.post(
            "/api/v1/sales-orders/upload-document",
            params={"doc_type": "invoice"},
            files={"file": ("x.pdf", b"data", "application/pdf")},
        )
        assert resp.status_code == 503
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# get_document_url: 404 handling, no leaking a stale/cached URL
# ---------------------------------------------------------------------------


@patch("app.routers.sales_orders.cloudinary_service.get_signed_url")
def test_get_document_url_404s_when_order_not_found(mock_signed, monkeypatch):
    from app import db as db_module

    def fake_get_db():
        session = MagicMock()
        session.execute.return_value.mappings.return_value.first.return_value = None
        yield session

    app.dependency_overrides[db_module.get_db] = fake_get_db
    client = _client_with_auth(monkeypatch)
    try:
        resp = client.get("/api/v1/sales-orders/999/documents/invoice/url")
        assert resp.status_code == 404
        mock_signed.assert_not_called()
    finally:
        app.dependency_overrides.clear()


@patch("app.routers.sales_orders.cloudinary_service.get_signed_url")
def test_get_document_url_404s_when_no_document_attached(mock_signed, monkeypatch):
    from app import db as db_module

    def fake_get_db():
        session = MagicMock()
        session.execute.return_value.mappings.return_value.first.return_value = {"doc": None}
        yield session

    app.dependency_overrides[db_module.get_db] = fake_get_db
    client = _client_with_auth(monkeypatch)
    try:
        resp = client.get("/api/v1/sales-orders/1/documents/invoice/url")
        assert resp.status_code == 404
        mock_signed.assert_not_called()
    finally:
        app.dependency_overrides.clear()


@patch("app.routers.sales_orders.cloudinary_service.get_signed_url")
def test_get_document_url_returns_a_fresh_url_each_call(mock_signed, monkeypatch):
    from app import db as db_module

    doc = {"public_id": "sales-order-documents/abc", "resource_type": "image", "format": "pdf"}

    def fake_get_db():
        session = MagicMock()
        session.execute.return_value.mappings.return_value.first.return_value = {"doc": doc}
        yield session

    app.dependency_overrides[db_module.get_db] = fake_get_db
    mock_signed.side_effect = ["https://signed.example/1", "https://signed.example/2"]
    client = _client_with_auth(monkeypatch)
    try:
        first = client.get("/api/v1/sales-orders/1/documents/invoice/url").json()["url"]
        second = client.get("/api/v1/sales-orders/1/documents/invoice/url").json()["url"]
        assert first != second
        assert mock_signed.call_count == 2
    finally:
        app.dependency_overrides.clear()
