"""
Learns a reusable invoice/quotation template from a client's existing filled-in
PDF, so future invoices can be rendered back into their exact layout.

Pipeline:
  1. extract_lines()      - pdfplumber gives exact (x, y, size, font) for every
                             line of text on the page.
  2. classify_lines()     - Claude reads the text of every line and labels each
                             one either "static" (logo text, column headers,
                             terms, company letterhead - keep forever) or
                             "variable" (this example's client name, amounts,
                             dates, items - erase and treat as a fillable slot).
  3. build_template()     - Rasterizes each page to an image, paints over every
                             "variable" region in white, and produces a field
                             position map describing exactly where to redraw
                             new data. Rasterizing (rather than just drawing a
                             white box on top of the original vector PDF) is
                             deliberate: a white box only visually covers the
                             old text - the underlying text data would still
                             be present and recoverable via copy-paste or a
                             text extractor. Flattening to pixels means the
                             erased example data (a real client's name, real
                             amounts) is genuinely gone, not just hidden.

Known limitation: if a future invoice has more line items than the example
had, extra rows are placed using the row spacing detected from the example
(extrapolated), not literally copied from rows that didn't exist in the source.

Trade-off: because the background becomes a rasterized image, the static
branding/terms text is no longer selectable/searchable in the final PDF (it
still looks identical and prints the same). Only the newly-drawn data
(client name, items, totals, etc.) remains real, selectable text.
"""
import json
import re
import pdfplumber
from pdf2image import convert_from_path
from PIL import ImageDraw
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.utils import ImageReader
from io import BytesIO

from anthropic import Anthropic
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

client = Anthropic(api_key=ANTHROPIC_API_KEY)


def extract_lines(pdf_path: str) -> list:
    """
    Returns a flat list of every line (or column segment) of text on every
    page, with its exact bounding box and font info:
    {id, page, x0, top, x1, bottom, text, size, fontname}

    Words on the same visual row are grouped together UNLESS there's a large
    horizontal gap between them (e.g. a table with Description / Qty / Price
    columns spaced apart) - in that case each column becomes its own segment,
    since each needs its own tracked position.
    """
    COLUMN_GAP_THRESHOLD = 18  # points; gaps wider than this start a new segment

    lines = []
    line_id = 0
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            words = page.extract_words(extra_attrs=["size", "fontname"])
            buckets = {}
            for w in words:
                key = round(w["top"] / 2) * 2
                buckets.setdefault(key, []).append(w)

            for key in sorted(buckets.keys()):
                ws = sorted(buckets[key], key=lambda w: w["x0"])
                # Split into column segments wherever the gap is too wide
                segments = [[ws[0]]]
                for w in ws[1:]:
                    prev = segments[-1][-1]
                    gap = w["x0"] - prev["x1"]
                    if gap > COLUMN_GAP_THRESHOLD:
                        segments.append([w])
                    else:
                        segments[-1].append(w)

                for seg in segments:
                    text = " ".join(w["text"] for w in seg)
                    lines.append({
                        "id": line_id,
                        "page": page_num,
                        "page_width": page.width,
                        "page_height": page.height,
                        "x0": seg[0]["x0"],
                        "top": seg[0]["top"],
                        "x1": seg[-1]["x1"],
                        "bottom": seg[0]["bottom"],
                        "text": text,
                        "size": seg[0].get("size", 10),
                        "fontname": seg[0].get("fontname", "Helvetica"),
                    })
                    line_id += 1
    return lines


CLASSIFY_SYSTEM_PROMPT = """You are analyzing a scanned/exported invoice or quotation PDF that has
already been filled out for one example client, in order to build a reusable template.

For every line of text given (with an id), decide:
- "static": permanent branding/structure that should appear on EVERY future invoice unchanged
  (company name, company address/logo text, column headers like "Description"/"Qty"/"Amount",
  labels like "Bill To:", terms & conditions, footer text, the word "INVOICE"/"QUOTATION").
- "variable": specific to THIS example only, and must be replaced with new data on every future
  invoice (the example client's name/phone/address, invoice number, date, line item descriptions,
  quantities, unit prices, amounts, subtotal, total, notes).

For "variable" lines, also assign a "field" name from this fixed set where applicable:
  client_name, client_phone, client_address, invoice_number, date, subtotal, total, notes,
  paynow_bank, paynow_beneficiary, paynow_number
(paynow_bank/paynow_beneficiary/paynow_number are the example's bank name, account beneficiary
name, and PayNow number shown in a "Pay To" section - these must be treated as variable so they
can be swapped for whichever company is actually using this template, not left as the example's.)
If a variable line is part of the repeating line-items table (one row per item), instead set
"field": "item_row" and specify which column it is via "column": one of
  description, qty, unit_price, amount

Respond with ONLY a JSON array (no markdown fences, no commentary), one object per input line id:
[{"id": 0, "type": "static"}, {"id": 1, "type": "variable", "field": "client_name"}, ...]
For item_row lines: {"id": 5, "type": "variable", "field": "item_row", "column": "description"}
"""


