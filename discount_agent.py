"""
╔══════════════════════════════════════════════════════════════╗
║        Automated Discount Notification Agent                 ║
║        Built with Gradio + BeautifulSoup + SMTP              ║
╚══════════════════════════════════════════════════════════════╝
"""

import gradio as gr
import requests
from bs4 import BeautifulSoup
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import json
import threading
import time
import re
from datetime import datetime
from pathlib import Path

# ─────────────────────────────────────────────────────────────
# DATA STORE  (in-memory + JSON file for persistence)
# ─────────────────────────────────────────────────────────────
DATA_FILE = Path("watchlist.json")

def load_data():
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"products": [], "email_cfg": {}, "log": []}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

state = load_data()
monitor_thread = None
monitor_running = False
log_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────
def add_log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    icons = {"INFO": "ℹ️", "SUCCESS": "✅", "WARNING": "⚠️", "ERROR": "❌", "AGENT": "🤖"}
    icon = icons.get(level, "•")
    entry = f"[{ts}] {icon} {msg}"
    with log_lock:
        state["log"].insert(0, entry)
        state["log"] = state["log"][:120]   # keep last 120
    return entry

# ─────────────────────────────────────────────────────────────
# SCRAPER — supports Amazon, Flipkart, generic pages
# ─────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def parse_price(text):
    """Extract numeric price from any string."""
    if not text:
        return None
    text = text.replace(",", "").replace("\u20b9", "").replace("Rs.", "").replace("INR", "")
    match = re.search(r"[\d]+(?:\.\d{1,2})?", text)
    if match:
        return float(match.group())
    return None

def scrape_price(url):
    """Scrape current price from a product URL."""
    add_log(f"Scraping: {url[:60]}...", "AGENT")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        resp.raise_for_status()
    except Exception as e:
        add_log(f"Request failed: {e}", "ERROR")
        return None, "Request failed"

    soup = BeautifulSoup(resp.text, "lxml")

    # ── Amazon ──────────────────────────────────────────────
    if "amazon" in url:
        selectors = [
            "#priceblock_ourprice", "#priceblock_dealprice",
            ".a-price-whole", "#apex_desktop .a-price .a-offscreen",
            ".priceToPay .a-price-whole", "#price_inside_buybox",
            "#corePrice_feature_div .a-price .a-offscreen",
        ]
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                price = parse_price(el.get_text())
                if price:
                    add_log(f"Amazon price found: ₹{price:,.0f}", "SUCCESS")
                    return price, "OK"

        # Try meta og:price
        meta = soup.find("meta", {"name": "twitter:data1"})
        if meta and meta.get("content"):
            price = parse_price(meta["content"])
            if price:
                return price, "OK"

    # ── Flipkart ────────────────────────────────────────────
    elif "flipkart" in url:
        selectors = [
            "._30jeq3._16Jk6d", "._30jeq3", ".CEmiEU ._30jeq3",
            "[class*='_30jeq3']", "._16Jk6d",
        ]
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                price = parse_price(el.get_text())
                if price:
                    add_log(f"Flipkart price found: ₹{price:,.0f}", "SUCCESS")
                    return price, "OK"

    # ── Meesho ──────────────────────────────────────────────
    elif "meesho" in url:
        el = soup.select_one("h4[class*='sc-']")
        if el:
            price = parse_price(el.get_text())
            if price:
                return price, "OK"

    # ── Generic fallback: look for price patterns ───────────
    # Search common price class names
    generic_selectors = [
        "[class*='price']", "[class*='Price']", "[class*='cost']",
        "[id*='price']", "[id*='Price']", "span.amount", ".product-price",
    ]
    for sel in generic_selectors:
        for el in soup.select(sel)[:5]:
            text = el.get_text(strip=True)
            price = parse_price(text)
            if price and 1 < price < 10_000_000:
                add_log(f"Generic price found: ₹{price:,.0f}", "SUCCESS")
                return price, "OK"

    # Last resort: scan all text for Rs. / ₹ patterns
    full_text = soup.get_text()
    matches = re.findall(r"(?:₹|Rs\.?)\s*([\d,]+(?:\.\d{1,2})?)", full_text)
    if matches:
        prices = [float(m.replace(",", "")) for m in matches]
        prices = [p for p in prices if 1 < p < 10_000_000]
        if prices:
            price = min(prices)   # take the smallest = likely sale price
            add_log(f"Fallback price: ₹{price:,.0f}", "WARNING")
            return price, "Fallback"

    add_log("Could not find price on page", "ERROR")
    return None, "Price not found"

