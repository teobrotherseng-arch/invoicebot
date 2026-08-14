"""
Main bot entrypoint - schema-isolated multi-company version.

Architecture (see db.py for the schema-per-company details):
  - Give each client company its own dedicated Telegram group and add this
    bot to it. Every message from that group is automatically treated as
    belonging to that company - no manual switching required.
  - Onboard a company by running /newcompany <name> INSIDE their group.
    That links the group's chat id to the company and creates its own
    private database schema.
  - Optionally follow up by sending a PDF of one of their existing filled
    quotations/invoices right after - the bot learns their exact layout.
  - Each company can have a monthly invoice cap (/setlimit) - once hit,
    new invoices are hard-blocked until next month or a manual reset.

Flow (per company, same shape as before):
  1. Staff sends a voice note describing a job -> handle_voice()
  2. Transcribed + translated/extracted via Claude.
  3. Client looked up/created within that company's private schema. Missing
     phone/address is flagged to the admin.
  4. Draft sent back into the SAME chat with Approve / Reject buttons -
     only users in ADMIN_TELEGRAM_IDS can actually tap them.
  5. On Approve -> PDF invoice generated (using the company's learned
     template if one exists, otherwise a clean generic layout) with a
     PayNow QR for that company, and sent back.

Run: python bot.py
"""
import os
import re
import logging
import tempfile
from datetime import datetime
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, MessageHandler, CommandHandler, CallbackQueryHandler,
    ContextTypes, filters
)

from config import TELEGRAM_BOT_TOKEN, ADMIN_TELEGRAM_IDS, OUTPUT_DIR, TEMPLATE_DIR
from db import init_db, get_session, get_company_session, get_company_by_chat, create_company_schema, Client, Invoice, Company
from transcribe import transcribe_audio
from extract import extract_invoice_data
from invoice_pdf import build_invoice_pdf
from logo_processing import process_logo

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def require_message(func):
    """Guards handlers against updates with no usable message (e.g. an
    edited message with only update.edited_message set, or other update
    types with no message content at all) - update.effective_message covers
    both new and edited messages, but can still be None in rarer cases."""
    @wraps(func)
    async def wrapper(update, context):
        if not update.effective_message:
            return
        return await func(update, context)
    return wrapper

# In-memory: admin_id -> company_id, set right after /newcompany or /setlogo
# so the next image that admin sends is saved as that company's logo.
PENDING_LOGO_UPLOAD = {}

# In-memory: admin_id -> {"company_id", "step", "proxy_type", "proxy_value", "bank"}
# Drives the button-guided PayNow setup (mobile/UEN choice -> value -> bank -> beneficiary).
PENDING_PAYNOW_WIZARD = {}

# In-memory: admin_id -> company_id, set when the Settings menu's "Terms &
# Conditions" button is tapped, so the next plain-text message is saved as
# that company's terms text.
PENDING_TERMS_INPUT = {}

# In-memory: admin_id -> {"invoice_id", "step": "phone"/"address"}
# Drives the button-guided "fill in missing client details" flow.
PENDING_CLIENT_DETAILS_WIZARD = {}

# In-memory: admin_id -> company_id, set when Settings > "Set company
# address" is tapped, so the next plain-text message is saved as that
# company's address (shown on the invoice header).
PENDING_ADDRESS_INPUT = {}

# In-memory: admin_id -> company_id, set when Settings > "Set company UEN"
# is tapped, so the next plain-text message is saved as that company's UEN.
PENDING_UEN_INPUT = {}

# In-memory: admin_id -> company_id, set when Settings > "Manage approvers"
# > "Add approver" is tapped, so the next plain-text message (a numeric
# Telegram ID) is added to that company's approver list.
PENDING_APPROVER_INPUT = {}


def is_admin(user_id: int) -> bool:
    return not ADMIN_TELEGRAM_IDS or user_id in ADMIN_TELEGRAM_IDS


def can_approve_for(user_id: int, company: Company) -> bool:
    """Global admins (you/Jo) can always approve anywhere. A company's own
    designated approvers can approve only within their own company."""
    if is_admin(user_id):
        return True
    return user_id in (company.approver_telegram_ids or [])


def next_invoice_number(company_session, doc_type: str) -> str:
    prefix = "QT" if doc_type == "quotation" else "INV"
    count = company_session.query(Invoice).filter(Invoice.doc_type == doc_type).count() + 1
    return f"{prefix}-{datetime.now().strftime('%Y%m')}-{count:04d}"


def invoices_this_month(company_session) -> int:
    start_of_month = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return company_session.query(Invoice).filter(Invoice.created_at >= start_of_month).count()


def find_or_create_client(company_session, name, phone, address):
    client = None
    if phone:
        client = company_session.query(Client).filter(Client.phone == phone).first()
    if not client and name:
        client = company_session.query(Client).filter(Client.name.ilike(name)).first()

    if client:
        if phone and not client.phone:
            client.phone = phone
        if address and not client.address:
            client.address = address
        company_session.commit()
        return client

    client = Client(name=name or "Unknown client", phone=phone, address=address)
    company_session.add(client)
    company_session.commit()
    return client


