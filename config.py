"""
Central config. Everything is loaded from environment variables so the
same code runs locally (.env file) and on Railway/Render (dashboard env vars).
"""
import os
from dotenv import load_dotenv

load_dotenv()

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# Comma-separated Telegram numeric user IDs allowed to approve/send invoices (e.g. Adam, Jo)
ADMIN_TELEGRAM_IDS = [
    int(x) for x in os.environ.get("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip()
]

# Comma-separated Telegram numeric user IDs allowed to submit job voice notes (staff)
# Leave empty to allow anyone to submit (not recommended for production).
STAFF_TELEGRAM_IDS = [
    int(x) for x in os.environ.get("STAFF_TELEGRAM_IDS", "").split(",") if x.strip()
]

# --- AI services ---
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]          # used for Whisper transcription
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]    # used for translation + extraction
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

# --- Database ---
# Railway/Render Postgres plugins expose this automatically as DATABASE_URL
DATABASE_URL = os.environ["DATABASE_URL"]

# --- Company / display defaults ---
COMPANY_NAME = os.environ.get("COMPANY_NAME", "")       # only used as a fallback label
COMPANY_ADDRESS = os.environ.get("COMPANY_ADDRESS", "")

# Where generated PDFs are written before being sent back over Telegram
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/tmp/invoices")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Where learned client templates (background PDFs) are stored
TEMPLATE_DIR = os.environ.get("TEMPLATE_DIR", "/tmp/templates")
os.makedirs(TEMPLATE_DIR, exist_ok=True)