# ─────────────────────────────────────────────────────────────
# EMAIL NOTIFICATION
# ─────────────────────────────────────────────────────────────
def send_email(cfg, product, current_price, original_price, discount_pct):
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🔥 Discount Alert: {product['name']} — ₹{current_price:,.0f} ({discount_pct:.0f}% OFF)"
        msg["From"]    = cfg["sender"]
        msg["To"]      = cfg["recipient"]

        html = f"""
        <html><body style="font-family:Arial,sans-serif;background:#f5f5f5;padding:20px">
        <div style="max-width:520px;margin:0 auto;background:#fff;border-radius:10px;
                    box-shadow:0 2px 10px rgba(0,0,0,.1);overflow:hidden">
          <div style="background:#1a237e;color:#fff;padding:24px 28px">
            <h2 style="margin:0;font-size:1.3rem">🤖 Discount Notification Agent</h2>
            <p style="margin:6px 0 0;opacity:.8;font-size:.9rem">Automated Price Alert</p>
          </div>
          <div style="padding:28px">
            <h3 style="color:#1a237e;margin-top:0">{product['name']}</h3>
            <table style="width:100%;border-collapse:collapse;margin:14px 0">
              <tr style="background:#f9f9f9">
                <td style="padding:10px;font-weight:bold;color:#555">Original Price</td>
                <td style="padding:10px;text-decoration:line-through;color:#999">₹{original_price:,.0f}</td>
              </tr>
              <tr style="background:#e8f5e9">
                <td style="padding:10px;font-weight:bold;color:#2e7d32">Current Price</td>
                <td style="padding:10px;font-size:1.3rem;font-weight:bold;color:#2e7d32">₹{current_price:,.0f}</td>
              </tr>
              <tr style="background:#fff8e1">
                <td style="padding:10px;font-weight:bold;color:#e65100">You Save</td>
                <td style="padding:10px;font-size:1.1rem;font-weight:bold;color:#e65100">
                  ₹{(original_price - current_price):,.0f} ({discount_pct:.0f}% OFF)
                </td>
              </tr>
            </table>
            <a href="{product['url']}"
               style="display:block;text-align:center;background:#f9a825;color:#000;
                      padding:14px;border-radius:8px;text-decoration:none;font-weight:bold;
                      font-size:1rem;margin-top:18px">
              🛒 Buy Now
            </a>
            <p style="font-size:.8rem;color:#999;margin-top:20px;text-align:center">
              Sent by Automated Discount Notification Agent
            </p>
          </div>
        </div>
        </body></html>
        """
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(cfg["sender"], cfg["app_password"])
            server.send_message(msg)

        add_log(f"Email sent for {product['name']}", "SUCCESS")
        return True
    except Exception as e:
        add_log(f"Email failed: {e}", "ERROR")
        return False