def is_name_missing(client) -> bool:
    return not client.name or client.name.strip().lower() == "unknown client"


def missing_client_fields(client) -> list:
    """Returns which of name/phone/address still need to be filled in, in
    the order they should be asked for."""
    missing = []
    if is_name_missing(client):
        missing.append("name")
    if not client.phone:
        missing.append("phone")
    if not client.address:
        missing.append("address")
    return missing


def draft_caption(inv: Invoice, client: Client, company: Company) -> str:
    lines = [
        f"📋 *{inv.doc_type.upper()} draft* `{inv.invoice_number}` (id: {inv.id})",
        f"*Company:* {company.name}",
        "",
        f"*Client:* {'⚠️ MISSING' if is_name_missing(client) else client.name}",
        f"*Phone:* {client.phone or '⚠️ MISSING'}",
        f"*Address:* {client.address or '⚠️ MISSING'}",
        "",
        "*Items:*",
    ]
    for item in inv.items:
        lines.append(f"  • {item['description']} — {item['qty']} × S${item['unit_price']:.2f} = S${item['amount']:.2f}")
    lines.append("")
    lines.append(f"*Total: S${inv.subtotal:.2f}*")
    lines.append("")
    lines.append(f"_Summary: {inv.english_summary}_")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Company management
# ---------------------------------------------------------------------------

def paynow_type_buttons(company_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📱 Mobile number", callback_data=f"paynow_type:mobile:{company_id}"),
        InlineKeyboardButton("🏢 Company UEN", callback_data=f"paynow_type:uen:{company_id}"),
    ]])


@require_message
async def new_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/newcompany <name> - run this INSIDE that company's dedicated Telegram group."""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /newcompany <company name> (run this inside their dedicated group chat)")
        return

    chat_id = str(update.effective_chat.id)
    name = " ".join(context.args)

    session = get_session()
    try:
        existing = get_company_by_chat(session, chat_id)
        if existing:
            await update.effective_message.reply_text(
                f"🏢 This chat is already linked to *{existing.name}*.\n\n"
                f"Want to change something? Run ⚙️ /settings",
                parse_mode="Markdown",
            )
            return

        company = Company(name=name, telegram_chat_id=chat_id, schema_name="pending")
        session.add(company)
        session.commit()

        schema_name = f"company_{company.id}"
        company.schema_name = schema_name
        session.commit()
        create_company_schema(schema_name)

        await update.effective_message.reply_text(
            f"✅ *{name}* is set up! (id: {company.id})\n\n"
            f"🏢 This chat is now their home - every voice note sent here becomes an invoice for them.\n\n"
            f"👇 Let's set up PayNow first, then run ⚙️ /settings anytime to add their logo, "
            f"terms & conditions, or invoice limit.",
            parse_mode="Markdown",
        )
        await update.effective_message.reply_text(
            f"💰 Is {name}'s PayNow a mobile number or a company UEN?",
            reply_markup=paynow_type_buttons(company.id),
        )
    finally:
        session.close()


