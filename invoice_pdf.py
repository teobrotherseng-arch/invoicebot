"""
Builds a clean, formal invoice/quotation PDF and embeds a PayNow QR code
for the total amount.
"""
import io
from datetime import date
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT

from paynow_qr import generate_paynow_qr_image

styles = getSampleStyleSheet()
title_style = ParagraphStyle("TitleStyle", parent=styles["Title"], alignment=TA_RIGHT, fontSize=20)
small_right = ParagraphStyle("SmallRight", parent=styles["Normal"], alignment=TA_RIGHT, fontSize=9)


def build_invoice_pdf(
    output_path: str,
    doc_type: str,
    invoice_number: str,
    client_name: str,
    client_phone: str,
    client_address: str,
    items: list,
    subtotal: float,
    total: float,
    company_name: str = "",
    company_address: str = "",
    paynow_proxy_type: str = "MOBILE",
    paynow_proxy_value: str = "",
    notes: str = None,
    job_date: str = None,
    logo_path: str = None,
    terms_text: str = None,
):
    doc = SimpleDocTemplate(output_path, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    elements = []

    doc_label = "QUOTATION" if doc_type == "quotation" else "INVOICE"

    # --- Header: logo (if set) + company info left, doc title right ---
    company_block = Paragraph(f"<b>{company_name}</b><br/>{company_address or ''}", styles["Normal"])
    if logo_path:
        logo_img = Image(logo_path, width=30 * mm, height=30 * mm, kind="proportional")
        left_cell = Table([[logo_img], [Spacer(1, 2 * mm)], [company_block]], colWidths=[100 * mm])
    else:
        left_cell = company_block

    header_data = [[left_cell, Paragraph(doc_label, title_style)]]
    header_table = Table(header_data, colWidths=[100 * mm, 70 * mm])
    header_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    elements.append(header_table)
    elements.append(Spacer(1, 10 * mm))

    # --- Doc meta + client details ---
    meta_lines = (
        f"<b>{doc_label.title()} #:</b> {invoice_number}<br/>"
        f"<b>Date:</b> {job_date or date.today().isoformat()}"
    )
    client_lines = (
        f"<b>Bill To:</b><br/>{client_name or '-'}<br/>"
        f"{client_address or ''}<br/>"
        f"{client_phone or ''}"
    )
    meta_table = Table(
        [[Paragraph(client_lines, styles["Normal"]), Paragraph(meta_lines, small_right)]],
        colWidths=[100 * mm, 70 * mm],
    )
    meta_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    elements.append(meta_table)
    elements.append(Spacer(1, 10 * mm))

    # --- Line items table ---
    table_data = [["Description", "Qty", "Unit Price (S$)", "Amount (S$)"]]
    for item in items:
        table_data.append([
            item.get("description", ""),
            str(item.get("qty", "")),
            f"{item.get('unit_price', 0):.2f}",
            f"{item.get('amount', 0):.2f}",
        ])
    table_data.append(["", "", "Subtotal", f"{subtotal:.2f}"])
    table_data.append(["", "", "Total Due", f"{total:.2f}"])

    items_table = Table(table_data, colWidths=[80 * mm, 20 * mm, 35 * mm, 35 * mm])
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2b2b2b")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -3), 0.5, colors.grey),
        ("FONTNAME", (2, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (2, -2), (-1, -2), 0.5, colors.grey),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 8 * mm))

    if notes:
        elements.append(Paragraph(f"<b>Notes:</b> {notes}", styles["Normal"]))
        elements.append(Spacer(1, 8 * mm))

    if terms_text:
        elements.append(Paragraph("<b>Terms &amp; Conditions</b>", styles["Normal"]))
        elements.append(Spacer(1, 2 * mm))
        # Preserve line breaks the company typed when setting their terms
        for line in terms_text.split("\n"):
            if line.strip():
                elements.append(Paragraph(line.strip(), ParagraphStyle(
                    "TermsLine", parent=styles["Normal"], fontSize=8, leading=11
                )))
        elements.append(Spacer(1, 8 * mm))

    # --- PayNow QR (invoices only - quotations don't need payment yet) ---
    if doc_type == "invoice" and total > 0 and paynow_proxy_value:
        qr_bytes = generate_paynow_qr_image(total, invoice_number, paynow_proxy_type, paynow_proxy_value, merchant_name=company_name)
        qr_image = Image(io.BytesIO(qr_bytes), width=45 * mm, height=45 * mm)
        elements.append(Paragraph("<b>Scan to pay via PayNow</b>", styles["Normal"]))
        elements.append(Spacer(1, 3 * mm))
        elements.append(qr_image)
        elements.append(Paragraph(f"Amount: S${total:.2f}", styles["Normal"]))

    doc.build(elements)
    return output_path


