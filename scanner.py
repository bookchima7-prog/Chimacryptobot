import os
import json
import time
import requests
import schedule
import threading
from datetime import datetime, timedelta
import google.generativeai as genai

# ============================================================
# CONFIGURATION — Replace these with your real keys on Render
# ============================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "your_gemini_key_here")
CRYPTOPANIC_API_KEY = os.environ.get("CRYPTOPANIC_API_KEY", "your_cryptopanic_key_here")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "your_telegram_bot_token_here")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "your_telegram_chat_id_here")

# ============================================================
# WEEKLY ROTATION SCHEDULE
# Group A: Mon + Thu | Group B: Tue + Fri | Group C: Wed + Sat
# Group D: Sunday only
# ============================================================
SCHEDULE = {
    0: {"group": "A", "rescan": False},   # Monday
    1: {"group": "B", "rescan": False},   # Tuesday
    2: {"group": "C", "rescan": False},   # Wednesday
    3: {"group": "A", "rescan": True},    # Thursday (rescan A)
    4: {"group": "B", "rescan": True},    # Friday (rescan B)
    5: {"group": "C", "rescan": True},    # Saturday (rescan C)
    6: {"group": "D", "rescan": False},   # Sunday
}

GROUP_RANGES = {
    "A": (0, 1400),
    "B": (1400, 2800),
    "C": (2800, 4200),
    "D": (4200, 9800),
}

RESULTS_FILE = "scan_results.json"

# ============================================================
# SETUP GEMINI
# ============================================================
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")


# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=10)
        print(f"[Telegram] Sent: {message[:80]}...")
    except Exception as e:
        print(f"[Telegram Error] {e}")