@require_message
async def set_paynow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setpaynow - launches a button-guided setup. Or, for direct use:
    /setpaynow <mobile|uen> <value> [bank=... beneficiary=...]
    """
    if not is_admin(update.effective_user.id):
        return

    session = get_session()
    try:
        company = get_company_by_chat(session, update.effective_chat.id)
        if not company:
            await update.effective_message.reply_text("No company linked to this chat yet - run /newcompany first.")
            return

        if not context.args:
            await update.effective_message.reply_text(
                f"Is {company.name}'s PayNow a mobile number or a company UEN?",
                reply_markup=paynow_type_buttons(company.id),
            )
            return

        if len(context.args) < 2:
            await update.effective_message.reply_text(
                "Usage: /setpaynow <mobile|uen> <value> [bank=your bank] [beneficiary=account name]\n"
                "Or just run /setpaynow with nothing after it for a guided setup."
            )
            return

        proxy_type = context.args[0].upper()
        proxy_value = context.args[1]
        rest = " ".join(context.args[2:])
        kv = {}
        for key in ("bank", "beneficiary"):
            m = re.search(rf"{key}=(.*?)(?=\s+(?:bank|beneficiary)=|$)", rest)
            if m and m.group(1).strip():
                kv[key] = m.group(1).strip()

        company.paynow_proxy_type = proxy_type
        company.paynow_proxy_value = proxy_value
        if "bank" in kv:
            company.bank_name = kv["bank"]
        if "beneficiary" in kv:
            company.beneficiary_name = kv["beneficiary"]
        session.commit()
        await update.effective_message.reply_text(
            f"✅ PayNow set for {company.name}: {proxy_type} {proxy_value}\n"
            f"Bank: {company.bank_name or '(not set)'}\n"
            f"Beneficiary: {company.beneficiary_name or '(not set)'}"
        )
    finally:
        session.close()


async def handle_paynow_wizard_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles taps on the Mobile/UEN buttons that kick off the PayNow wizard."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("Only admins can set this up.", show_alert=True)
        return

    _, proxy_type, company_id_str = query.data.split(":")
    company_id = int(company_id_str)

    PENDING_PAYNOW_WIZARD[query.from_user.id] = {
        "company_id": company_id, "step": "value", "proxy_type": proxy_type,
    }
    label = "mobile number (e.g. +6591234567)" if proxy_type == "mobile" else "UEN (e.g. 201912345A)"
    await query.edit_message_text(f"Got it - please reply here with the {label}.")


@require_message
async def handle_wizard_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Advances the PayNow wizard, captures terms & conditions text, or
    advances the client-details wizard, one plain-text reply at a time.
    Ignores messages from anyone not currently mid-wizard, so it never
    interferes with normal group chat."""
    user_id = update.effective_user.id

    if user_id in PENDING_CLIENT_DETAILS_WIZARD:
        state = PENDING_CLIENT_DETAILS_WIZARD[user_id]
        text = update.effective_message.text.strip()
        inv_id = state["invoice_id"]

        reg_session = get_session()
        try:
            company = get_company_by_chat(reg_session, update.effective_chat.id)
        finally:
            reg_session.close()

        cs = get_company_session(company)
        try:
            inv = cs.query(Invoice).get(inv_id)
            client = inv.client

            if state["step"] == "name":
                client.name = text
            elif state["step"] == "phone":
                client.phone = text
            elif state["step"] == "address":
                client.address = text
            cs.commit()

            still_missing = missing_client_fields(client)
            if still_missing:
                next_step = still_missing[0]
                state["step"] = next_step
                prompts = {
                    "name": "What's the client's name?",
                    "phone": "What's the client's phone number?",
                    "address": "What's the client's full address?",
                }
                await update.effective_message.reply_text(prompts[next_step])
                return

            if inv.status == "needs_client_details":
                inv.status = "pending_approval"
                cs.commit()
            del PENDING_CLIENT_DETAILS_WIZARD[user_id]
            await update.effective_message.reply_text(
                f"✅ Details saved for {inv.invoice_number}. You can now tap Approve on the draft above."
            )
        finally:
            cs.close()
        return

    if user_id in PENDING_TERMS_INPUT:
        company_id = PENDING_TERMS_INPUT.pop(user_id)
        text = update.effective_message.text
        session = get_session()
        try:
            company = session.query(Company).get(company_id)
            company.terms_and_conditions = text
            session.commit()
            await update.effective_message.reply_text(f"✅ Terms & conditions saved for {company.name}.")
        finally:
            session.close()
        return

    if user_id in PENDING_ADDRESS_INPUT:
        company_id = PENDING_ADDRESS_INPUT.pop(user_id)
        text = update.effective_message.text.strip()
        session = get_session()
        try:
            company = session.query(Company).get(company_id)
            company.address = text
            session.commit()
            await update.effective_message.reply_text(f"✅ Address saved for {company.name}.")
        finally:
            session.close()
        return

    if user_id in PENDING_UEN_INPUT:
        company_id = PENDING_UEN_INPUT.pop(user_id)
        text = update.effective_message.text.strip()
        session = get_session()
        try:
            company = session.query(Company).get(company_id)
            company.uen = text
            session.commit()
            await update.effective_message.reply_text(f"✅ UEN saved for {company.name}.")
        finally:
            session.close()
        return

    if user_id in PENDING_APPROVER_INPUT:
        company_id = PENDING_APPROVER_INPUT.pop(user_id)
        text = update.effective_message.text.strip()
        try:
            approver_id = int(text)
        except ValueError:
            await update.effective_message.reply_text("That doesn't look like a numeric Telegram ID - try again.")
            PENDING_APPROVER_INPUT[user_id] = company_id  # let them retry
            return
        session = get_session()
        try:
            company = session.query(Company).get(company_id)
            current = list(company.approver_telegram_ids or [])
            if approver_id not in current:
                current.append(approver_id)
                company.approver_telegram_ids = current
                session.commit()
            await update.effective_message.reply_text(
                f"✅ {approver_id} can now approve/reject invoices for {company.name}."
            )
        finally:
            session.close()
        return

    if user_id not in PENDING_PAYNOW_WIZARD:
        return

    state = PENDING_PAYNOW_WIZARD[user_id]
    text = update.effective_message.text.strip()

    if state["step"] == "value":
        state["proxy_value"] = text
        state["step"] = "bank"
        await update.effective_message.reply_text("What's the bank name? (or reply 'skip')")
        return

    if state["step"] == "bank":
        state["bank"] = None if text.lower() == "skip" else text
        state["step"] = "beneficiary"
        await update.effective_message.reply_text("What's the beneficiary/account holder name? (or reply 'skip')")
        return

    if state["step"] == "beneficiary":
        beneficiary = None if text.lower() == "skip" else text
        session = get_session()
        try:
            company = session.query(Company).get(state["company_id"])
            company.paynow_proxy_type = state["proxy_type"].upper()
            company.paynow_proxy_value = state["proxy_value"]
            if state.get("bank"):
                company.bank_name = state["bank"]
            if beneficiary:
                company.beneficiary_name = beneficiary
            session.commit()
            await update.effective_message.reply_text(
                f"✅ PayNow set for {company.name}:\n"
                f"{state['proxy_type'].upper()}: {state['proxy_value']}\n"
                f"Bank: {company.bank_name or '(not set)'}\n"
                f"Beneficiary: {company.beneficiary_name or '(not set)'}"
            )
        finally:
            session.close()
        del PENDING_PAYNOW_WIZARD[user_id]
        return