def render_invoice_from_template(
    output_path: str,
    background_pdf_path: str,
    field_map: dict,
    invoice_number: str,
    client_name: str,
    client_phone: str,
    client_address: str,
    items: list,
    subtotal: float,
    total: float,
    notes: str = None,
    job_date: str = None,
    paynow_proxy_type: str = "MOBILE",
    paynow_proxy_value: str = "",
    paynow_bank: str = "",
    paynow_beneficiary: str = "",
    doc_type: str = "invoice",
):
    """
    Draws new invoice data back into a previously-learned client template
    (see pdf_template.build_template), preserving that client's exact
    logo/layout/branding.
    """
    reader = PdfReader(background_pdf_path)
    writer = PdfWriter()

    header_values = {
        "client_name": client_name or "-",
        "client_phone": client_phone or "",
        "client_address": client_address or "",
        "invoice_number": invoice_number,
        "date": job_date or date.today().isoformat(),
        "subtotal": f"{subtotal:.2f}",
        "total": f"{total:.2f}",
        "notes": notes or "",
        # These follow whatever the company has configured via /setpaynow -
        # never left as whatever the original example PDF happened to show.
        "paynow_bank": paynow_bank or "",
        "paynow_beneficiary": paynow_beneficiary or "",
        "paynow_number": paynow_proxy_value or "",
    }

    item_page = field_map.get("item_page", 0)
    item_start_y = field_map.get("item_start_y")
    row_height = field_map.get("item_row_height", 14)
    columns = field_map.get("item_columns", {})
    example_row_count = field_map.get("example_row_count", 1)
    extra_rows = max(0, len(items) - example_row_count)
    shift_down = extra_rows * row_height  # applied to anything below the table
    qr_anchor = field_map.get("qr_anchor")

    for page_num, page in enumerate(reader.pages):
        pw = field_map["pages"][page_num]["width"]
        ph = field_map["pages"][page_num]["height"]

        buf = io.BytesIO()
        c = rl_canvas.Canvas(buf, pagesize=(pw, ph))
        c.setFillColor(colors.black)

        for field, pos in field_map.get("header_fields", {}).items():
            if pos["page"] != page_num or field not in header_values:
                continue
            y = pos["y"]
            # Fields printed below the item table (lower y = further down the
            # page) need to shift down if this invoice has more rows than the
            # example did, so they don't get crowded by the extra rows.
            if item_start_y is not None and y < item_start_y:
                y -= shift_down
            c.setFont("Helvetica", pos["size"])
            c.drawString(pos["x"], y, str(header_values[field]))

        if page_num == item_page and item_start_y is not None:
            for i, item in enumerate(items):
                y = item_start_y - i * row_height
                row_values = {
                    "description": str(item.get("description", "")),
                    "qty": str(item.get("qty", "")),
                    "unit_price": f"{item.get('unit_price', 0):.2f}",
                    "amount": f"{item.get('amount', 0):.2f}",
                }
                for col, pos in columns.items():
                    c.setFont("Helvetica", pos["size"])
                    c.drawString(pos["x"], y, row_values.get(col, ""))

        # PayNow QR - always regenerated from the company's current
        # /setpaynow settings, never the original example's QR image.
        if doc_type == "invoice" and total > 0 and paynow_proxy_value:
            draw_qr_here = qr_anchor and qr_anchor["page"] == page_num
            if draw_qr_here or (page_num == item_page and not qr_anchor):
                qr_bytes = generate_paynow_qr_image(
                    total, invoice_number, paynow_proxy_type, paynow_proxy_value
                )
                from reportlab.lib.utils import ImageReader
                if qr_anchor and draw_qr_here:
                    x, y, w, h = qr_anchor["x"], qr_anchor["y"], qr_anchor["width"], qr_anchor["height"]
                else:
                    x, y, w, h = 30, 30, 90, 90
                    c.setFont("Helvetica", 8)
                    c.drawString(x, y - 10, "Scan to pay via PayNow")
                c.drawImage(ImageReader(io.BytesIO(qr_bytes)), x, y, width=w, height=h)

        c.save()
        buf.seek(0)
        overlay_reader = PdfReader(buf)
        page.merge_page(overlay_reader.pages[0])
        writer.add_page(page)

    with open(output_path, "wb") as f:
        writer.write(f)
    return output_path