# ─────────────────────────────────────────────────────────────
# AGENT CORE — check one product
# ─────────────────────────────────────────────────────────────
def check_product(product, email_cfg=None, send_notification=True):
    name = product["name"]
    url  = product["url"]
    target_price = float(product.get("target_price", 0))
    threshold_pct = float(product.get("threshold_pct", 10))

    add_log(f"--- Checking: {name} ---", "AGENT")
    current_price, status = scrape_price(url)

    if current_price is None:
        product["status"] = "❌ Scrape Failed"
        product["last_checked"] = datetime.now().strftime("%H:%M:%S")
        return product

    # Store price history
    if "price_history" not in product:
        product["price_history"] = []
    product["price_history"].append({
        "price": current_price,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M")
    })
    product["price_history"] = product["price_history"][-30:]  # keep last 30
    product["current_price"] = current_price
    product["last_checked"] = datetime.now().strftime("%H:%M:%S")

    # Determine original / baseline price
    original_price = product.get("original_price") or current_price
    if not product.get("original_price"):
        product["original_price"] = current_price

    # Calculate discount
    if original_price > 0:
        discount_pct = ((original_price - current_price) / original_price) * 100
    else:
        discount_pct = 0

    product["discount_pct"] = round(discount_pct, 1)

    # ── Agent Decision Logic ──────────────────────────────
    alert_conditions = []

    if target_price > 0 and current_price <= target_price:
        alert_conditions.append(f"price ₹{current_price:,.0f} ≤ target ₹{target_price:,.0f}")

    if discount_pct >= threshold_pct:
        alert_conditions.append(f"{discount_pct:.1f}% discount ≥ threshold {threshold_pct:.0f}%")

    if alert_conditions:
        reason = " | ".join(alert_conditions)
        add_log(f"DISCOUNT DETECTED for {name}: {reason}", "SUCCESS")
        product["status"] = f"🔥 {discount_pct:.0f}% OFF — ₹{current_price:,.0f}"
        product["alert_count"] = product.get("alert_count", 0) + 1

        if send_notification and email_cfg and email_cfg.get("sender"):
            if not product.get("notified_at_price") or product["notified_at_price"] != current_price:
                send_email(email_cfg, product, current_price, original_price, discount_pct)
                product["notified_at_price"] = current_price
    else:
        add_log(
            f"No alert for {name}: ₹{current_price:,.0f} "
            f"(discount {discount_pct:.1f}% < threshold {threshold_pct:.0f}%)", "INFO"
        )
        product["status"] = f"✅ ₹{current_price:,.0f} ({discount_pct:.1f}% off)"

    return product

# ─────────────────────────────────────────────────────────────
# BACKGROUND MONITOR THREAD
# ─────────────────────────────────────────────────────────────
def monitor_loop(interval_mins):
    global monitor_running
    add_log(f"Monitor started — checking every {interval_mins} min", "INFO")
    while monitor_running:
        for i, product in enumerate(state["products"]):
            if not monitor_running:
                break
            state["products"][i] = check_product(
                product, state.get("email_cfg", {}), send_notification=True
            )
            save_data(state)
            time.sleep(2)
        if monitor_running:
            add_log(f"Cycle complete. Next check in {interval_mins} min.", "INFO")
            time.sleep(interval_mins * 60)
    add_log("Monitor stopped.", "INFO")

# ─────────────────────────────────────────────────────────────
# GRADIO ACTIONS
# ─────────────────────────────────────────────────────────────
def add_product(name, url, target_price, threshold_pct, original_price):
    url = url.strip()
    name = name.strip()

    if not url or not name:
        return get_watchlist_df(), "⚠️ Please enter both product name and URL."

    # Check duplicate
    for p in state["products"]:
        if p["url"] == url:
            return get_watchlist_df(), f"⚠️ '{name}' is already in watchlist."

    product = {
        "name": name,
        "url": url,
        "target_price": float(target_price) if target_price else 0,
        "threshold_pct": float(threshold_pct) if threshold_pct else 10,
        "original_price": float(original_price) if original_price else 0,
        "current_price": None,
        "status": "⏳ Not checked yet",
        "last_checked": "—",
        "discount_pct": 0,
        "price_history": [],
        "alert_count": 0,
    }
    state["products"].append(product)
    save_data(state)
    add_log(f"Added: {name}", "INFO")
    return get_watchlist_df(), f"✅ Added '{name}' to watchlist."

