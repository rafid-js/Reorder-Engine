# Winterfell Reorder Engine

AI-driven inventory reorder system for Winterfell — a fast-fashion e-commerce brand in Bangladesh.

Pulls real-time sales and stock data from **WooCommerce**, **Nuport**, and **Zoho Books**,
runs it through **Claude AI** for intelligent recommendations, then delivers actionable alerts
via **Google Sheets**, **Gmail**, and **WhatsApp**.

---

## Architecture

```
run_reorder.py          ← Entry point + APScheduler (10 AM BST daily)
config.py               ← .env loading, constants, logging

data/
  woocommerce.py        ← Pull orders (last 30d) via WC REST API
  nuport.py             ← Pull deliveries + stock levels from Nuport
  zoho.py               ← Pull purchase orders from Zoho Books
  merger.py             ← Merge all 3 sources by SKU

engine/
  velocity.py           ← Sales velocity, days_remaining, urgency tiers
  reorder.py            ← Reorder qty formula (with MOQ rounding)
  intelligence.py       ← Claude API integration (claude-sonnet-4-20250514)

outputs/
  sheets.py             ← Google Sheets "Reorder Queue" (overwrite + color)
  email.py              ← Gmail HTML briefing
  whatsapp.py           ← Twilio WhatsApp alert

logs/
  reorder.log           ← Rotating log file (5 MB × 7 backups)
```

---

## Quick Start

### 1. Clone and install dependencies

```bash
git clone https://github.com/rafid-js/reorder-engine.git
cd reorder-engine
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.template .env
# Edit .env with your actual credentials
```