@require_message
async def set_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setlimit <n or 'none'> - run inside that company's group."""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /setlimit <number> or /setlimit none for unlimited")
        return

    session = get_session()
    try:
        company = get_company_by_chat(session, update.effective_chat.id)
        if not company:
            await update.effective_message.reply_text("No company linked to this chat yet - run /newcompany first.")
            return
        if context.args[0].lower() == "none":
            company.invoice_limit_per_month = None
            session.commit()
            await update.effective_message.reply_text(f"✅ {company.name} now has no invoice limit.")
        else:
            company.invoice_limit_per_month = int(context.args[0])
            session.commit()
            await update.effective_message.reply_text(f"✅ {company.name} capped at {company.invoice_limit_per_month} invoices/month.")
    finally:
        session.close()


@require_message
async def list_companies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    session = get_session()
    try:
        companies = session.query(Company).all()
        if not companies:
            await update.effective_message.reply_text("No companies yet. Run /newcompany <name> inside their group chat.")
            return
        lines = ["*Companies:*"]
        for c in companies:
            has_logo = "🖼️" if c.logo_path else "—"
            has_terms = "📄" if c.terms_and_conditions else "—"
            limit = f"{c.invoice_limit_per_month}/mo" if c.invoice_limit_per_month else "unlimited"
            lines.append(f"`{c.id}` {c.name} - logo:{has_logo} terms:{has_terms}, limit: {limit}")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")
    finally:
        session.close()


@require_message
async def set_logo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setlogo - run inside a company's group, then send their logo image next."""
    if not is_admin(update.effective_user.id):
        return
    session = get_session()
    try:
        company = get_company_by_chat(session, update.effective_chat.id)
        if not company:
            await update.effective_message.reply_text("No company linked to this chat yet - run /newcompany first.")
            return
        PENDING_LOGO_UPLOAD[update.effective_user.id] = company.id
        await update.effective_message.reply_text(
            f"🖼️ Ready for {company.name}'s logo - send the image as your next message."
        )
    finally:
        session.close()


@require_message
async def set_terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setterms <text> - run inside a company's group. Sets their terms & conditions text."""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /setterms <your terms and conditions text>\n"
            "Put each separate point on its own line if you want line breaks in the PDF."
        )
        return

    text = update.effective_message.text.split(None, 1)[1] if len(context.args) else ""
    session = get_session()
    try:
        company = get_company_by_chat(session, update.effective_chat.id)
        if not company:
            await update.effective_message.reply_text("No company linked to this chat yet - run /newcompany first.")
            return
        company.terms_and_conditions = text
        session.commit()
        await update.effective_message.reply_text(f"✅ Terms & conditions saved for {company.name}.")
    finally:
        session.close()


def settings_menu_buttons(company_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼️ Change logo", callback_data=f"settings:logo:{company_id}")],
        [InlineKeyboardButton("💰 Update PayNow", callback_data=f"settings:paynow:{company_id}")],
        [InlineKeyboardButton("📄 Update terms & conditions", callback_data=f"settings:terms:{company_id}")],
        [InlineKeyboardButton("🏠 Set company address", callback_data=f"settings:address:{company_id}")],
        [InlineKeyboardButton("🏢 Set company UEN", callback_data=f"settings:uen:{company_id}")],
        [InlineKeyboardButton("🔢 Set invoice limit", callback_data=f"settings:limit:{company_id}")],
        [InlineKeyboardButton("👥 Manage approvers", callback_data=f"settings:approvers:{company_id}")],
        [InlineKeyboardButton("ℹ️ View current settings", callback_data=f"settings:view:{company_id}")],
    ])


def approvers_menu_buttons(company) -> InlineKeyboardMarkup:
    rows = []
    for approver_id in (company.approver_telegram_ids or []):
        rows.append([InlineKeyboardButton(
            f"❌ Remove {approver_id}", callback_data=f"rmapprover:{approver_id}:{company.id}"
        )])
    rows.append([InlineKeyboardButton("➕ Add approver", callback_data=f"addapprover_btn:{company.id}")])
    return InlineKeyboardMarkup(rows)