def remove_product(index):
    try:
        idx = int(index)
        if 0 <= idx < len(state["products"]):
            name = state["products"][idx]["name"]
            state["products"].pop(idx)
            save_data(state)
            add_log(f"Removed: {name}", "INFO")
            return get_watchlist_df(), f"🗑️ Removed '{name}'."
        return get_watchlist_df(), "⚠️ Invalid index."
    except Exception as e:
        return get_watchlist_df(), f"❌ Error: {e}"

def get_watchlist_df():
    if not state["products"]:
        return [["—", "—", "—", "—", "—", "—"]]
    rows = []
    for i, p in enumerate(state["products"]):
        cp = f"₹{p['current_price']:,.0f}" if p.get("current_price") else "—"
        op = f"₹{p['original_price']:,.0f}" if p.get("original_price") else "—"
        tp = f"₹{p['target_price']:,.0f}" if p.get("target_price") else "—"
        rows.append([
            i,
            p["name"],
            cp,
            op,
            tp,
            p.get("status", "—"),
        ])
    return rows

def check_all_now():
    if not state["products"]:
        return get_watchlist_df(), get_log_text(), "⚠️ Watchlist is empty."
    for i, product in enumerate(state["products"]):
        state["products"][i] = check_product(
            product, state.get("email_cfg", {}), send_notification=True
        )
        time.sleep(1)
    save_data(state)
    return get_watchlist_df(), get_log_text(), "✅ All products checked!"

def check_one_now(index):
    try:
        idx = int(index)
        if 0 <= idx < len(state["products"]):
            state["products"][idx] = check_product(
                state["products"][idx], state.get("email_cfg", {}), send_notification=True
            )
            save_data(state)
            return get_watchlist_df(), get_log_text(), f"✅ Checked #{idx}"
        return get_watchlist_df(), get_log_text(), "⚠️ Invalid index."
    except Exception as e:
        return get_watchlist_df(), get_log_text(), f"❌ Error: {e}"

def save_email_cfg(sender, app_password, recipient):
    state["email_cfg"] = {
        "sender": sender.strip(),
        "app_password": app_password.strip(),
        "recipient": recipient.strip(),
    }
    save_data(state)
    add_log(f"Email config saved (from: {sender})", "INFO")
    return "✅ Email settings saved!"

def test_email(sender, app_password, recipient):
    cfg = {"sender": sender.strip(), "app_password": app_password.strip(), "recipient": recipient.strip()}
    dummy_product = {
        "name": "Test Product",
        "url": "https://example.com",
        "original_price": 5000,
    }
    success = send_email(cfg, dummy_product, 4000.0, 5000.0, 20.0)
    return ("✅ Test email sent! Check your inbox." if success else "❌ Email failed — check credentials."), get_log_text()

def start_monitor(interval_mins):
    global monitor_thread, monitor_running
    if monitor_running:
        return "⚠️ Monitor is already running!", get_log_text()
    if not state["products"]:
        return "⚠️ Watchlist is empty!", get_log_text()
    monitor_running = True
    monitor_thread = threading.Thread(
        target=monitor_loop, args=(float(interval_mins),), daemon=True
    )
    monitor_thread.start()
    return f"✅ Monitor started! Checking every {interval_mins} min.", get_log_text()

def stop_monitor():
    global monitor_running
    monitor_running = False
    return "🛑 Monitor stopped.", get_log_text()

def get_log_text():
    with log_lock:
        logs = list(state["log"])
    return "\n".join(logs) if logs else "No activity yet."

def get_stats():
    total = len(state["products"])
    alerts = sum(p.get("alert_count", 0) for p in state["products"])
    discounted = sum(1 for p in state["products"] if p.get("discount_pct", 0) > 0)
    status = "🟢 Running" if monitor_running else "🔴 Stopped"
    return (
        f"**Total Products:** {total}  |  "
        f"**Discounts Detected:** {discounted}  |  "
        f"**Total Alerts Sent:** {alerts}  |  "
        f"**Monitor:** {status}"
    )

def refresh_all():
    return get_watchlist_df(), get_log_text(), get_stats()