See the [Credentials Setup](#credentials-setup) section for each service.

### 3. Run manually

```bash
python run_reorder.py --now
```

### 4. Start the scheduler (10 AM BST daily)

```bash
python run_reorder.py
```

---

## Credentials Setup

### WooCommerce

1. Go to **WooCommerce → Settings → Advanced → REST API**
2. Click **Add Key** — set permissions to **Read**
3. Copy `Consumer Key` → `WC_CONSUMER_KEY`
4. Copy `Consumer Secret` → `WC_CONSUMER_SECRET`
5. Set `WC_URL` to your store URL (e.g. `https://shop.winterfell.com.bd`)

### Nuport

1. Log in to your Nuport dashboard
2. Go to **Settings → API Keys** and generate a key
3. Set `NUPORT_API_KEY` and `NUPORT_BASE_URL`

### Zoho Books

1. Go to [Zoho API Console](https://api-console.zoho.com/)
2. Create a **Server-based Application**
3. Set the redirect URI to `https://www.zoho.com/books`
4. Grant scopes: `ZohoBooks.purchaseorders.READ`
5. Generate a **Refresh Token** via the OAuth2 flow:
   ```
   https://accounts.zoho.com/oauth/v2/auth?
     scope=ZohoBooks.purchaseorders.READ&
     client_id=YOUR_CLIENT_ID&
     response_type=code&
     redirect_uri=https://www.zoho.com/books&
     access_type=offline
   ```
6. Exchange the authorization code for a refresh token:
   ```bash
   curl -X POST "https://accounts.zoho.com/oauth/v2/token" \
     -d "code=YOUR_CODE&client_id=YOUR_ID&client_secret=YOUR_SECRET&\
         redirect_uri=https://www.zoho.com/books&grant_type=authorization_code"
   ```
7. Find your **Org ID** at: `https://www.zohoapis.com/books/v3/organizations`

### Anthropic (Claude API)

1. Go to [console.anthropic.com](https://console.anthropic.com/)
2. Create an API key under **API Keys**
3. Set `ANTHROPIC_API_KEY=sk-ant-...`

### Google Sheets + Gmail (Service Account)

Both Google Sheets writing and Gmail sending use the **same Google Service Account**.
Gmail requires **Domain-Wide Delegation** to send as your sender address.

#### Step 1 — Create a Service Account

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select existing)
3. Enable these APIs:
   - **Google Sheets API**
   - **Google Drive API**
   - **Gmail API**
4. Go to **IAM & Admin → Service Accounts → Create Service Account**
5. Name it (e.g. `winterfell-reorder-engine`)
6. Click **Create and Continue** (no role needed at project level)
7. Click **Done**

#### Step 2 — Download the JSON key

1. Click the service account → **Keys** tab → **Add Key → Create new key → JSON**
2. Save the downloaded file as `service_account.json` in the project root
3. Set `GOOGLE_SERVICE_ACCOUNT_JSON=service_account.json` in `.env`

#### Step 3 — Share the Google Sheet

1. Open your Google Sheet
2. Copy the Sheet ID from the URL:
   `https://docs.google.com/spreadsheets/d/SHEET_ID_HERE/edit`
3. Set `GOOGLE_SHEETS_ID=SHEET_ID_HERE` in `.env`
4. Share the sheet with the service account email (found in the JSON file under `client_email`):
   - Click **Share** → paste the `client_email` → set **Editor**

#### Step 4 — Enable Domain-Wide Delegation for Gmail

> This step requires **Google Workspace** admin access.

1. In Google Cloud Console → Service Account → **Edit**
2. Enable **Domain-wide Delegation** → note the **Client ID**
3. Go to [Google Workspace Admin](https://admin.google.com/) →
   **Security → API Controls → Domain-wide Delegation → Add new**
4. Enter the **Client ID** and these scopes:
   ```
   https://www.googleapis.com/auth/gmail.send
   ```
5. Set `GMAIL_SENDER` to the Google Workspace email you want to send from

### WhatsApp via Twilio

1. Sign up at [twilio.com](https://www.twilio.com/)
2. Go to **Messaging → Try it out → Send a WhatsApp message**
3. Follow the sandbox setup (for testing) or apply for a WhatsApp Business number
4. Copy:
   - `TWILIO_ACCOUNT_SID` from the Twilio Console dashboard
   - `TWILIO_AUTH_TOKEN` from the Twilio Console dashboard
   - `TWILIO_WHATSAPP_FROM` = `whatsapp:+14155238886` (sandbox) or your approved number
5. Set `WHATSAPP_NUMBER` to the recipient's number in E.164 format: `+8801XXXXXXXXX`

---

## Business Logic Reference

| Parameter | Value | Meaning |
|---|---|---|
| Lookback window | 30 days | How far back orders/deliveries are pulled |
| Velocity long window | 14 days | Primary velocity calculation |
| Velocity short window | 7 days | Trend detection (vs 14d) |
| Reorder trigger | ≤ 20 days remaining | When an alert is raised |
| Reorder buffer | 1.3× | Aggressive safety multiplier |
| Reorder horizon | 30 days | Target supply days per order |
| 🔴 CRITICAL | ≤ 7 days remaining | Immediate action |
| 🟡 WARNING | 8–20 days remaining | Order this week |
| 🟢 HEALTHY | > 20 days remaining | No action needed |

**True Demand** = Total Ordered − Total Delivered (real unfulfilled demand)

**Reorder Qty** = `ceil((velocity_14d × 30d × 1.3) − current_stock)`, rounded up to MOQ

---

## Resilience

- Every API call retries up to 3× with exponential backoff (2s, 4s, 8s)
- If any single data source fails, the run continues with available data and logs a warning
- If Claude API fails, raw-data reports are still sent without AI recommendations
- If the entire pipeline crashes, a WhatsApp error alert is sent automatically
- All events logged to `logs/reorder.log` with 5 MB rotating + 7-day retention

---

## .env Reference

```dotenv
WC_URL=https://yourstore.com
WC_CONSUMER_KEY=ck_...
WC_CONSUMER_SECRET=cs_...

NUPORT_API_KEY=...
NUPORT_BASE_URL=https://api.nuport.com

ZOHO_CLIENT_ID=...
ZOHO_CLIENT_SECRET=...
ZOHO_REFRESH_TOKEN=...
ZOHO_ORG_ID=...

ANTHROPIC_API_KEY=sk-ant-...

GOOGLE_SHEETS_ID=...
GOOGLE_SERVICE_ACCOUNT_JSON=service_account.json

GMAIL_SENDER=ops@yourdomain.com
REORDER_EMAIL=purchasing@yourdomain.com

WHATSAPP_NUMBER=+8801XXXXXXXXX
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
```

---

## Extending the System

- **Meta Ads integration**: Set `meta_ads_high=True` per SKU in `engine/intelligence.py` when a SKU has active high-spend campaigns. The Claude prompt already handles this field.
- **Multiple suppliers**: Zoho Books purchase orders with multiple vendors per SKU are automatically surfaced in Claude's `supplier_note`.
- **MOQ per supplier**: Update `moq` in Zoho Books purchase orders; the reorder formula rounds up automatically.
- **Custom alert channels**: Add a new file under `outputs/` and wire it into `run_reorder.py`.
