"""
Takes the raw (possibly mixed Chinese/English) transcript and asks Claude to:
  1. Translate/normalize it to clear English
  2. Pull out structured invoice/quotation fields as JSON

We ask for JSON-only output and parse it defensively, since this feeds
straight into the PDF generator and database.
"""
import json
import re
from anthropic import Anthropic
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

client = Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are a back-office assistant for a Singapore cleaning company (JS2 Cleaning).
Staff send you voice-note transcripts describing a job they just did or quoted for a client.
The transcript may mix Chinese and English in the same sentence.

Your job:
1. Understand the transcript regardless of language mixing.
2. Produce a clean, professional ENGLISH summary of the job.
3. Extract structured fields for an invoice or quotation.

Respond with ONLY a single valid JSON object, no markdown fences, no commentary, matching exactly this schema:

{
  "doc_type": "invoice" | "quotation",
  "client_name": string | null,
  "client_phone": string | null,
  "client_address": string | null,
  "job_date": string | null,          // ISO format YYYY-MM-DD if mentioned, else null
  "items": [
    {"description": string, "qty": number, "unit_price": number, "amount": number}
  ],
  "notes": string | null,
  "english_summary": string           // 1-3 sentence plain-English summary of the job
}

Rules:
- If only a total amount is mentioned with no breakdown, put one line item with qty=1, unit_price=amount, amount=amount.
- If the staff member calls it a "quote"/"quotation"/"estimate", doc_type is "quotation", otherwise default to "invoice".
- If a field truly isn't mentioned, use null (for strings) - never invent client details.
- Numbers must be plain numbers (no currency symbols, no commas).
"""


def extract_invoice_data(raw_transcript: str) -> dict:
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Transcript:\n\n{raw_transcript}"}],
    )
    text = "".join(block.text for block in message.content if block.type == "text").strip()

    # Defensive parsing in case the model wraps JSON in fences despite instructions
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Could not parse AI response as JSON: {e}\nRaw response:\n{text}")

    data.setdefault("items", [])
    for item in data["items"]:
        item["amount"] = round(float(item.get("qty", 1)) * float(item.get("unit_price", 0)), 2) \
            if item.get("amount") is None else round(float(item["amount"]), 2)

    data["subtotal"] = round(sum(i["amount"] for i in data["items"]), 2)
    return data
