# JS2 Invoice Bot (Multi-Company, Standard Layout)

One codebase, one Telegram bot, serving multiple client companies - each with
its own genuinely separate data (own Postgres schema), own PayNow target, own
logo, and own terms & conditions, all rendered into one reliable standard
invoice/quotation layout.

## Why one standard layout

An earlier version of this bot tried to clone each company's exact existing
PDF format automatically. In practice this was fragile - real-world
quotations have merged label+value text, wrapped multi-line addresses,
percentage-based payment milestone tables, and other quirks that an
automated layout-cloning pipeline struggled with reliably. One clean,
tested, standard layout - customized per company with their logo, PayNow QR,
and terms & conditions - is far more robust and maintainable.

## How company routing works

**Give each company its own dedicated Telegram group and add this bot to it.**
Every message from that group is automatically understood as belonging to
that company - there's no manual switching.

## Onboarding a new company

1. Create a Telegram group for them, add your bot to it.
2. Inside that group, run: `/newcompany ABC Cleaning Pte Ltd`
   This permanently links that group to the new company and creates their
   own private database schema.
3. Set their PayNow - a button-guided setup starts automatically after step 2
   (choose Mobile or UEN, then reply with the number/UEN, then optionally
   their bank name and beneficiary name). You can also relaunch this anytime
   with `/setpaynow` (no arguments).
4. `/setlogo` then send their logo image as your next message.
5. `/setterms <their terms and conditions text>` - put each point on its own
   line if you want line breaks in the PDF.
6. Optional: `/setlimit 100` to cap them at 100 invoices/month. Once hit,
   new invoices are hard-blocked until next month (or you raise the limit).
   Leave unset for unlimited.

## Day to day

- Staff sends a voice note describing a job, in that company's group
  (English/Chinese/mixed OK)
- Bot transcribes (Whisper), translates/extracts fields (Claude), looks up/
  creates the client within that company's private data
- A draft appears right there in the group with Approve/Reject buttons -
  only users in `ADMIN_TELEGRAM_IDS`, or that company's designated approvers
  (see below), can actually tap them
- On Approve, a PDF is generated in the standard layout with their logo,
  their PayNow QR, and their terms & conditions

## Letting a company approve its own invoices

By default, only you and Jo (`ADMIN_TELEGRAM_IDS`) can approve/reject any
company's invoices. To let a client's own team self-approve without you in
the loop:
- `/addapprover <their Telegram numeric ID>` - run inside their group
- `/removeapprover <telegram_id>` - revoke it
- `/approvers` - see who's currently authorized in this chat

Get someone's numeric Telegram ID by having them message @userinfobot.

## Admin commands

Run inside a specific company's group:
- `/newcompany <name>` - link this chat to a new company
- `/setpaynow` - button-guided PayNow setup (or `/setpaynow <mobile|uen> <value> [bank=... beneficiary=...]` directly)
- `/setlogo` - then send their logo image
- `/setterms <text>` - their terms & conditions
- `/setlimit <n>` or `/setlimit none` - cap or uncap monthly invoices
- `/addapprover` / `/removeapprover` / `/approvers` - manage who can approve here
- `/setdetails <invoice_id> phone=... address=...` - fill in missing client details

Run anywhere:
- `/companies` - see every onboarded company, their logo/terms status, and invoice limits

## 1. Create the Telegram bot

1. Open Telegram, message **@BotFather**
2. `/newbot`, follow the prompts, copy the token it gives you (this is `TELEGRAM_BOT_TOKEN`)

## 2. Get your Telegram numeric user IDs (for admin allow-list)

Message **@userinfobot** on Telegram - it replies with your numeric ID.
Do this for yourself and Jo. These IDs go in `ADMIN_TELEGRAM_IDS` and apply
across every company's group. Individual companies' own approvers are set
per-company via `/addapprover` instead (see above).

## 3. Get API keys

- **OpenAI** (for transcription): https://platform.openai.com/api-keys
- **Anthropic** (for translation/extraction): https://console.anthropic.com/settings/keys

## 4. Deploy (Railway)

1. Push this folder to a GitHub repo (`Dockerfile`, `bot.py` etc. at repo root,
   or set the service's Root Directory accordingly).
2. Go to https://railway.app -> New Project -> Deploy from GitHub repo.
3. Add a **Postgres** plugin to the project.
4. In the service's Variables tab, add:
   ```
   TELEGRAM_BOT_TOKEN=...
   ADMIN_TELEGRAM_IDS=your_id,jos_id
   OPENAI_API_KEY=...
   ANTHROPIC_API_KEY=...
   CLAUDE_MODEL=claude-sonnet-5
   ```
   Then add `DATABASE_URL` as a **reference** to the Postgres plugin
   (`${{Postgres.DATABASE_URL}}`), not a manually typed value.
5. Deploy, then check Deploy Logs for `Bot starting...` with no errors.

## 5. Onboard your first company and test

1. Create a Telegram group, add your bot
2. `/newcompany JS2 Cleaning`
3. Follow the PayNow buttons
4. `/setlogo` then send their logo
5. `/setterms <your terms>`
6. Send a voice note describing a job
7. If details are missing, `/setdetails <invoice_id> phone=... address=...`
8. Tap **Approve** - you should get back a PDF with their logo, a scannable
   PayNow QR, and their terms & conditions

Repeat with a new group + `/newcompany` for each additional company.

## Notes / next steps you may want later

- **Full field editing before approval**: currently editing is limited to
  `/setdetails` for phone/address.
- **Sending directly to the client**: right now the approved PDF goes to the
  company's group. Sending straight to the client would need either their
  Telegram, or email/WhatsApp integration.
- **Discounts / payment milestones**: the standard layout doesn't currently
  support a discount line or percentage-based payment milestone breakdowns -
  if a company needs these, they'd be a good next feature to add.
- **Fuzzy client matching**: matching is currently exact-phone or
  case-insensitive exact-name.