def classify_lines(lines: list, batch_size: int = 60) -> dict:
    """
    Returns {line_id: {"type":..., "field":..., "column":...}}

    Classifies in batches rather than one big call - a multi-page quotation
    with lengthy terms & conditions can easily have 150-250+ lines, and
    asking for one classification object per line in a single response risks
    the model's output being cut off before it finishes the whole document.
    """
    results = {}
    for i in range(0, len(lines), batch_size):
        batch = lines[i:i + batch_size]
        payload = [{"id": l["id"], "text": l["text"]} for l in batch]
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4000,
            system=CLASSIFY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload)}],
        )
        text = "".join(b.text for b in message.content if b.type == "text").strip()
        text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
        classified = json.loads(text)
        results.update({c["id"]: c for c in classified})
    return results


STATIC_KEYWORDS = {
    "description", "qty", "quantity", "unit price", "amount", "total", "subtotal",
    "bill to", "billed to", "pay to", "bank", "beneficiary", "paynow number",
    "invoice", "quotation", "date:", "address:", "notes", "terms", "balance amount",
}

_MONEY_RE = re.compile(r"^\$?\d[\d,]*\.\d{2}$")
_DATE_RE = re.compile(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$|^\d{4}-\d{2}-\d{2}$")
_REF_RE = re.compile(r"^#?[A-Za-z]{0,4}\d{3,}$")


def _apply_safety_net(lines: list, labels: dict) -> dict:
    """
    Post-processes Claude's classification with hard rules, because a
    misclassification here means either a client's real invoice shows a
    stale example client's data (bad), or a static label gets wiped (minor
    cosmetic issue). We bias hard toward the safer failure mode.
    """
    for l in lines:
        text = l["text"].strip()
        norm = text.lower().rstrip(":")
        info = labels.get(l["id"], {"type": "static"})

        # Never erase known column headers / labels, even if Claude said variable -
        # these must always survive onto every future invoice.
        if norm in STATIC_KEYWORDS:
            labels[l["id"]] = {"type": "static"}
            continue

        # Force-erase anything that looks like a concrete value the classifier
        # missed (a stray amount, date, or invoice reference marked "static").
        # No field mapping is assigned, so it's erased and left blank rather
        # than risk leaving a previous client's real data on the page.
        if info.get("type") == "static" and (_MONEY_RE.match(text) or _DATE_RE.match(text) or _REF_RE.match(text)):
            labels[l["id"]] = {"type": "variable", "field": None}

    return labels


PAYNOW_KEYWORDS = ("paynow", "pay to", "scan to pay", "bank", "beneficiary")


def _find_qr_images(pdf_path: str, lines: list) -> dict:
    """
    Finds embedded raster images positioned near payment-related text (bank/
    beneficiary/PayNow labels) and treats them as the example's QR code -
    these need to be erased and replaced, unlike a company logo (which is
    typically vector-drawn, not a raster image, and untouched by this).
    Returns {page_num: {"x0","top","x1","bottom"}} for the first match per page.
    """
    payment_line_positions = [
        (l["page"], l["top"]) for l in lines
        if any(kw in l["text"].lower() for kw in PAYNOW_KEYWORDS)
    ]

    qr_images = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            nearby_tops = [top for pg, top in payment_line_positions if pg == page_num]
            if not nearby_tops:
                continue
            for img in page.images:
                # "near" = within 250pt vertically of any payment-related text on the same page
                if any(abs(img["top"] - t) < 250 for t in nearby_tops):
                    qr_images[page_num] = {
                        "x0": img["x0"], "top": img["top"], "x1": img["x1"], "bottom": img["bottom"]
                    }
                    break  # one QR per page is the expected case
    return qr_images


def build_template(pdf_path: str, output_background_path: str) -> dict:
    """
    Main entry point. Reads the uploaded example PDF, erases the
    client-specific example data, saves a reusable background PDF to
    output_background_path, and returns a field position map (JSON-safe dict)
    to store alongside it.
    """
    lines = extract_lines(pdf_path)
    labels = classify_lines(lines)
    labels = _apply_safety_net(lines, labels)
    qr_images = _find_qr_images(pdf_path, lines)

    reader = PdfReader(pdf_path)
    page_sizes = [(float(p.mediabox.width), float(p.mediabox.height)) for p in reader.pages]

    RENDER_DPI = 200
    pil_pages = convert_from_path(pdf_path, dpi=RENDER_DPI)

    writer = PdfWriter()
    field_map = {"pages": [], "header_fields": {}, "item_columns": {}, "item_rows": []}

    # Group lines by page
    pages_lines = {}
    for l in lines:
        pages_lines.setdefault(l["page"], []).append(l)

    for page_num, pil_img in enumerate(pil_pages):
        page_w, page_h = page_sizes[page_num]
        field_map["pages"].append({"width": page_w, "height": page_h})
        scale = pil_img.width / page_w  # pixels per PDF point

        draw = ImageDraw.Draw(pil_img)

        for l in pages_lines.get(page_num, []):
            info = labels.get(l["id"], {"type": "static"})
            if info.get("type") != "variable":
                continue  # leave static branding untouched

            # Paint over the example's value directly on the pixels (small
            # padding so nothing bleeds through at the edges)
            pad = 1.5
            px0 = (l["x0"] - pad) * scale
            py0 = (l["top"] - pad) * scale
            px1 = (l["x1"] + pad) * scale
            py1 = (l["bottom"] + pad) * scale
            draw.rectangle([px0, py0, px1, py1], fill="white")

            field = info.get("field")
            baseline_y = page_h - l["bottom"] + 2  # reportlab y for drawString baseline

            if field == "item_row":
                field_map["item_rows"].append({
                    "page": page_num, "top": l["top"], "x0": l["x0"], "y": baseline_y,
                    "size": l["size"], "column": info.get("column", "description"),
                })
            elif field:
                field_map["header_fields"][field] = {
                    "page": page_num, "x": l["x0"], "y": baseline_y, "size": l["size"],
                }

        if page_num in qr_images:
            img = qr_images[page_num]
            pad = 3
            px0 = (img["x0"] - pad) * scale
            py0 = (img["top"] - pad) * scale
            px1 = (img["x1"] + pad) * scale
            py1 = (img["bottom"] + pad) * scale
            draw.rectangle([px0, py0, px1, py1], fill="white")
            field_map["qr_anchor"] = {
                "page": page_num, "x": img["x0"], "y": page_h - img["bottom"],
                "width": img["x1"] - img["x0"], "height": img["bottom"] - img["top"],
            }

        # Place the now-flattened (fully rasterized - no recoverable old
        # text) image onto a PDF page sized to exactly match the original.
        buf = BytesIO()
        c = rl_canvas.Canvas(buf, pagesize=(page_w, page_h))
        c.drawImage(ImageReader(pil_img), 0, 0, width=page_w, height=page_h)
        c.save()
        buf.seek(0)
        writer.add_page(PdfReader(buf).pages[0])

    with open(output_background_path, "wb") as f:
        writer.write(f)

    # Derive item table geometry: column x-positions (avg across detected rows)
    # and row height (spacing between distinct rows, or a sane fallback).
    if field_map["item_rows"]:
        by_column = {}
        for r in field_map["item_rows"]:
            by_column.setdefault(r["column"], []).append(r)
        for col, rows in by_column.items():
            avg_x = sum(r["x0"] for r in rows) / len(rows)
            avg_size = sum(r["size"] for r in rows) / len(rows)
            field_map["item_columns"][col] = {"x": avg_x, "size": avg_size}

        # Cluster row "top" positions within a tolerance rather than requiring
        # exact float equality - different columns on the same visual row can
        # have tiny sub-pixel differences in their reported top, which would
        # otherwise cause the same row to be miscounted as multiple rows.
        ROW_CLUSTER_TOLERANCE = 4  # points
        sorted_tops = sorted(r["top"] for r in field_map["item_rows"])
        clustered_tops = []
        for t in sorted_tops:
            if not clustered_tops or t - clustered_tops[-1] > ROW_CLUSTER_TOLERANCE:
                clustered_tops.append(t)

        if len(clustered_tops) >= 2:
            gaps = [clustered_tops[i + 1] - clustered_tops[i] for i in range(len(clustered_tops) - 1)]
            row_height = sum(gaps) / len(gaps)
        else:
            avg_size = field_map["item_columns"].get("description", {}).get("size", 10)
            row_height = avg_size * 1.8  # fallback spacing when only 1 example row

        first_row = min(field_map["item_rows"], key=lambda r: r["top"])
        field_map["item_start_y"] = first_row["y"]
        field_map["item_row_height"] = row_height
        field_map["item_page"] = first_row["page"]
        field_map["example_row_count"] = len(clustered_tops)

    return field_map