# ─────────────────────────────────────────────────────────────
# DEMO DATA (pre-filled examples for quick testing)
# ─────────────────────────────────────────────────────────────
DEMO_PRODUCTS = [
    {
        "name": "Demo: iPhone 15 (Flipkart)",
        "url": "https://www.flipkart.com/apple-iphone-15/p/itm6ac6485515ae4",
        "target_price": 65000,
        "threshold_pct": 5,
        "original_price": 79900,
        "current_price": None,
        "status": "⏳ Not checked yet",
        "last_checked": "—",
        "discount_pct": 0,
        "price_history": [],
        "alert_count": 0,
    },
    {
        "name": "Demo: Samsung Galaxy S24 (Amazon)",
        "url": "https://www.amazon.in/Samsung-Galaxy-S24-Smartphone-Display/dp/B0CS4GNZ7S",
        "target_price": 55000,
        "threshold_pct": 10,
        "original_price": 74999,
        "current_price": None,
        "status": "⏳ Not checked yet",
        "last_checked": "—",
        "discount_pct": 0,
        "price_history": [],
        "alert_count": 0,
    },
]

if not state["products"]:
    state["products"] = DEMO_PRODUCTS
    save_data(state)
    add_log("Loaded demo products. Add your own or click Check All!", "INFO")

# ─────────────────────────────────────────────────────────────
# GRADIO UI
# ─────────────────────────────────────────────────────────────
CSS = """
#title-bar { background: linear-gradient(135deg, #1a237e, #3949ab);
             padding: 20px 30px; border-radius: 12px; margin-bottom: 4px; }
#title-bar h1 { color: #f9a825; margin: 0; font-size: 1.6rem; }
#title-bar p  { color: rgba(255,255,255,.8); margin: 4px 0 0; font-size:.9rem; }
.stats-bar { background: #e8eaf6; border-radius: 8px; padding: 10px 16px;
             font-size: .95rem; border-left: 4px solid #3949ab; }
.log-box { font-family: 'Courier New', monospace; font-size: .82rem;
           background: #1e1e2e; color: #cdd6f4; border-radius: 8px; }
.alert-row { background: #fff3e0 !important; }
.gr-button-primary { background: #1a237e !important; }
"""