@require_message
async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/settings - shows a tap-driven menu for this company's customization
    (logo, PayNow, terms & conditions, invoice limit) instead of needing to
    remember each command."""
    if not is_admin(update.effective_user.id):
        return
    session = get_session()
    try:
        company = get_company_by_chat(session, update.effective_chat.id)
        if not company:
            await update.effective_message.reply_text("No company linked to this chat yet - run /newcompany first.")
            return
        await update.effective_message.reply_text(
            f"⚙️ Settings for *{company.name}* - what would you like to change?",
            parse_mode="Markdown",
            reply_markup=settings_menu_buttons(company.id),
        )
    finally:
        session.close()


async def handle_settings_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes taps on the /settings menu buttons to the right guided flow."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("Only admins can change these settings.", show_alert=True)
        return

    _, action, company_id_str = query.data.split(":")
    company_id = int(company_id_str)
    user_id = query.from_user.id

    if action == "logo":
        PENDING_LOGO_UPLOAD[user_id] = company_id
        await query.edit_message_text("🖼️ Send the logo image now (as a photo or a file).")

    elif action == "paynow":
        session = get_session()
        try:
            company = session.query(Company).get(company_id)
            await query.edit_message_text(
                f"Is {company.name}'s PayNow a mobile number or a company UEN?",
            )
            await context.bot.send_message(
                chat_id=query.message.chat.id,
                text="Choose one:",
                reply_markup=paynow_type_buttons(company_id),
            )
        finally:
            session.close()

    elif action == "terms":
        PENDING_TERMS_INPUT[user_id] = company_id
        await query.edit_message_text(
            "📄 Send your terms & conditions as your next message.\n"
            "Put each point on its own line if you want line breaks in the PDF."
        )

    elif action == "limit":
        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("30/month", callback_data=f"setlimit:30:{company_id}"),
                InlineKeyboardButton("100/month", callback_data=f"setlimit:100:{company_id}"),
            ],
            [InlineKeyboardButton("Unlimited", callback_data=f"setlimit:none:{company_id}")],
        ])
        await query.edit_message_text(
            "🔢 Pick a monthly invoice cap, or use `/setlimit <number>` for a custom amount.",
            parse_mode="Markdown",
            reply_markup=buttons,
        )

    elif action == "address":
        PENDING_ADDRESS_INPUT[user_id] = company_id
        await query.edit_message_text("🏠 Send the company's address as your next message.")

    elif action == "uen":
        PENDING_UEN_INPUT[user_id] = company_id
        await query.edit_message_text("🏢 Send the company's UEN as your next message.")

    elif action == "approvers":
        session = get_session()
        try:
            company = session.query(Company).get(company_id)
            ids = company.approver_telegram_ids or []
            text = (
                f"👥 Approvers for *{company.name}*:\n" + ("\n".join(f"• {i}" for i in ids) if ids else "(none yet)")
            )
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=approvers_menu_buttons(company))
        finally:
            session.close()

    elif action == "view":
        session = get_session()
        try:
            company = session.query(Company).get(company_id)
            ids = company.approver_telegram_ids or []
            summary = (
                f"ℹ️ *Current settings for {company.name}*\n\n"
                f"PayNow: {company.paynow_proxy_type} {company.paynow_proxy_value or '(not set)'}\n"
                f"Bank: {company.bank_name or '(not set)'}\n"
                f"Beneficiary: {company.beneficiary_name or '(not set)'}\n"
                f"Address: {company.address or '(not set)'}\n"
                f"UEN: {company.uen or '(not set)'}\n"
                f"Logo: {'✅ set' if company.logo_path else '(not set)'}\n"
                f"Terms & conditions: {'✅ set' if company.terms_and_conditions else '(not set)'}\n"
                f"Monthly invoice limit: {company.invoice_limit_per_month or 'unlimited'}\n"
                f"Approvers: {', '.join(str(i) for i in ids) if ids else '(none - only global admins)'}"
            )
            await query.edit_message_text(summary, parse_mode="Markdown")
        finally:
            session.close()


async def handle_remove_approver_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles tapping '❌ Remove <id>' under Settings > Manage approvers."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("Only admins can change this.", show_alert=True)
        return

    _, approver_id_str, company_id_str = query.data.split(":")
    approver_id = int(approver_id_str)
    company_id = int(company_id_str)

    session = get_session()
    try:
        company = session.query(Company).get(company_id)
        current = list(company.approver_telegram_ids or [])
        if approver_id in current:
            current.remove(approver_id)
            company.approver_telegram_ids = current
            session.commit()
        ids = company.approver_telegram_ids or []
        text = (
            f"👥 Approvers for *{company.name}*:\n" + ("\n".join(f"• {i}" for i in ids) if ids else "(none yet)")
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=approvers_menu_buttons(company))
    finally:
        session.close()


async def handle_add_approver_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles tapping '➕ Add approver' under Settings > Manage approvers."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("Only admins can change this.", show_alert=True)
        return

    company_id = int(query.data.split(":")[1])
    PENDING_APPROVER_INPUT[query.from_user.id] = company_id
    await query.edit_message_text(
        "➕ Have them message @userinfobot to get their numeric Telegram ID, then send it here."
    )


async def handle_fill_details_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the 'Fill in missing details' button - starts a guided
    phone/address wizard instead of requiring /setdetails syntax."""
    query = update.callback_query
    await query.answer()

    inv_id = int(query.data.split(":")[1])

    reg_session = get_session()
    try:
        company = get_company_by_chat(reg_session, query.message.chat.id)
        if not company:
            await query.answer("No company linked to this chat.", show_alert=True)
            return
        if not can_approve_for(query.from_user.id, company):
            await query.answer("You're not authorized to edit invoices for this company.", show_alert=True)
            return
    finally:
        reg_session.close()

    cs = get_company_session(company)
    try:
        inv = cs.query(Invoice).get(inv_id)
        if not inv:
            await query.answer("Invoice no longer exists.", show_alert=True)
            return
        client = inv.client
        missing = missing_client_fields(client)
        first_missing = missing[0] if missing else "phone"
    finally:
        cs.close()

    PENDING_CLIENT_DETAILS_WIZARD[query.from_user.id] = {"invoice_id": inv_id, "step": first_missing}
    prompts = {
        "name": "What's the client's name?",
        "phone": "What's the client's phone number?",
        "address": "What's the client's full address?",
    }
    await context.bot.send_message(chat_id=query.message.chat.id, text=f"✏️ {prompts[first_missing]}")


