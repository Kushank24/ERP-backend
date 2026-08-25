"""PDF generation service — ported from Bill-of-Quantity/src/infrastructure/pdf/pdf_service.py."""
from __future__ import annotations

import io
import logging
import os
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image

logger = logging.getLogger(__name__)

# Absolute path to the logo sitting next to this backend directory.
_LOGO_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "esafe.png")


class PDFGenerationService:
    def __init__(self, logo_path: str = _LOGO_PATH):
        self.logo_path = logo_path
        self.styles = getSampleStyleSheet()
        self._setup_custom_styles()

    def _setup_custom_styles(self) -> None:
        self.title_style = ParagraphStyle(
            name="TitleStyle",
            fontSize=16,
            alignment=1,
            fontName="Helvetica-Bold",
            textColor=colors.black,
            spaceAfter=4,
        )
        self.header_style = ParagraphStyle(
            name="HeaderStyle",
            fontSize=9,
            fontName="Helvetica-Bold",
            textColor=colors.black,
        )
        self.normal_style = ParagraphStyle(
            name="NormalStyle",
            fontSize=9,
            fontName="Helvetica",
            textColor=colors.black,
        )

    # ------------------------------------------------------------------
    # Public: Purchase Order PDF
    # ------------------------------------------------------------------

    def generate_purchase_order_pdf(self, data: dict[str, Any]) -> io.BytesIO:
        buffer = io.BytesIO()
        pdf = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            leftMargin=24,
            rightMargin=24,
            topMargin=24,
            bottomMargin=24,
        )
        elements: list = []
        self._add_combined_header(elements, data)
        title = Paragraph("<b>PURCHASE ORDER</b>", self.title_style)
        title.hAlign = "CENTER"
        elements.append(title)
        elements.append(Spacer(1, 8))
        self._add_vendor_details(elements, data)
        self._add_materials_table(elements, data)
        self._add_comments_section(elements, data)
        self._add_signature_line(elements)
        pdf.build(elements)
        buffer.seek(0)
        logger.info("Generated PDF for PO: %s", data.get("PurchaseNumber"))
        return buffer

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fmt_date(self, raw: Any) -> str:
        if not raw:
            return ""
        if hasattr(raw, "strftime"):
            return raw.strftime("%B %d, %Y")
        if isinstance(raw, str) and raw:
            try:
                from datetime import datetime
                return datetime.strptime(raw, "%Y-%m-%d").strftime("%B %d, %Y")
            except ValueError:
                pass
        return str(raw)

    def _add_combined_header(self, elements: list, data: dict[str, Any]) -> None:
        # Resolve logo path at render time; fall back gracefully if missing.
        resolved_logo = os.path.realpath(self.logo_path)
        if os.path.isfile(resolved_logo):
            logo_cell: Any = Image(resolved_logo, width=100, height=50)
        else:
            logo_cell = Paragraph("<b>E-SAFE</b>", self.title_style)

        po_details_data = [
            [Paragraph("DATE", self.header_style), data.get("PurchaseDate", "")],
            [Paragraph("PO #", self.header_style), data.get("PurchaseNumber", "")],
            [Paragraph("EXPECTED DELIVERY DATE", self.header_style),
             self._fmt_date(data.get("PurchaseOrderDeliveryDate")) or "Not specified"],
        ]
        actual = self._fmt_date(data.get("ActualDeliveryDate"))
        if actual:
            po_details_data.append([Paragraph("ACTUAL DELIVERY DATE", self.header_style), actual])
        bills = data.get("BillNumbers", "")
        if bills and bills.strip():
            bill_list = [b.strip() for b in bills.split(",") if b.strip()]
            if bill_list:
                po_details_data.append([Paragraph("BILL NUMBERS", self.header_style), ", ".join(bill_list)])

        po_details_table = Table(po_details_data, colWidths=[150, 230])
        po_details_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.white),
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.black),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
        ]))

        header_table = Table([[logo_cell, po_details_table]], colWidths=[140, 380])
        header_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (0, 0), (0, 0), "LEFT"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ]))
        elements.append(header_table)
        elements.append(Spacer(1, 10))

    def _add_vendor_details(self, elements: list, data: dict[str, Any]) -> None:
        vendor_header_style = ParagraphStyle(
            name="VendorHeaderStyle",
            fontSize=10,
            fontName="Helvetica-Bold",
            textColor=colors.black,
            alignment=1,
        )
        vendor_data = [
            [Paragraph("<b>VENDOR</b>", vendor_header_style)],
            [Paragraph(data.get("Vendor Name", ""), self.normal_style)],
            [Paragraph(data.get("Vendor Address", ""), self.normal_style)],
            [Paragraph(f"Phone: {data.get('Vendor Phone', '')}", self.normal_style)],
            [Paragraph(f"Vendor GSTIN Number: {data.get('Vendor GSTIN Number', 'N/A')}", self.normal_style)],
        ]
        vendor_table = Table(vendor_data, colWidths=[250])
        vendor_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("BACKGROUND", (0, 0), (0, 0), colors.white),
            ("TEXTCOLOR", (0, 0), (0, 0), colors.black),
            ("FONTNAME", (0, 0), (0, 0), "Helvetica-Bold"),
            ("BACKGROUND", (0, 1), (0, -1), colors.white),
            ("TEXTCOLOR", (0, 1), (0, -1), colors.black),
        ]))

        ship_to_data = [
            [Paragraph("<b>SHIP TO</b>", vendor_header_style)],
            [Paragraph("E-SAFE Enterprises, 08AACFE4028Q1Z5", self.normal_style)],
            [Paragraph("+91-9773313466, accounts@esafe.co.in", self.normal_style)],
            [Paragraph("G-176, Boranada Industrial Area", self.normal_style)],
            [Paragraph("Jodhpur, Rajasthan, India, 342012", self.normal_style)],
        ]
        ship_to_table = Table(ship_to_data, colWidths=[250])
        ship_to_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("BACKGROUND", (0, 0), (0, 0), colors.white),
            ("TEXTCOLOR", (0, 0), (0, 0), colors.black),
            ("FONTNAME", (0, 0), (0, 0), "Helvetica-Bold"),
            ("BACKGROUND", (0, 1), (0, -1), colors.white),
            ("TEXTCOLOR", (0, 1), (0, -1), colors.black),
        ]))

        combined = Table([[vendor_table, ship_to_table]], colWidths=[250, 250])
        combined.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
        elements.append(combined)
        elements.append(Spacer(1, 8))

    def _add_materials_table(self, elements: list, data: dict[str, Any]) -> None:
        materials = data.get("Materials", [])
        if not materials:
            elements.append(Paragraph("No materials specified", self.normal_style))
            return

        elements.append(Paragraph("<b>Materials</b>", self.header_style))
        elements.append(Spacer(1, 6))

        rows = [
            [
                Paragraph("<b>ITEM #</b>", self.header_style),
                Paragraph("<b>DESCRIPTION</b>", self.header_style),
                Paragraph("<b>LENGTH/WEIGHT/NOS</b>", self.header_style),
                Paragraph("<b>UNIT</b>", self.header_style),
                Paragraph("<b>UNIT PRICE</b>", self.header_style),
                Paragraph("<b>TOTAL</b>", self.header_style),
            ]
        ]

        for i, mat in enumerate(materials, start=1):
            qty = float(mat.get("length_weight_nos", 0))
            cost = float(mat.get("per_unit_cost", 0))
            comment = mat.get("comment", "")
            name = mat.get("name", "")
            desc_text = (
                f"{name}<br/><font size=8 color='grey'><i>Note: {comment}</i></font>"
                if comment
                else name
            )
            rows.append([
                str(i),
                Paragraph(desc_text, self.normal_style),
                str(qty) if qty > 0 else "N/A",
                mat.get("unit", "Nos"),
                f"Rs.{cost:.2f}",
                f"Rs.{qty * cost:.2f}",
            ])

        subtotal = sum(
            float(m.get("length_weight_nos", 0)) * float(m.get("per_unit_cost", 0))
            for m in materials
        )
        gst_pct = float(data.get("PurchaseGST", 18))
        if gst_pct > 1:
            gst_pct /= 100
        gst_amt = subtotal * gst_pct
        total = subtotal + gst_amt

        n = len(materials)
        subtotal_row = n + 1
        gst_row = n + 2
        total_row = n + 3

        rows.append(["SUBTOTAL", "", "", "", "", f"Rs.{subtotal:.2f}"])
        rows.append([f"GST ({int(gst_pct * 100)}%)", "", "", "", "", f"Rs.{gst_amt:.2f}"])
        rows.append(["TOTAL", "", "", "", "", f"Rs.{total:.2f}"])

        tbl = Table(rows, colWidths=[40, 200, 80, 60, 70, 70])
        tbl.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.white),
            ("ALIGN", (0, 0), (-1, 0), "CENTER"),
            ("BACKGROUND", (0, 1), (-1, n), colors.white),
            ("ALIGN", (0, 1), (0, n), "CENTER"),
            ("ALIGN", (1, 1), (1, n), "LEFT"),
            ("ALIGN", (2, 1), (-1, n), "CENTER"),
            ("BACKGROUND", (0, subtotal_row), (-1, total_row), colors.white),
            ("FONTNAME", (0, subtotal_row), (-1, total_row), "Helvetica-Bold"),
            ("SPAN", (0, subtotal_row), (4, subtotal_row)),
            ("SPAN", (0, gst_row), (4, gst_row)),
            ("SPAN", (0, total_row), (4, total_row)),
            ("ALIGN", (0, subtotal_row), (4, total_row), "CENTER"),
            ("ALIGN", (5, subtotal_row), (5, total_row), "CENTER"),
            ("BACKGROUND", (0, total_row), (-1, total_row), colors.lightgrey),
            ("LINEABOVE", (0, total_row), (-1, total_row), 2, colors.black),
        ]))
        elements.append(tbl)
        elements.append(Spacer(1, 8))

    def _add_comments_section(self, elements: list, data: dict[str, Any]) -> None:
        comments = data.get("Comments", "Thank you for your business.")
        delivery_text = self._format_delivery_details(data.get("DeliveryDetails", {}))
        full = f"{comments}\n\n{delivery_text}" if delivery_text else comments

        comments_table = Table(
            [[Paragraph("<b>Comments or Special Instructions</b>", self.header_style)],
             [Paragraph(full, self.normal_style)]],
            colWidths=[520],
        )
        comments_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), colors.white),
            ("TEXTCOLOR", (0, 0), (0, 0), colors.black),
            ("FONTNAME", (0, 0), (0, 0), "Helvetica-Bold"),
            ("BACKGROUND", (0, 1), (0, 1), colors.white),
            ("TEXTCOLOR", (0, 1), (0, 1), colors.black),
            ("GRID", (0, 0), (0, 1), 1, colors.black),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        elements.append(comments_table)
        elements.append(Spacer(1, 8))

    def _format_delivery_details(self, details: Any) -> str:
        if not details or not isinstance(details, dict):
            return ""
        labels = {
            "payment": "Payment Terms",
            "transportation": "Transportation",
            "terms": "Delivery Terms",
            "address": "Delivery Address",
            "other": "Other Instructions",
        }
        parts = [
            f"• {labels.get(k, k.title())}: {v}"
            for k, v in details.items()
            if v and str(v).strip()
        ]
        if parts:
            return "<b>Delivery &amp; Payment Details:</b><br/>" + "<br/>".join(parts)
        return ""

    def _add_signature_line(self, elements: list) -> None:
        sig_table = Table(
            [["Signature:_______________________________________", "Date:_______________________"]],
            colWidths=[350, 150],
        )
        sig_table.setStyle(TableStyle([
            ("ALIGN", (0, 0), (0, 0), "LEFT"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ]))
        elements.append(sig_table)

    # ------------------------------------------------------------------
    # Public: Work Order PDF
    # ------------------------------------------------------------------

    def generate_work_order_pdf(self, data: dict[str, Any]) -> io.BytesIO:
        buffer = io.BytesIO()
        pdf = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            leftMargin=36,
            rightMargin=36,
            topMargin=36,
            bottomMargin=36,
        )
        elements: list = []
        self._add_wo_logo(elements)
        title = Paragraph("<b>WORK ORDER</b>", self.title_style)
        title.hAlign = "CENTER"
        elements.append(title)
        elements.append(Spacer(1, 12))
        self._add_wo_header(elements, data)
        self._add_wo_products_table(elements, data)
        self._add_signature_line(elements)
        pdf.build(elements)
        buffer.seek(0)
        logger.info("Generated PDF for WO: %s", data.get("work_order_number"))
        return buffer

    def _add_wo_logo(self, elements: list) -> None:
        resolved = os.path.realpath(self.logo_path)
        if os.path.isfile(resolved):
            logo = Image(resolved, width=100, height=50)
            logo.hAlign = "LEFT"
            elements.append(logo)
            elements.append(Spacer(1, 12))

    def _add_wo_header(self, elements: list, data: dict[str, Any]) -> None:
        rows: list = [
            [Paragraph("PO NUMBER", self.header_style), str(data.get("po_number", "") or "")],
            [Paragraph("PO DATE", self.header_style), str(data.get("po_date", "") or "")],
            [Paragraph("PARTY NAME", self.header_style), str(data.get("party_name", "") or "")],
        ]
        delivery = self._fmt_date(data.get("delivery_date"))
        if delivery:
            rows.append([Paragraph("DELIVERY DATE", self.header_style), delivery])

        tbl = Table(rows, colWidths=[120, 200])
        tbl.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ("BACKGROUND", (0, 0), (0, -1), colors.lightgrey),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ]))
        elements.append(tbl)
        elements.append(Spacer(1, 24))

    def _add_wo_products_table(self, elements: list, data: dict[str, Any]) -> None:
        products = data.get("products", [])
        if not products:
            elements.append(Paragraph("No products specified", self.normal_style))
            return

        elements.append(Paragraph("<b>Products</b>", self.header_style))
        elements.append(Spacer(1, 12))

        prod_rows = [["PRODUCT NAME", "QUANTITY", "PRODUCT CODE"]]
        for p in products:
            prod_rows.append([
                p.get("name", ""),
                str(p.get("quantity", "")),
                p.get("product_code") or "N/A",
            ])

        prod_tbl = Table(prod_rows, colWidths=[200, 80, 240])
        prod_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 10),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 12),
            ("BACKGROUND", (0, 1), (-1, -1), colors.beige),
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ("ALIGN", (1, 1), (1, -1), "RIGHT"),
        ]))
        elements.append(prod_tbl)
        elements.append(Spacer(1, 20))

        # Material requirements
        materials = data.get("materials", {})
        if not materials:
            return

        elements.append(Paragraph("<b>Material Requirements</b>", self.header_style))
        elements.append(Spacer(1, 12))

        mat_rows = [["MATERIAL", "UNIT", "QTY PER UNIT", "TOTAL REQUIRED"]]
        for mat_key, info in materials.items():
            mat_rows.append([
                mat_key,
                info.get("unit", "Nos"),
                str(info.get("quantity_per_unit", 0)),
                str(info.get("total_required", 0)),
            ])

        mat_tbl = Table(mat_rows, colWidths=[150, 80, 120, 120])
        mat_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 10),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 12),
            ("BACKGROUND", (0, 1), (-1, -1), colors.beige),
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ]))
        elements.append(mat_tbl)
        elements.append(Spacer(1, 12))
