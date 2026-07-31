"""
Trading Engine — standalone AI signal generator + auto-trader.

No Discord dependency at all. Runs entirely on its own: scans your trading
watchlist plus a broad market universe for top movers, asks Claude to
evaluate genuine trade ideas (equity or single-leg options), and sends
anything actionable to the control center — which auto-executes via Alpaca
when a signal's confidence clears the threshold.

Your existing bot.py / Discord alerts (metals, crypto, news) are completely
untouched by this — this script only reads trading_watchlist.json.

Run: python trading_engine.py
Stop: Ctrl+C
"""
import os, json, time, datetime
import anthropic
import requests
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
CONTROL_CENTER_URL = os.getenv("CONTROL_CENTER_URL", "https://control-center-production-512b.up.railway.app")
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_SECONDS", 900))  # 15 min default
TOP_MOVERS_COUNT = int(os.getenv("TOP_MOVERS_COUNT", 8))

TRADING_WATCHLIST_FILE = "trading_watchlist.json"

# Broad, liquid universe scanned each cycle for top movers, independent of
# your explicit trading watchlist — catches opportunities you haven't listed.
# Narrow, gold- and index-focused universe — deliberately excludes single
# stocks and crypto to avoid earnings/news-driven noise unrelated to the
# technical/momentum setups this engine is built to evaluate.
MARKET_UNIVERSE = [
    "GLD", "SLV", "GDX",       # gold, silver, gold miners
    "SPY", "QQQ", "IWM", "DIA",  # S&P 500, Nasdaq, Russell 2000, Dow
]

if not ANTHROPIC_KEY:
    raise SystemExit("ANTHROPIC_API_KEY not set — check your .env file")

client_ai = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