async def handle_limit_preset_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the quick-preset buttons under the Settings > invoice limit menu."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("Only admins can change this.", show_alert=True)
        return

    _, value, company_id_str = query.data.split(":")
    company_id = int(company_id_str)

    session = get_session()
    try:
        company = session.query(Company).get(company_id)
        company.invoice_limit_per_month = None if value == "none" else int(value)
        session.commit()
        label = "unlimited" if value == "none" else f"{value}/month"
        await query.edit_message_text(f"✅ {company.name} set to {label}.")
    finally:
        session.close()


@require_message
async def add_approver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/addapprover <telegram_user_id> - run inside that company's group.
    Lets that person approve/reject invoices for THIS company only, without
    needing your global admin access."""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /addapprover <telegram_user_id>\n"
            "Get their numeric ID by having them message @userinfobot"
        )
        return
    try:
        approver_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("That doesn't look like a numeric Telegram ID.")
        return

    session = get_session()
    try:
        company = get_company_by_chat(session, update.effective_chat.id)
        if not company:
            await update.effective_message.reply_text("No company linked to this chat yet - run /newcompany first.")
            return
        current = list(company.approver_telegram_ids or [])
        if approver_id not in current:
            current.append(approver_id)
            company.approver_telegram_ids = current
            session.commit()
        await update.effective_message.reply_text(f"✅ {approver_id} can now approve/reject invoices for {company.name}.")
    finally:
        session.close()


@require_message
async def remove_approver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/removeapprover <telegram_user_id> - run inside that company's group."""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /removeapprover <telegram_user_id>")
        return
    try:
        approver_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("That doesn't look like a numeric Telegram ID.")
        return

    session = get_session()
    try:
        company = get_company_by_chat(session, update.effective_chat.id)
        if not company:
            await update.effective_message.reply_text("No company linked to this chat yet.")
            return
        current = list(company.approver_telegram_ids or [])
        if approver_id in current:
            current.remove(approver_id)
            company.approver_telegram_ids = current
            session.commit()
            await update.effective_message.reply_text(f"✅ Removed {approver_id} from {company.name}'s approvers.")
        else:
            await update.effective_message.reply_text("That ID wasn't in the approver list.")
    finally:
        session.close()


@require_message
async def list_approvers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/approvers - run inside a company's group."""
    session = get_session()
    try:
        company = get_company_by_chat(session, update.effective_chat.id)
        if not company:
            await update.effective_message.reply_text("No company linked to this chat.")
            return
        ids = company.approver_telegram_ids or []
        text = ", ".join(str(i) for i in ids) if ids else "(none set - only your global admins can approve here)"
        await update.effective_message.reply_text(f"Approvers for {company.name}: {text}")
    finally:
        session.close()