# ============================================================
# LOAD / SAVE PREVIOUS RESULTS
# ============================================================
def load_previous_results():
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_results(data):
    with open(RESULTS_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ============================================================
# STEP 1 — FETCH ALL COINS FROM COINGECKO
# ============================================================
def fetch_all_coins():
    print("[CoinGecko] Fetching full coin list...")
    url = "https://api.coingecko.com/api/v3/coins/markets"
    all_coins = []
    page = 1
    while True:
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 250,
            "page": page,
            "sparkline": False,
            "price_change_percentage": "1h,24h"
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            data = resp.json()
            if not data or not isinstance(data, list):
                break
            all_coins.extend(data)
            print(f"[CoinGecko] Fetched page {page} — {len(all_coins)} coins so far")
            if len(data) < 250:
                break
            page += 1
            time.sleep(1.5)  # Rate limit respect
            if len(all_coins) >= 9800:
                break
        except Exception as e:
            print(f"[CoinGecko Error] {e}")
            break
    return all_coins


# ============================================================
# STEP 2 — STAGE 1 FILTER (No AI needed)
# ============================================================
def stage1_filter(coins):
    passed = []
    for coin in coins:
        try:
            price_change_24h = coin.get("price_change_percentage_24h") or 0
            volume = coin.get("total_volume") or 0
            market_cap = coin.get("market_cap") or 0
            current_price = coin.get("current_price") or 0

            # Filter criteria
            if (
                price_change_24h >= 3.0 and        # Price up at least 3%
                volume >= 500000 and                # Volume at least $500K
                market_cap >= 1000000 and           # Market cap at least $1M
                current_price > 0                   # Valid price
            ):
                passed.append(coin)
        except:
            continue
    print(f"[Filter] Stage 1: {len(passed)} coins passed out of {len(coins)}")
    return passed


# ============================================================
# STEP 3 — FETCH NEWS FROM CRYPTOPANIC
# ============================================================
def fetch_news(coin_symbol):
    url = "https://cryptopanic.com/api/v1/posts/"
    params = {
        "auth_token": CRYPTOPANIC_API_KEY,
        "currencies": coin_symbol.upper(),
        "filter": "hot",
        "public": "true"
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        results = data.get("results", [])
        headlines = [r.get("title", "") for r in results[:5]]
        return headlines
    except:
        return []


# ============================================================
# STEP 4 — GEMINI AI ANALYSIS
# ============================================================
def gemini_analyse(coin, news_headlines):
    name = coin.get("name", "")
    symbol = coin.get("symbol", "").upper()
    price = coin.get("current_price", 0)
    change_24h = coin.get("price_change_percentage_24h", 0)
    volume = coin.get("total_volume", 0)
    market_cap = coin.get("market_cap", 0)
    high_24h = coin.get("high_24h", 0)
    low_24h = coin.get("low_24h", 0)

    news_text = "\n".join(news_headlines) if news_headlines else "No recent news found."

    prompt = f"""
You are a professional crypto market analyst. Analyse this coin and give a Buy/Sell/Hold recommendation.

COIN DATA:
- Name: {name} ({symbol})
- Current Price: ${price}
- 24H Change: {change_24h:.2f}%
- 24H Volume: ${volume:,.0f}
- Market Cap: ${market_cap:,.0f}
- 24H High: ${high_24h}
- 24H Low: ${low_24h}

RECENT NEWS:
{news_text}

Respond ONLY in this exact JSON format, nothing else:
{{
  "signal": "BUY" or "SELL" or "HOLD",
  "confidence": <number between 0 and 100>,
  "sentiment": "BULLISH" or "BEARISH" or "NEUTRAL",
  "reason": "<2-3 sentence explanation>",
  "risk": "LOW" or "MEDIUM" or "HIGH"
}}
"""
    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        # Clean JSON
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        return result
    except Exception as e:
        print(f"[Gemini Error] {symbol}: {e}")
        return None


# ============================================================
# STEP 5 — COMPARE WITH PREVIOUS SCAN (Option B)
# ============================================================
def compare_with_previous(symbol, new_score, previous_results):
    prev = previous_results.get(symbol)
    if not prev:
        return None, None

    prev_score = prev.get("confidence", 0)
    diff = new_score - prev_score

    if diff >= 5:
        trend = "↗️ STRENGTHENING"
        advice = "Signal growing stronger since last scan"
    elif diff <= -5:
        trend = "↘️ WEAKENING"
        advice = "Signal fading since last scan — be cautious"
    else:
        trend = "➡️ STABLE"
        advice = "Signal holding steady since last scan"

    return trend, advice


# ============================================================
# STEP 6 — BUILD TELEGRAM MESSAGE
# ============================================================
def build_alert_message(top_coins, group, is_rescan, day_name, previous_results):
    date_str = datetime.now().strftime("%d %b %Y")
    rescan_label = "🔄 RESCAN" if is_rescan else "🔍 FRESH SCAN"

    msg = f"""📊 <b>CRYPTO SCANNER ALERT</b>
{rescan_label} — Group {group}
📅 {day_name}, {date_str}
━━━━━━━━━━━━━━━━━━━━━━

"""

    if not top_coins:
        msg += "❌ No strong signals found today.\nMarket conditions not favourable.\n"
        msg += "\n━━━━━━━━━━━━━━━━━━━━━━"
        msg += "\n🤖 Chima Dtrader Scanner"
        return msg

    for i, (coin, analysis) in enumerate(top_coins):
        symbol = coin.get("symbol", "").upper()
        name = coin.get("name", "")
        price = coin.get("current_price", 0)
        change = coin.get("price_change_percentage_24h", 0)
        signal = analysis.get("signal", "HOLD")
        confidence = analysis.get("confidence", 0)
        reason = analysis.get("reason", "")
        risk = analysis.get("risk", "MEDIUM")
        sentiment = analysis.get("sentiment", "NEUTRAL")

        medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i] if i < 5 else "✅"
        signal_emoji = "🟢" if signal == "BUY" else "🔴" if signal == "SELL" else "🟡"

        msg += f"{medal} <b>{name} ({symbol})</b>\n"
        msg += f"{signal_emoji} <b>{signal}</b> — {confidence}% Confidence\n"
        msg += f"💰 Price: ${price:,.4f} ({change:+.2f}%)\n"
        msg += f"📰 Sentiment: {sentiment}\n"
        msg += f"⚠️ Risk: {risk}\n"
        msg += f"💡 {reason}\n"

        # Option B comparison
        if is_rescan:
            trend, advice = compare_with_previous(symbol, confidence, previous_results)
            if trend:
                msg += f"📈 Trend: {trend}\n"
                msg += f"ℹ️ {advice}\n"

        msg += "\n"

    msg += "━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += "⚠️ <i>Not financial advice. DYOR.</i>\n"
    msg += "🤖 <b>Chima Dtrader Scanner</b>"
    return msg


# ============================================================
# MAIN DAILY SCAN
# ============================================================
def run_daily_scan():
    today = datetime.now().weekday()  # 0=Monday, 6=Sunday
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    day_name = day_names[today]

    schedule_info = SCHEDULE[today]
    group = schedule_info["group"]
    is_rescan = schedule_info["rescan"]
    start_idx, end_idx = GROUP_RANGES[group]

    print(f"\n{'='*50}")
    print(f"[Scanner] Starting {day_name} scan — Group {group}")
    print(f"[Scanner] Coins range: {start_idx} to {end_idx}")
    print(f"{'='*50}\n")

    # Load previous results for comparison
    all_previous = load_previous_results()
    previous_results = all_previous.get(group, {})

    # Step 1: Fetch coins
    send_telegram(f"🔄 <b>Starting {day_name} scan...</b>\nGroup {group} | Fetching coins...")
    all_coins = fetch_all_coins()

    if not all_coins:
        send_telegram("❌ Failed to fetch coins from CoinGecko. Will retry tomorrow.")
        return

    # Slice the group
    group_coins = all_coins[start_idx:min(end_idx, len(all_coins))]
    print(f"[Scanner] Group {group} has {len(group_coins)} coins to scan")

    # Step 2: Stage 1 filter
    filtered_coins = stage1_filter(group_coins)

    if not filtered_coins:
        msg = f"📊 <b>{day_name} Scan Complete</b>\nGroup {group} — No coins passed initial filter today.\nMarket conditions weak across the board."
        send_telegram(msg)
        return

    # Step 3 & 4: News + Gemini analysis
    results = []
    print(f"[Scanner] Running AI analysis on {len(filtered_coins)} coins...")

    for i, coin in enumerate(filtered_coins[:50]):  # Cap at 50 for Gemini safety
        symbol = coin.get("symbol", "")
        name = coin.get("name", "")
        print(f"[{i+1}/{min(len(filtered_coins), 50)}] Analysing {name} ({symbol.upper()})...")

        news = fetch_news(symbol)
        time.sleep(0.5)

        analysis = gemini_analyse(coin, news)
        time.sleep(1)  # Rate limit

        if analysis and analysis.get("signal") == "BUY" and analysis.get("confidence", 0) >= 75:
            results.append((coin, analysis))
            print(f"  ✅ {symbol.upper()} — {analysis.get('confidence')}% BUY")
        else:
            print(f"  ❌ {symbol.upper()} — filtered out")

    # Step 5: Sort by confidence
    results.sort(key=lambda x: x[1].get("confidence", 0), reverse=True)
    top_coins = results[:5]  # Top 5 max

    # Step 6: Save results for future comparison (only on fresh scan days)
    if not is_rescan:
        current_scan = {}
        for coin, analysis in results:
            symbol = coin.get("symbol", "").upper()
            current_scan[symbol] = {
                "confidence": analysis.get("confidence", 0),
                "signal": analysis.get("signal", ""),
                "price": coin.get("current_price", 0),
                "date": datetime.now().strftime("%Y-%m-%d")
            }
        all_previous[group] = current_scan
        save_results(all_previous)
        print(f"[Scanner] Saved {len(current_scan)} results for Group {group}")

    # Step 7: Send Telegram alert
    message = build_alert_message(top_coins, group, is_rescan, day_name, previous_results)
    send_telegram(message)

    print(f"\n[Scanner] Done! Found {len(top_coins)} top picks today.")
    print(f"[Scanner] Next scan tomorrow.\n")


# ============================================================
# SCHEDULER — Runs every day at 8:00 AM
# ============================================================
def start_scheduler():
    schedule.every().day.at("08:00").do(run_daily_scan)
    print("[Scheduler] Bot started. Scanning daily at 08:00 AM.")
    send_telegram("🤖 <b>Chima Dtrader Crypto Scanner is LIVE!</b>\nDaily scans at 8:00 AM.\nCovering 9,800+ coins weekly. 🚀")

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    print("=" * 50)
    print("  CHIMA DTRADER CRYPTO SCANNER")
    print("  Built for Godswill | SMC Trader")
    print("=" * 50)
    start_scheduler()