with gr.Blocks(title="Automated Discount Notification Agent") as app:

    # ── Header ────────────────────────────────────────────
    gr.HTML("""
    <div id="title-bar">
      <h1>🤖 Automated Discount Notification Agent</h1>
      <p>Autonomously monitors e-commerce prices &amp; sends email alerts when discounts are detected</p>
    </div>
    """)

    stats_md = gr.Markdown(get_stats(), elem_classes="stats-bar")

    with gr.Tabs():

        # ══════════════════════════════════════════════════
        # TAB 1 — WATCHLIST
        # ══════════════════════════════════════════════════
        with gr.Tab("📋 Watchlist"):
            gr.Markdown("### 🛒 Your Product Watchlist")

            watchlist_table = gr.Dataframe(
                headers=["#", "Product Name", "Current Price", "Original Price", "Target Price", "Status"],
                value=get_watchlist_df(),
                interactive=False,
                wrap=True,
            )

            with gr.Row():
                check_all_btn  = gr.Button("🔍 Check All Prices Now", variant="primary", scale=2)
                refresh_btn    = gr.Button("🔄 Refresh Table", scale=1)

            status_box = gr.Textbox(label="Status", interactive=False, lines=1)

            gr.Markdown("---")
            gr.Markdown("#### ➕ Add New Product")

            with gr.Row():
                add_name      = gr.Textbox(label="Product Name", placeholder="e.g. Samsung Galaxy S24", scale=2)
                add_url       = gr.Textbox(label="Product URL", placeholder="https://www.flipkart.com/...", scale=3)

            with gr.Row():
                add_orig      = gr.Number(label="Original / MRP Price (₹)", value=0, minimum=0)
                add_target    = gr.Number(label="Target Price (₹)  [alert when ≤ this]", value=0, minimum=0)
                add_threshold = gr.Slider(label="Discount Threshold (%)", minimum=1, maximum=90, value=10, step=1)

            add_btn = gr.Button("➕ Add to Watchlist", variant="primary")

            gr.Markdown("---")
            gr.Markdown("#### 🗑️ Remove Product")
            with gr.Row():
                remove_idx = gr.Number(label="Product # to Remove", value=0, minimum=0, precision=0)
                remove_btn = gr.Button("🗑️ Remove", variant="stop")

        # ══════════════════════════════════════════════════
        # TAB 2 — MONITOR
        # ══════════════════════════════════════════════════
        with gr.Tab("⏱️ Auto Monitor"):
            gr.Markdown("### ⚙️ Background Price Monitor")
            gr.Markdown(
                "> The agent runs in the background, checks all products at regular intervals, "
                "and sends email alerts automatically when discounts are detected."
            )

            with gr.Row():
                interval_slider = gr.Slider(
                    label="Check Interval (minutes)", minimum=1, maximum=120, value=30, step=1
                )

            with gr.Row():
                start_btn = gr.Button("▶️ Start Monitor", variant="primary", scale=2)
                stop_btn  = gr.Button("⏹️ Stop Monitor",  variant="stop",    scale=1)

            monitor_status = gr.Textbox(label="Monitor Status", interactive=False, lines=1)

            gr.Markdown("---")
            gr.Markdown("#### 🔍 Check Single Product")
            with gr.Row():
                single_idx = gr.Number(label="Product # to Check", value=0, minimum=0, precision=0)
                single_btn = gr.Button("🔍 Check This Product", variant="secondary")

        # ══════════════════════════════════════════════════
        # TAB 3 — EMAIL SETTINGS
        # ══════════════════════════════════════════════════
        with gr.Tab("📧 Email Settings"):
            gr.Markdown("### 📬 Configure Email Notifications")
            gr.Markdown(
                "> **Gmail Users:** Go to *Google Account → Security → 2-Step Verification → App Passwords* "
                "and generate an App Password. Use that here (not your regular Gmail password)."
            )

            email_sender    = gr.Textbox(
                label="Your Gmail Address",
                placeholder="yourname@gmail.com",
                value=state.get("email_cfg", {}).get("sender", ""),
            )
            email_password  = gr.Textbox(
                label="Gmail App Password",
                placeholder="xxxx xxxx xxxx xxxx",
                type="password",
                value=state.get("email_cfg", {}).get("app_password", ""),
            )
            email_recipient = gr.Textbox(
                label="Send Alerts To (email address)",
                placeholder="alerts@example.com",
                value=state.get("email_cfg", {}).get("recipient", ""),
            )

            with gr.Row():
                save_email_btn = gr.Button("💾 Save Settings", variant="primary")
                test_email_btn = gr.Button("✉️ Send Test Email", variant="secondary")

            email_status = gr.Textbox(label="Status", interactive=False, lines=1)

        # ══════════════════════════════════════════════════
        # TAB 4 — AGENT LOG
        # ══════════════════════════════════════════════════
        with gr.Tab("📜 Agent Log"):
            gr.Markdown("### 🤖 Live Agent Activity Log")
            gr.Markdown(
                "> Tracks every action the agent takes: scraping, price comparisons, "
                "discount decisions, notifications sent."
            )

            log_refresh_btn = gr.Button("🔄 Refresh Log", variant="secondary")
            log_box = gr.Textbox(
                label="",
                value=get_log_text(),
                lines=28,
                max_lines=28,
                interactive=False,
                elem_classes="log-box",
            )

        # ══════════════════════════════════════════════════
        # TAB 5 — HOW IT WORKS
        # ══════════════════════════════════════════════════
        with gr.Tab("ℹ️ How It Works"):
            gr.Markdown("""
### 🏗️ System Architecture

```
User Config (URL + Target Price + Threshold)
         │
         ▼
 ┌──────────────────────────────────────┐
 │        LangGraph-Style Agent         │
 │  ┌──────────┐    ┌───────────────┐   │
 │  │ Scraper  │───►│ Price Analyzer│   │
 │  │  Node    │    │     Node      │   │
 │  └──────────┘    └──────┬────────┘   │
 │                         │            │
 │              ┌──────────▼─────────┐  │
 │              │   Decision Node    │  │
 │              │ (Threshold Check)  │  │
 │              └──────┬──────┬──────┘  │
 │                     │      │         │
 │              Discount?   No Discount │
 │                  │           │       │
 │          ┌───────▼───┐  ┌───▼────┐  │
 │          │  Notify   │  │  Sleep │  │
 │          │  Node     │  │  Node  │  │
 │          └───────────┘  └───┬────┘  │
 └────────────────────────────-┘        │
                    (loops back)         │
 └─────────────────────────────────────-┘
```

### 🔧 Technology Stack
| Component | Technology |
|---|---|
| UI Framework | **Gradio** |
| Web Scraping | **Requests + BeautifulSoup4** |
| Price Parsing | **Regex + Heuristics** |
| Email Alerts | **SMTP (Gmail)** |
| Background Monitoring | **Python threading** |
| Data Persistence | **JSON file (watchlist.json)** |
| Supported Sites | Amazon India, Flipkart, Meesho, Generic |

### 📋 How to Use
1. **Add Products** — Paste product URL, set target price or discount threshold
2. **Configure Email** — Add Gmail + App Password for notifications
3. **Check Now** — Manually trigger a price check anytime
4. **Start Monitor** — Agent runs in background and checks automatically
5. **Agent Log** — View every decision the agent makes in real time
            """)

    # ─────────────────────────────────────────────────────
    # EVENT BINDINGS
    # ─────────────────────────────────────────────────────
    add_btn.click(
        add_product,
        inputs=[add_name, add_url, add_target, add_threshold, add_orig],
        outputs=[watchlist_table, status_box],
    )

    remove_btn.click(
        remove_product,
        inputs=[remove_idx],
        outputs=[watchlist_table, status_box],
    )

    check_all_btn.click(
        check_all_now,
        outputs=[watchlist_table, log_box, status_box],
    )

    refresh_btn.click(
        refresh_all,
        outputs=[watchlist_table, log_box, stats_md],
    )

    start_btn.click(
        start_monitor,
        inputs=[interval_slider],
        outputs=[monitor_status, log_box],
    )

    stop_btn.click(
        stop_monitor,
        outputs=[monitor_status, log_box],
    )

    single_btn.click(
        check_one_now,
        inputs=[single_idx],
        outputs=[watchlist_table, log_box, monitor_status],
    )

    save_email_btn.click(
        save_email_cfg,
        inputs=[email_sender, email_password, email_recipient],
        outputs=[email_status],
    )

    test_email_btn.click(
        test_email,
        inputs=[email_sender, email_password, email_recipient],
        outputs=[email_status, log_box],
    )

    log_refresh_btn.click(
        lambda: (get_log_text(), get_stats()),
        outputs=[log_box, stats_md],
    )

    # Load initial state on page open
    app.load(refresh_all, outputs=[watchlist_table, log_box, stats_md])


# ─────────────────────────────────────────────────────────────
# LAUNCH  — works both locally and on Render.com
# ─────────────────────────────────────────────────────────────
import os as _os

_PORT = int(_os.environ.get("PORT", 7860))
_HOST = "0.0.0.0" if _os.environ.get("RENDER") else "127.0.0.1"
_IN_BROWSER = not bool(_os.environ.get("RENDER"))

if __name__ == "__main__":
    print("\n" + "="*58)
    print("  Automated Discount Notification Agent")
    print("="*58)
    if _os.environ.get("RENDER"):
        print(f"  Running on Render at port {_PORT}")
    else:
        print(f"  Open: http://127.0.0.1:{_PORT}")
    print("="*58 + "\n")

app.launch(
    server_name=_HOST,
    server_port=_PORT,
    inbrowser=_IN_BROWSER,
    show_error=True,
    theme=gr.themes.Soft(primary_hue="indigo"),
    css=CSS,
)