@require_message
async def handle_logo_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Only meaningful right after /setlogo or /newcompany - saves the image as
    that company's logo. Handles both a Telegram "photo" (compressed) and an
    image sent "as file" (uncompressed, better quality). The image is
    automatically cleaned up (trimmed, background lightened to transparent,
    centered on a consistent canvas) before saving."""
    user_id = update.effective_user.id
    if user_id not in PENDING_LOGO_UPLOAD:
        return  # not in logo-upload mode, ignore stray images

    if update.effective_message.photo:
        file_id = update.effective_message.photo[-1].file_id  # highest resolution
    elif update.effective_message.document and update.effective_message.document.mime_type in (
        "image/png", "image/jpeg", "image/jpg"
    ):
        file_id = update.effective_message.document.file_id
    else:
        return

    company_id = PENDING_LOGO_UPLOAD.pop(user_id)
    raw_path = os.path.join(TEMPLATE_DIR, f"company_{company_id}_logo_raw")
    logo_path = os.path.join(TEMPLATE_DIR, f"company_{company_id}_logo.png")

    tg_file = await context.bot.get_file(file_id)
    await tg_file.download_to_drive(raw_path)

    try:
        process_logo(raw_path, logo_path)
    except Exception as e:
        log.exception("Logo processing failed")
        await update.effective_message.reply_text(f"❌ Couldn't process that image: {e}. Try a different file.")
        return
    finally:
        if os.path.exists(raw_path):
            os.remove(raw_path)

    session = get_session()
    try:
        company = session.query(Company).get(company_id)
        company.logo_path = logo_path
        session.commit()
        await update.effective_message.reply_text(f"✅ Logo saved for {company.name}.")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Job intake
# ---------------------------------------------------------------------------

@require_message
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    reg_session = get_session()
    try:
        company = get_company_by_chat(reg_session, update.effective_chat.id)
        if not company:
            await update.effective_message.reply_text(
                "⚠️ This chat isn't linked to a company yet. An admin needs to run /newcompany here first."
            )
            return

        cs = get_company_session(company)
        try:
            if company.invoice_limit_per_month is not None:
                used = invoices_this_month(cs)
                if used >= company.invoice_limit_per_month:
                    await update.effective_message.reply_text(
                        f"🚫 {company.name} has reached its monthly limit of {company.invoice_limit_per_month} invoices. "
                        f"Blocked until next month (or an admin can raise it with /setlimit)."
                    )
                    return
        finally:
            cs.close()

        company_id, company_name = company.id, company.name
    finally:
        reg_session.close()

    await update.effective_message.reply_text(f"🎧 Got it — transcribing and processing for {company_name}...")

    voice = update.effective_message.voice or update.effective_message.audio
    tg_file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await tg_file.download_to_drive(tmp.name)
        local_path = tmp.name

    try:
        raw_transcript = transcribe_audio(local_path)
    except Exception as e:
        log.exception("Transcription failed")
        await update.effective_message.reply_text(f"❌ Transcription failed: {e}")
        return
    finally:
        os.remove(local_path)

    try:
        data = extract_invoice_data(raw_transcript)
    except Exception as e:
        log.exception("Extraction failed")
        await update.effective_message.reply_text(f"❌ Could not extract invoice details: {e}")
        return

    reg_session = get_session()
    try:
        company = reg_session.query(Company).get(company_id)
    finally:
        reg_session.close()

    cs = get_company_session(company)
    try:
        client = find_or_create_client(cs, data.get("client_name"), data.get("client_phone"), data.get("client_address"))

        inv = Invoice(
            doc_type=data.get("doc_type", "invoice"),
            invoice_number=next_invoice_number(cs, data.get("doc_type", "invoice")),
            client_id=client.id,
            items=data["items"],
            subtotal=data["subtotal"],
            total=data["subtotal"],
            raw_transcript=raw_transcript,
            english_summary=data.get("english_summary"),
            status="pending_approval",
            submitted_by=user.username or str(user.id),
        )
        cs.add(inv)
        cs.commit()

        missing = bool(missing_client_fields(client))
        if missing:
            inv.status = "needs_client_details"
            cs.commit()

        caption = draft_caption(inv, client, company)
        buttons = [[
            InlineKeyboardButton("✅ Approve & generate invoice", callback_data=f"approve:{inv.id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{inv.id}"),
        ]]
        if missing:
            buttons.append([InlineKeyboardButton("✏️ Fill in missing details", callback_data=f"filldetails:{inv.id}")])
            caption += "\n\n⚠️ *Missing client details* — tap the button below to fill them in."

        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons)
        )
        await update.effective_message.reply_text("✅ Draft ready above for admin approval.")
    finally:
        cs.close()


@require_message
async def set_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setdetails <invoice_id> phone=... address=... - run in the same chat as the draft.
    Address can contain spaces - e.g.:
    /setdetails 1 phone=+6591234567 address=683C Edgedale Plains 03-689 Singapore 823683
    """
    if len(context.args) < 2:
        await update.effective_message.reply_text("Usage: /setdetails <invoice_id> name=... phone=+65... address=full address here")
        return

    reg_session = get_session()
    try:
        company = get_company_by_chat(reg_session, update.effective_chat.id)
        if not company:
            await update.effective_message.reply_text("No company linked to this chat.")
            return
        if not can_approve_for(update.effective_user.id, company):
            await update.effective_message.reply_text("You're not authorized to edit invoices for this company.")
            return
    finally:
        reg_session.close()

    invoice_id = int(context.args[0])
    rest = " ".join(context.args[1:])
    kv = {}
    for key in ("name", "phone", "address"):
        m = re.search(rf"{key}=(.*?)(?=\s+(?:name|phone|address)=|$)", rest)
        if m and m.group(1).strip():
            kv[key] = m.group(1).strip()

    cs = get_company_session(company)
    try:
        inv = cs.query(Invoice).get(invoice_id)
        if not inv:
            await update.effective_message.reply_text("Invoice not found in this company.")
            return
        client = inv.client
        if "name" in kv:
            client.name = kv["name"]
        if "phone" in kv:
            client.phone = kv["phone"]
        if "address" in kv:
            client.address = kv["address"]
        cs.commit()

        if not missing_client_fields(client) and inv.status == "needs_client_details":
            inv.status = "pending_approval"
            cs.commit()

        await update.effective_message.reply_text(
            f"Updated client details for invoice {inv.invoice_number}.\n"
            f"Name: {client.name}\nPhone: {client.phone}\nAddress: {client.address}\n\n"
            f"You can now Approve it from the earlier message."
        )
    finally:
        cs.close()


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, inv_id_str = query.data.split(":")
    inv_id = int(inv_id_str)

    reg_session = get_session()
    try:
        company = get_company_by_chat(reg_session, query.message.chat.id)
        if not company:
            await query.edit_message_text("No company linked to this chat.")
            return
        if not can_approve_for(query.from_user.id, company):
            await query.answer("You're not authorized to approve invoices for this company.", show_alert=True)
            return
    finally:
        reg_session.close()

    cs = get_company_session(company)
    try:
        inv = cs.query(Invoice).get(inv_id)
        if not inv:
            await query.edit_message_text("Invoice no longer exists.")
            return

        if action == "reject":
            inv.status = "rejected"
            cs.commit()
            await query.edit_message_text(f"❌ {inv.invoice_number} rejected.")
            return

        client = inv.client
        if missing_client_fields(client):
            await context.bot.send_message(
                chat_id=query.message.chat.id,
                text=(
                    f"⚠️ Can't approve {inv.invoice_number} yet - still missing client details.\n\n"
                    f"Name: {'MISSING' if is_name_missing(client) else client.name}\n"
                    f"Phone: {client.phone or 'MISSING'}\n"
                    f"Address: {client.address or 'MISSING'}"
                ),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✏️ Fill in missing details", callback_data=f"filldetails:{inv.id}")
                ]]),
            )
            return

        pdf_path = os.path.join(OUTPUT_DIR, f"{company.id}_{inv.invoice_number}.pdf")

        build_invoice_pdf(
            output_path=pdf_path,
            doc_type=inv.doc_type,
            invoice_number=inv.invoice_number,
            client_name=client.name,
            client_phone=client.phone,
            client_address=client.address,
            items=inv.items,
            subtotal=inv.subtotal,
            total=inv.total,
            company_name=company.name,
            company_address=company.address or "",
            company_uen=company.uen or "",
            paynow_proxy_type=company.paynow_proxy_type,
            paynow_proxy_value=company.paynow_proxy_value or "",
            logo_path=company.logo_path,
            terms_text=company.terms_and_conditions,
        )

        inv.pdf_path = pdf_path
        inv.status = "approved"
        cs.commit()

        await query.edit_message_text(f"✅ {inv.invoice_number} approved. Generating PDF...")
        with open(pdf_path, "rb") as f:
            await context.bot.send_document(chat_id=query.message.chat.id, document=f, filename=os.path.basename(pdf_path))
    finally:
        cs.close()


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error", exc_info=context.error)
    try:
        for admin_id in (ADMIN_TELEGRAM_IDS or []):
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"⚠️ Bot hit an unexpected error:\n`{context.error}`",
                parse_mode="Markdown",
            )
    except Exception:
        pass


