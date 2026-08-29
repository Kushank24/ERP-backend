"""PDF generation service — ported from Bill-of-Quantity/src/infrastructure/pdf/pdf_service.py."""
from __future__ import annotations

import io
import logging
import os
from xml.sax.saxutils import escape
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image

logger = logging.getLogger(__name__)

_LOGO_PATH = os.path.join(os.path.dirname(__file__), "static", "esafe.png")


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
        shop_floor = data.get("Variant") == "shop-floor"
        elements: list = []
        self._add_combined_header(elements, data)
        title_text = "<b>SHOP FLOOR PURCHASE ORDER</b>" if shop_floor else "<b>PURCHASE ORDER</b>"
        title = Paragraph(title_text, self.title_style)
        title.hAlign = "CENTER"
        elements.append(title)
        elements.append(Spacer(1, 8))
        self._add_vendor_details(elements, data)
        self._add_materials_table(elements, data, shop_floor=shop_floor)
        if not shop_floor:
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

    def _add_materials_table(self, elements: list, data: dict[str, Any], shop_floor: bool = False) -> None:
        materials = data.get("Materials", [])
        if not materials:
            elements.append(Paragraph("No materials specified", self.normal_style))
            return

        elements.append(Paragraph("<b>Materials</b>", self.header_style))
        elements.append(Spacer(1, 6))

        if shop_floor:
            rows = [[
                Paragraph("<b>ITEM #</b>", self.header_style),
                Paragraph("<b>DESCRIPTION</b>", self.header_style),
                Paragraph("<b>LENGTH/WEIGHT/NOS</b>", self.header_style),
                Paragraph("<b>UNIT</b>", self.header_style),
            ]]
            for i, mat in enumerate(materials, start=1):
                qty = float(mat.get("length_weight_nos", 0))
                comment = mat.get("comment", "")
                name = mat.get("name", "")
                desc_text = (
                    f"{name}<br/><font size=8 color='grey'><i>Note: {comment}</i></font>"
                    if comment else name
                )
                rows.append([
                    str(i),
                    Paragraph(desc_text, self.normal_style),
                    str(qty) if qty > 0 else "N/A",
                    mat.get("unit", "Nos"),
                ])
            n = len(materials)
            tbl = Table(rows, colWidths=[40, 290, 130, 60])
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
            ]))
            elements.append(tbl)
            elements.append(Spacer(1, 8))
            return

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
        extra_costs = data.get("AdditionalCosts") or []
        extra_total = sum(float(ec.get("amount", 0)) for ec in extra_costs)

        gst_pct = float(data.get("PurchaseGST", 18))
        if gst_pct > 1:
            gst_pct /= 100
        gst_base = subtotal + extra_total
        gst_amt = gst_base * gst_pct
        total = gst_base + gst_amt

        n = len(materials)
        subtotal_row = n + 1
        extra_row_start = n + 2
        gst_row = n + 2 + len(extra_costs)
        total_row = n + 3 + len(extra_costs)

        rows.append(["SUBTOTAL", "", "", "", "", f"Rs.{subtotal:.2f}"])
        for ec in extra_costs:
            rows.append([ec.get("label", "Extra Cost"), "", "", "", "", f"Rs.{float(ec.get('amount', 0)):.2f}"])
        rows.append([f"GST ({int(gst_pct * 100)}%)", "", "", "", "", f"Rs.{gst_amt:.2f}"])
        rows.append(["TOTAL", "", "", "", "", f"Rs.{total:.2f}"])

        tbl = Table(rows, colWidths=[40, 200, 80, 60, 70, 70])
        styles = [
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
        ]
        for i in range(len(extra_costs)):
            styles.append(("SPAN", (0, extra_row_start + i), (4, extra_row_start + i)))
        tbl.setStyle(TableStyle(styles))
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
        self._add_wo_header(elements, data)
        self._add_wo_products_table(elements, data)
        self._add_signature_line(elements)
        pdf.build(elements)
        buffer.seek(0)
        logger.info("Generated PDF for WO: %s", data.get("work_order_number"))
        return buffer

    def _add_wo_header(self, elements: list, data: dict[str, Any]) -> None:
        from reportlab.lib.enums import TA_CENTER
        resolved_logo = os.path.realpath(self.logo_path)
        if os.path.isfile(resolved_logo):
            logo_cell: Any = Image(resolved_logo, width=100, height=50)
        else:
            logo_cell = Paragraph("<b>E-SAFE</b>", self.title_style)

        title_style = ParagraphStyle(
            "WOTitle", fontSize=14, fontName="Helvetica-Bold", alignment=TA_CENTER, spaceAfter=6
        )

        details_rows: list = [
            [Paragraph("<b>WORK ORDER</b>", title_style), ""],
            [Paragraph("WO NUMBER", self.header_style), str(data.get("work_order_number", "") or "")],
            [Paragraph("PO NUMBER", self.header_style), str(data.get("po_number", "") or "")],
            [Paragraph("PO DATE", self.header_style), str(data.get("po_date", "") or "")],
            [Paragraph("PARTY NAME", self.header_style), str(data.get("party_name", "") or "")],
        ]
        delivery = self._fmt_date(data.get("delivery_date"))
        if delivery:
            details_rows.append([Paragraph("DELIVERY DATE", self.header_style), delivery])

        details_tbl = Table(details_rows, colWidths=[130, 270])
        details_tbl.setStyle(TableStyle([
            ("SPAN", (0, 0), (1, 0)),
            ("ALIGN", (0, 0), (1, 0), "CENTER"),
            ("FONTNAME", (0, 0), (1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (1, 0), 12),
            ("TOPPADDING", (0, 0), (1, 0), 6),
            ("BOTTOMPADDING", (0, 0), (1, 0), 6),
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ("BACKGROUND", (0, 1), (0, -1), colors.lightgrey),
            ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("FONTSIZE", (0, 1), (-1, -1), 9),
        ]))

        header_table = Table([[logo_cell, details_tbl]], colWidths=[140, 400])
        header_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (0, 0), (0, 0), "LEFT"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ]))
        elements.append(header_table)
        elements.append(Spacer(1, 16))

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
        materials = data.get("materials", [])
        if not materials:
            return

        elements.append(Paragraph("<b>Material Requirements</b>", self.header_style))
        elements.append(Spacer(1, 12))

        mat_rows = [["MATERIAL", "SECTION SIZE", "UNIT", "QTY PER UNIT", "TOTAL REQUIRED"]]
        for mat in materials:
            ss = mat.get("section_size", 0) or 0
            ss_display = str(int(ss)) if ss > 0 and ss == int(ss) else (str(ss) if ss > 0 else "-")
            mat_rows.append([
                Paragraph(mat.get("name", ""), self.normal_style),
                ss_display,
                mat.get("unit", "Nos"),
                str(mat.get("quantity_per_unit", 0)),
                str(mat.get("total_required", 0)),
            ])

        mat_tbl = Table(mat_rows, colWidths=[175, 85, 55, 110, 110])
        mat_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 10),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 12),
            ("BACKGROUND", (0, 1), (-1, -1), colors.beige),
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
        ]))
        elements.append(mat_tbl)
        elements.append(Spacer(1, 12))

    # ------------------------------------------------------------------
    # Public: Offer / Quotation PDF  (E-Safe format)
    # ------------------------------------------------------------------

    def generate_offer_pdf(self, data: dict[str, Any]) -> io.BytesIO:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
        from datetime import date as _date

        buffer = io.BytesIO()
        pdf = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=0.5 * inch,
            rightMargin=0.5 * inch,
            topMargin=0.5 * inch,
            bottomMargin=0.5 * inch,
        )
        elements: list = []

        # ── styles ────────────────────────────────────────────────────
        normal_st = ParagraphStyle("OfNormal", fontSize=11, fontName="Helvetica",
                                   spaceAfter=1, alignment=TA_JUSTIFY)
        no_space_st = ParagraphStyle("OfNoSpace", fontSize=11, fontName="Helvetica",
                                     spaceAfter=0, alignment=TA_JUSTIFY)
        justify_st = ParagraphStyle("OfJustify", fontSize=11, fontName="Helvetica",
                                    spaceAfter=1, alignment=TA_JUSTIFY)
        header_cell_st = ParagraphStyle("OfHdrCell", fontSize=9, fontName="Helvetica-Bold",
                                        alignment=TA_CENTER)
        desc_st = ParagraphStyle("OfItemDesc", fontSize=9, fontName="Helvetica",
                                 leading=11, leftIndent=2, rightIndent=2, spaceAfter=2)

        # ── logo + company header ─────────────────────────────────────
        resolved = os.path.realpath(self.logo_path)
        company_info_html = (
            "<b>E-SAFE ENTERPRISES</b><br/>"
            "G-176, Boranada Industrial Park, Jodhpur-342012 (RAJ.)<br/>"
            "PHONE: +91291 2944321 MOBILE: +91 94133 24321<br/>"
            "email: esafe@esafe.co.in VISIT US: www.esafe.co.in<br/>"
            "<b>GSTN 08AACFE4028Q1Z5</b>"
        )
        ci_style = ParagraphStyle("OfCI", fontSize=10, fontName="Helvetica",
                                  spaceAfter=4, alignment=TA_LEFT, leading=12)
        if os.path.isfile(resolved):
            logo_cell: Any = Image(resolved, width=2 * inch, height=0.85 * inch)
            hdr_data = [[logo_cell, Paragraph(company_info_html, ci_style)]]
            hdr_tbl = Table(hdr_data, colWidths=[2.5 * inch, 5 * inch])
            hdr_tbl.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (0, 0), 0),
                ("RIGHTPADDING", (0, 0), (0, 0), 10),
                ("LEFTPADDING", (1, 0), (1, 0), 10),
                ("RIGHTPADDING", (1, 0), (1, 0), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]))
        else:
            ci_center = ParagraphStyle("OfCIC", fontSize=10, fontName="Helvetica",
                                       spaceAfter=4, alignment=TA_CENTER, leading=12)
            hdr_data = [[Paragraph(company_info_html, ci_center)]]
            hdr_tbl = Table(hdr_data, colWidths=[7.5 * inch])
            hdr_tbl.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ]))
        elements.append(hdr_tbl)
        elements.append(Spacer(1, 8))

        # ── offer number / date ───────────────────────────────────────
        offer_number = data.get("offer_number") or f"OFF-{data.get('id', 'XXX')}"
        raw_date = data.get("offer_date")
        if hasattr(raw_date, "strftime"):
            offer_date_str = raw_date.strftime("%d-%m-%Y")
        elif isinstance(raw_date, str) and raw_date:
            try:
                from datetime import datetime as _dt
                offer_date_str = _dt.strptime(raw_date[:10], "%Y-%m-%d").strftime("%d-%m-%Y")
            except ValueError:
                offer_date_str = raw_date
        else:
            offer_date_str = _date.today().strftime("%d-%m-%Y")

        elements.append(Paragraph(f"<b>{escape(offer_number)} Dated: {offer_date_str}</b>", normal_st))
        elements.append(Spacer(1, 4))

        # ── To, section ───────────────────────────────────────────────
        elements.append(Paragraph("<b>To,</b>", no_space_st))
        elements.append(Paragraph(escape(str(data.get("company_name") or "")), no_space_st))
        address = str(data.get("company_address") or "")
        for line in [ln.strip() for ln in address.split("\n") if ln.strip()]:
            elements.append(Paragraph(escape(line), no_space_st))
        elements.append(Spacer(1, 6))

        # ── Reference / Kind Attn / Contact ──────────────────────────
        offer_id = data.get("id") or "XXX"
        year = _date.today().strftime("%Y")
        # Use enquiry reference_number if present (matches original behaviour)
        ref_no = (
            str(data.get("enquiry_reference_number") or "").strip()
            or f"ESE/QUO/{offer_id}/{year}"
        )
        attn = str(data.get("contact_person") or "N/A")
        phone = str(data.get("company_phone") or "N/A")
        email_val = str(data.get("company_email") or "N/A")

        elements.append(Paragraph(f"<b>Reference No:</b> {escape(ref_no)}", no_space_st))
        elements.append(Paragraph(f"<b>Kind Attn:</b> {escape(attn)}", no_space_st))
        elements.append(Paragraph(
            f"<b>Mobile No.:</b> {escape(phone)} | <b>Email id:</b> {escape(email_val)}",
            normal_st,
        ))
        elements.append(Spacer(1, 6))

        # ── Greeting / intro ──────────────────────────────────────────
        elements.append(Paragraph("<b>Dear Sir,</b>", no_space_st))
        elements.append(Paragraph(
            "We thank you for your valued enquiry and confidence in E-Safe Range of Products.",
            no_space_st,
        ))
        elements.append(Paragraph(
            "At E-Safe \"Taking Care of Your Safety\" is more than a motto - It's our Mission. "
            "Whether you are working at any Height, work confidently with E-Safe. From Ladders to "
            "Fall Protection, E-Safe offer a range of Climbing and safety equipments which are "
            "engineered to provide maximum stability, safety and comfort at every height. Our "
            "products are designed to be used anywhere from home to the most demanding job sites.",
            justify_st,
        ))
        elements.append(Spacer(1, 6))

        # ── currency symbol ───────────────────────────────────────────
        currency = str(data.get("currency") or "INR")
        _sym_map = {
            "INR": "Rs.", "Rs.": "Rs.", "Rs": "Rs.",
            "USD": "$", "$": "$",
            "EUR": "€", "€": "€",
        }
        sym = _sym_map.get(currency, currency)

        # ── items table ───────────────────────────────────────────────
        items = data.get("items") or []
        rows: list = [[
            Paragraph("SN", header_cell_st),
            Paragraph("Item Description", header_cell_st),
            Paragraph("Unit", header_cell_st),
            Paragraph("Qty", header_cell_st),
            Paragraph(f"Rate in {sym}<br/>Per PC", header_cell_st),
            Paragraph("Total<br/>Amount", header_cell_st),
        ]]

        for i, item in enumerate(items, 1):
            qty = int(item.get("quantity") or 1)
            up = float(item.get("unit_price") or 0)
            tp = float(item.get("total_price") or qty * up)
            # Bold product name
            desc_html = f"<b>{escape(str(item.get('description') or ''))}</b>"
            # Definition (if stored on item)
            definition = str(item.get("definition") or "").strip()
            if definition:
                desc_html += f"<br/>{escape(definition)}"
            # Specifications — model number always last
            specs = [s for s in (item.get("specifications") or []) if str(s.get("value") or "").strip()]
            if specs:
                def _is_model(s: dict) -> bool:
                    return "model" in str(s.get("spec_name") or "").lower()
                specs = sorted(specs, key=lambda s: (1 if _is_model(s) else 0))
                desc_html += "<br/>Specifications:"
                for s in specs:
                    desc_html += (
                        f"<br/>• {escape(str(s.get('spec_name') or ''))}: "
                        f"{escape(str(s.get('value')))}"
                    )
            rows.append([
                str(i),
                Paragraph(desc_html, desc_st),
                "PC",
                str(qty),
                f"{sym} {up:,.2f}",
                f"{sym} {tp:,.2f}",
            ])

        # cost breakdown rows
        subtotal = float(data.get("subtotal") or 0)
        packing_pct = float(data.get("packing_charges_pct") or 0)
        freight = float(data.get("freight_charges") or 0)
        _gst = data.get("gst_pct")
        gst_pct = float(_gst if _gst is not None else 18)
        packing_amt = subtotal * (packing_pct / 100)
        assessable = subtotal + packing_amt + freight
        gst_amt = assessable * (gst_pct / 100)
        grand_total = assessable + gst_amt

        breakdown_start = len(rows)
        rows.append(["", "Sub-Total", "", "", "", f"{sym} {subtotal:,.2f}"])
        if packing_pct > 0:
            rows.append(["", f"Packing Charges ({packing_pct:.0f}%)", "", "", "", f"{sym} {packing_amt:,.2f}"])
        if freight > 0:
            rows.append(["", "Freight Charges", "", "", "", f"{sym} {freight:,.2f}"])
        if packing_pct > 0 or freight > 0:
            rows.append(["", "Assessable Value", "", "", "", f"{sym} {assessable:,.2f}"])
        if gst_pct > 0:
            rows.append(["", f"IGST ({gst_pct:.0f}%)", "", "", "", f"{sym} {gst_amt:,.2f}"])
        rows.append(["", "GRAND TOTAL", "", "", "", f"{sym} {grand_total:,.2f}"])
        total_row = len(rows) - 1

        col_widths = [0.5 * inch, 4.0 * inch, 0.6 * inch, 0.6 * inch, 1.0 * inch, 1.0 * inch]
        item_tbl = Table(rows, colWidths=col_widths, repeatRows=1)

        tbl_style = [
            ("GRID", (0, 0), (-1, -1), 1, colors.darkblue),
            ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
            ("VALIGN", (0, 1), (-1, breakdown_start - 1), "TOP"),
            # Header row
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightblue),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 9),
            ("ALIGN", (0, 0), (-1, 0), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, 0), 3),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 3),
            # Product rows
            ("FONTSIZE", (0, 1), (-1, breakdown_start - 1), 9),
            ("ALIGN", (0, 1), (0, breakdown_start - 1), "CENTER"),   # SN
            ("ALIGN", (1, 1), (1, breakdown_start - 1), "LEFT"),      # Description
            ("ALIGN", (2, 1), (2, breakdown_start - 1), "CENTER"),   # Unit
            ("ALIGN", (3, 1), (3, breakdown_start - 1), "CENTER"),   # Qty
            ("ALIGN", (4, 1), (4, breakdown_start - 1), "RIGHT"),    # Rate
            ("ALIGN", (5, 1), (5, breakdown_start - 1), "RIGHT"),    # Total
            ("TOPPADDING", (0, 1), (-1, breakdown_start - 1), 2),
            ("BOTTOMPADDING", (0, 1), (-1, breakdown_start - 1), 2),
            # Breakdown rows
            ("FONTSIZE", (0, breakdown_start), (-1, -1), 9),
            ("FONTNAME", (1, breakdown_start), (1, -1), "Helvetica-Bold"),
            ("ALIGN", (1, breakdown_start), (1, -1), "CENTER"),
            ("ALIGN", (5, breakdown_start), (5, -1), "RIGHT"),
            ("TOPPADDING", (0, breakdown_start), (-1, -1), 2),
            ("BOTTOMPADDING", (0, breakdown_start), (-1, -1), 2),
            # Grand Total row
            ("BACKGROUND", (0, total_row), (-1, total_row), colors.darkblue),
            ("TEXTCOLOR", (0, total_row), (-1, total_row), colors.white),
            ("FONTNAME", (0, total_row), (-1, total_row), "Helvetica-Bold"),
            ("FONTSIZE", (0, total_row), (-1, total_row), 10),
        ]
        for r in range(breakdown_start, len(rows)):
            tbl_style.append(("SPAN", (1, r), (4, r)))

        item_tbl.setStyle(TableStyle(tbl_style))
        elements.append(item_tbl)
        elements.append(Spacer(1, 8))

        # ── Terms & Conditions ────────────────────────────────────────
        tc = (data.get("terms_conditions") or "").strip()
        terms_rows: list = [["Terms & Conditions", ""]]
        if tc:
            for line in tc.split("\n"):
                if ":" in line:
                    key, val = line.split(":", 1)
                    key, val = key.strip(), val.strip()
                    if key == "Currency":
                        continue
                    if key == "Freight Charges":
                        try:
                            if float(val.replace("Rs. ", "").replace("$ ", "").replace(",", "")) == 0:
                                continue
                        except (ValueError, TypeError):
                            pass
                    if key == "GST Extra":
                        try:
                            if float(val.replace("%", "").strip()) == 0:
                                is_inr = currency.upper() in ("INR", "RS", "RS.")
                                val = "Inclusive" if is_inr else "Not Applicable"
                        except (ValueError, TypeError):
                            pass
                    terms_rows.append([key, val])
        else:
            if gst_pct == 0:
                is_inr = currency.upper() in ("INR", "RS", "RS.")
                _gst_label = "Inclusive" if is_inr else "Not Applicable"
            else:
                _gst_label = f"{gst_pct:.0f}%"
            terms_rows += [
                ["Rates Quoted above are", "Ex-works / FOR Destination"],
                ["Packing Charges", "3% Extra / Nil"],
                ["GST Extra", _gst_label],
                ["Transportation", "Extra to be paid by Buyer"],
                ["Delivery", str(data.get("valid_until") or "As per mutual agreement")],
                ["Payment", "As per mutual agreement"],
                ["Manufactured by and Brand", "E-SAFE"],
                ["Our GST No.", "08AACFE4028Q1Z5"],
            ]

        notes_text = (data.get("notes") or "").strip()
        if notes_text:
            terms_rows.append(["Notes", notes_text])

        terms_label_st = ParagraphStyle("TL", fontSize=9, fontName="Helvetica-Bold", alignment=TA_LEFT)
        terms_val_st = ParagraphStyle("TV", fontSize=9, fontName="Helvetica", alignment=TA_LEFT)
        proc_terms: list = []
        for idx, row in enumerate(terms_rows):
            if idx == 0:
                proc_terms.append(row)
            else:
                proc_terms.append([
                    Paragraph(escape(str(row[0])), terms_label_st),
                    Paragraph(escape(str(row[1])), terms_val_st),
                ])

        terms_tbl = Table(proc_terms, colWidths=[2.8 * inch, 3.5 * inch])
        alt_rows = len(proc_terms)
        t_style = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightblue),
            ("ALIGN", (0, 0), (-1, 0), "CENTER"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 12),
            ("SPAN", (0, 0), (-1, 0)),
            ("TOPPADDING", (0, 0), (-1, 0), 8),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 12),
            ("GRID", (0, 0), (-1, -1), 1, colors.darkblue),
            ("VALIGN", (0, 1), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 1), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
        ]
        for r in range(2, alt_rows, 2):
            t_style.append(("BACKGROUND", (0, r), (-1, r), colors.lightgrey))
        terms_tbl.setStyle(TableStyle(t_style))
        elements.append(terms_tbl)
        elements.append(Spacer(1, 8))

        # ── Closing ───────────────────────────────────────────────────
        elements.append(Paragraph("Thanking you,", normal_st))
        elements.append(Spacer(1, 8))
        elements.append(Paragraph("For E-Safe Enterprises", normal_st))
        elements.append(Spacer(1, 8))
        elements.append(Paragraph("Authorized Signatory", normal_st))

        pdf.build(elements)
        buffer.seek(0)
        logger.info("Generated offer PDF: %s", data.get("offer_number"))
        return buffer