def load(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            print(f"[Warning] Could not parse {path}: {e}")
    return default


def fetch_market_data(watchlist):
    if not watchlist:
        return []
    results = []
    for w in watchlist:
        ticker = w["ticker"]
        try:
            t = yf.Ticker(ticker)
            info = t.info
            hist = t.history(period="2d")
            if hist.empty or len(hist) < 1:
                print(f"  [Skip] {ticker} — no data")
                continue
            price = round(float(hist["Close"].iloc[-1]), 4)
            prev = round(float(hist["Close"].iloc[-2]), 4) if len(hist) >= 2 else price
            change = round(((price - prev) / prev) * 100, 2) if prev else 0
            volume = int(hist["Volume"].iloc[-1]) if "Volume" in hist else 0
            avg_vol = int(info.get("averageVolume", volume) or volume)
            name = info.get("shortName") or info.get("longName") or ticker
            results.append({
                "ticker": ticker, "name": name, "price": price,
                "changePct": change, "volume": volume, "avgVolume": avg_vol,
                "type": w.get("type", "Stock"),
            })
            print(f"  {ticker}: ${price} ({change:+.2f}%)")
        except Exception as e:
            print(f"  [Error] {ticker}: {e}")
    return results


def get_top_movers(universe_tickers, top_n=8):
    if not universe_tickers:
        return []
    data = fetch_market_data([{"ticker": t, "type": "Stock"} for t in universe_tickers])
    return sorted(data, key=lambda d: abs(d.get("changePct", 0)), reverse=True)[:top_n]


def send_signal_to_control_center(symbol, direction, thesis, score=5, source="trading_engine",
                                   asset_type="equity", option_type=None, strike=None,
                                   expiration=None, position_effect="open", contracts=1):
    if not CONTROL_CENTER_URL:
        return
    try:
        resp = requests.post(
            f"{CONTROL_CENTER_URL}/api/signals",
            json={
                "symbol": symbol, "direction": direction, "thesis": thesis,
                "score": score, "source": source, "asset_type": asset_type,
                "option_type": option_type, "strike": strike, "expiration": expiration,
                "position_effect": position_effect, "contracts": contracts,
            },
            timeout=10,
        )
        print(f"    → sent to control center: {resp.status_code} {resp.json()}")
    except Exception as e:
        print(f"  [Control Center error] {e}")


def ai_generate_signals(market_data, watchlist_tickers):
    """Ask Claude to analyze current market data and return structured trade
    ideas with genuine confidence scores (0-10). Anything scoring at or above
    the control center's AUTO_EXECUTE_THRESHOLD will place a real trade with
    no further human review — so the model is instructed to score honestly,
    not to inflate confidence."""
    if not market_data:
        return []
    today = datetime.date.today().isoformat()
    prompt = f"""You are a disciplined trading analyst reviewing live market data for: {watchlist_tickers}
Today's date: {today}
Data: {json.dumps(market_data[:10], indent=2)}

For each ticker where you see a genuinely actionable setup, decide the direction based on
where the clean move actually points — do not default to buying just because a ticker is on
the list. Skip tickers with no clear edge — do not force an idea for every ticker.
Score confidence honestly on a 0-10 scale where 8+ means "I would act on this with real money right now."
Most setups should score well below 8. Only score 8+ when the signal is unusually clean and well-supported.

For equities: pick "long" (bullish — you expect the price to rise) or "short" (bearish — you
expect the price to fall; this opens a real short position, borrowing shares to sell). Base the
direction purely on what the data shows, not on a bias toward buying.

For options (only when there's a clear reason options fit better than shares — leverage on high
conviction, defined risk, income on a stalled name): single-leg only (long call, long put, covered
call, cash-secured put). Never propose multi-leg spreads.

Return ONLY a valid JSON array, no markdown, no backticks, no explanation. Each object:
For equity: {{"symbol": "TICKER", "asset_type": "equity", "direction": "long"|"short", "thesis": "one sentence, max 200 chars", "score": number}}
For options: {{"symbol": "TICKER", "asset_type": "option", "direction": "buy"|"sell", "option_type": "call"|"put", "strike": number, "expiration": "YYYY-MM-DD", "position_effect": "open"|"close", "contracts": 1, "thesis": "one sentence, max 200 chars", "score": number}}

If nothing is actionable, return an empty array []."""
    resp = client_ai.messages.create(
        model="claude-sonnet-4-6", max_tokens=1200,
        messages=[{"role": "user", "content": prompt}]
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    text = text.replace("```json", "").replace("```", "").strip()
    s, e = text.find("["), text.rfind("]")
    if s == -1 or e == -1:
        return []
    try:
        return json.loads(text[s:e+1])
    except json.JSONDecodeError:
        return []


def manage_open_positions():
    """Ask the control center to check every open position against its
    stop-loss/take-profit thresholds and close anything past either one —
    no approval needed."""
    try:
        resp = requests.post(f"{CONTROL_CENTER_URL}/api/positions/manage", timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if result.get("closed"):
            for c in result["closed"]:
                if c.get("ok"):
                    print(f"  CLOSED {c['symbol']} ({c['reason']}): {c['pnl_pct']:+.2f}% / ${c['pnl_usd']:+.2f}")
                else:
                    print(f"  [Error] Failed to close {c['symbol']}: {c.get('error')}")
        else:
            print(f"  Checked {result.get('checked', 0)} open position(s) — none past stop-loss/take-profit")
    except Exception as e:
        print(f"  [Error] Position management check failed: {e}")


def run_cycle():
    print(f"\n[{time.strftime('%H:%M:%S')}] Scan starting...")

    manage_open_positions()

    trading_wl = load(TRADING_WATCHLIST_FILE, [])
    trading_tickers = [w["ticker"] for w in trading_wl]
    if not trading_tickers:
        print(f"  [Warning] {TRADING_WATCHLIST_FILE} is empty or missing — only scanning top movers")

    trading_data = fetch_market_data(trading_wl) if trading_tickers else []

    print("  Scanning market universe for top movers...")
    top_movers = get_top_movers(MARKET_UNIVERSE, TOP_MOVERS_COUNT)
    if top_movers:
        movers_str = ", ".join(f"{m['ticker']} {m['changePct']:+.2f}%" for m in top_movers)
        print(f"  Top movers: {movers_str}")

    seen_tickers = {d["ticker"] for d in trading_data}
    combined_data = trading_data + [m for m in top_movers if m["ticker"] not in seen_tickers]
    combined_tickers = trading_tickers + [m["ticker"] for m in top_movers if m["ticker"] not in seen_tickers]

    if not combined_data:
        print("  No data to analyze this cycle.")
        return

    print("  Asking AI to evaluate for actionable signals...")
    try:
        signals = ai_generate_signals(combined_data, combined_tickers)
    except Exception as e:
        print(f"  [Error] AI signal generation failed: {e}")
        return

    if not signals:
        print("  No actionable signals this cycle.")
        return

    for sig in signals:
        desc = f"{sig.get('symbol')} {sig.get('direction')}"
        if sig.get("asset_type") == "option":
            desc += f" {sig.get('option_type')} ${sig.get('strike')} exp {sig.get('expiration')}"
        print(f"  SIGNAL: {desc} score={sig.get('score')} — {sig.get('thesis')}")
        send_signal_to_control_center(
            sig.get("symbol", ""), sig.get("direction", "buy"),
            sig.get("thesis", ""), score=sig.get("score", 0),
            source="ai_auto",
            asset_type=sig.get("asset_type", "equity"),
            option_type=sig.get("option_type"),
            strike=sig.get("strike"),
            expiration=sig.get("expiration"),
            position_effect=sig.get("position_effect", "open"),
            contracts=sig.get("contracts", 1),
        )


if __name__ == "__main__":
    print("=" * 60)
    print("Trading Engine — standalone, no Discord dependency")
    print(f"Control center: {CONTROL_CENTER_URL}")
    print(f"Scan interval: {SCAN_INTERVAL}s ({SCAN_INTERVAL/60:.0f} min)")
    print("=" * 60)
    while True:
        try:
            run_cycle()
        except Exception as e:
            print(f"[Error] Cycle failed: {e}")
        print(f"  Sleeping {SCAN_INTERVAL}s until next scan...")
        time.sleep(SCAN_INTERVAL)