@require_message
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "👋 *Hi! Here's how this works:*\n\n"
        "🎙️ Send a voice note describing a job (English/Chinese/mixed is fine) "
        "and I'll draft an invoice or quotation for approval.\n\n"
        "🏢 Each company has its own group chat like this one.\n\n"
        "⚙️ Type /settings anytime to set up or change:\n"
        "🖼️ Logo · 💰 PayNow · 📄 Terms & conditions · 🔢 Invoice limit · 👥 Approvers\n\n"
        "Everything is tap-and-reply - no need to remember any typing format. "
        "If I ever need something from you (like a phone number), I'll ask for "
        "just that one thing - you reply with it, and that's it. ✅\n\n"
        "🆕 First time setting up a company? Run:\n"
        "`/newcompany Their Name Here`",
        parse_mode="Markdown",
    )


async def _register_commands(app: Application):
    """Sets the native Telegram '/' menu - shown as a tappable list with
    short descriptions when someone types '/' in the chat, instead of them
    needing to remember exact command names."""
    await app.bot.set_my_commands([
        BotCommand("start", "👋 How this bot works"),
        BotCommand("newcompany", "🆕 Link this chat to a new company"),
        BotCommand("settings", "⚙️ Logo, PayNow, terms, invoice limit"),
        BotCommand("companies", "📋 List all onboarded companies"),
        BotCommand("approvers", "👥 See who can approve here"),
    ])


def main():
    init_db()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_register_commands).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("newcompany", new_company))
    app.add_handler(CommandHandler("setpaynow", set_paynow))
    app.add_handler(CommandHandler("setlogo", set_logo))
    app.add_handler(CommandHandler("setterms", set_terms))
    app.add_handler(CommandHandler("setlimit", set_limit))
    app.add_handler(CommandHandler("companies", list_companies))
    app.add_handler(CommandHandler("setdetails", set_details))
    app.add_handler(CommandHandler("addapprover", add_approver))
    app.add_handler(CommandHandler("removeapprover", remove_approver))
    app.add_handler(CommandHandler("approvers", list_approvers))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_logo_upload))
    app.add_handler(CallbackQueryHandler(handle_paynow_wizard_button, pattern=r"^paynow_type:"))
    app.add_handler(CallbackQueryHandler(handle_settings_menu_button, pattern=r"^settings:"))
    app.add_handler(CallbackQueryHandler(handle_remove_approver_button, pattern=r"^rmapprover:"))
    app.add_handler(CallbackQueryHandler(handle_add_approver_button, pattern=r"^addapprover_btn:"))
    app.add_handler(CallbackQueryHandler(handle_fill_details_button, pattern=r"^filldetails:"))
    app.add_handler(CallbackQueryHandler(handle_limit_preset_button, pattern=r"^setlimit:"))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^(approve|reject):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_wizard_text))
    app.add_error_handler(error_handler)

    log.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
