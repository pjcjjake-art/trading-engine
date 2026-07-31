import os, json, time, datetime, asyncio
import anthropic
import requests
import discord
import yfinance as yf
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY")
WEBHOOK_URL      = os.getenv("DISCORD_WEBHOOK")
BOT_TOKEN        = os.getenv("DISCORD_BOT_TOKEN")
SCAN_INTERVAL    = int(os.getenv("SCAN_INTERVAL_SECONDS", 900))
COMMAND_PREFIX   = "!"
CONTROL_CENTER_URL = os.getenv("CONTROL_CENTER_URL", "https://control-center-production-512b.up.railway.app")

client_ai = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)

FILES = {
    "watchlist":         "watchlist.json",
    "trading_watchlist": "trading_watchlist.json",
    "alerts":            "alerts.json",
    "fired":             "fired_alerts.json",
    "portfolio":         "portfolio.json",
    "seen_news":         "seen_news.json",
}

# Broad, liquid universe scanned each cycle for top movers, independent of
# the trading watchlist — catches opportunities you haven't explicitly listed.
MARKET_UNIVERSE = [
    "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","AMD","NFLX","AVGO",
    "JPM","BAC","XOM","CVX","UNH","JNJ","V","MA","HD","WMT",
    "SPY","QQQ","IWM","DIA","GLD","SLV",
    "COIN","MSTR","PLTR","SOFI",
    "BTC-USD","ETH-USD","SOL-USD",
]
TOP_MOVERS_COUNT = int(os.getenv("TOP_MOVERS_COUNT", 8))

# ── File helpers ──────────────────────────────────────────────────────────────
def load(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except:
            pass
    return default

def save(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

# ── Discord webhook (for auto scans) ─────────────────────────────────────────
def send_webhook(embeds):
    if not WEBHOOK_URL:
        return
    try:
        for i in range(0, len(embeds), 10):
            requests.post(WEBHOOK_URL, json={"embeds": embeds[i:i+10]}, timeout=10)
    except Exception as e:
        print(f"[Webhook error] {e}")

# ── Control Center signal forwarding ─────────────────────────────────────────
def send_signal_to_control_center(symbol, direction, thesis, score=5, source="bot.py",
                                   asset_type="equity", option_type=None, strike=None,
                                   expiration=None, position_effect="open", contracts=1):
    if not CONTROL_CENTER_URL:
        return
    try:
        requests.post(
            f"{CONTROL_CENTER_URL}/api/signals",
            json={
                "symbol": symbol,
                "direction": direction,
                "thesis": thesis,
                "score": score,
                "source": source,
                "asset_type": asset_type,
                "option_type": option_type,
                "strike": strike,
                "expiration": expiration,
                "position_effect": position_effect,
                "contracts": contracts,
            },
            timeout=5,
        )
    except Exception as e:
        print(f"[Control Center error] {e}")

# ── Embeds ────────────────────────────────────────────────────────────────────
def alert_embed(ticker, reason, price, pct, name=""):
    color = 0x3ba55d if pct >= 0 else 0xed4245
    return {
        "title": f"{'🟢' if pct >= 0 else '🔴'} Alert: {ticker}",
        "description": f"**{name or ticker}**\n{reason}",
        "color": color,
        "fields": [
            {"name": "Price",  "value": f"${price:.2f}", "inline": True},
            {"name": "Change", "value": f"{pct:+.2f}%",  "inline": True},
        ],
        "footer": {"text": f"Market Bot · {datetime.datetime.now().strftime('%H:%M')}"},
    }

def mover_embed(ticker, name, price, pct):
    color = 0x3ba55d if pct >= 0 else 0xed4245
    return {
        "title": f"⚡ Unusual Mover: {ticker}",
        "description": f"**{name or ticker}** is making a big move",
        "color": color,
        "fields": [
            {"name": "Price",  "value": f"${price:.2f}", "inline": True},
            {"name": "Change", "value": f"{pct:+.2f}%",  "inline": True},
        ],
        "footer": {"text": f"Market Bot · {datetime.datetime.now().strftime('%H:%M')}"},
    }

def news_embed(item, watchlist_tickers):
    cat_colors = {"macro": 0x3498db, "earnings": 0x2ecc71, "sector": 0x9b59b6, "geo": 0xe67e22, "watchlist": 0x8e44ad}
    cat_icons  = {"macro": "📊", "earnings": "📈", "sector": "⚙️", "geo": "🌐", "watchlist": "⭐"}
    sent_arrow = {"bullish": "▲", "bearish": "▼", "neutral": "●"}
    cat  = item.get("category", "macro")
    hits = [t for t in item.get("tickers", []) if t in watchlist_tickers]
    desc = item.get("summary", "")
    if hits:
        desc += f"\n\n⭐ **Watchlist:** {', '.join(hits)}"
    impact = item.get("impact", "")
    impact_str = f"**[{impact}]** " if impact == "HIGH" else f"[{impact}] " if impact else ""
    return {
        "title": f"{cat_icons.get(cat,'📰')} {item['title']}",
        "description": f"{impact_str}{desc}",
        "color": cat_colors.get(cat, 0x95a5a6),
        "fields": [
            {"name": "Sentiment", "value": f"{sent_arrow.get(item.get('sentiment','neutral'),'●')} {item.get('sentiment','—').capitalize()}", "inline": True},
            {"name": "Source",    "value": item.get("source", "—"), "inline": True},
            {"name": "Tickers",   "value": ", ".join(item.get("tickers", [])) or "—", "inline": True},
        ],
        "footer": {"text": f"Market Bot · NEWS · {datetime.datetime.now().strftime('%H:%M')}"},
    }

def opportunity_embed(analysis):
    return {
        "title": "🧠 AI Market Scan",
        "description": analysis,
        "color": 0x5865f2,
        "footer": {"text": f"Market Bot · AI · {datetime.datetime.now().strftime('%H:%M')}"},
    }

# ── AI calls ──────────────────────────────────────────────────────────────────
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
            price     = round(float(hist["Close"].iloc[-1]), 4)
            prev      = round(float(hist["Close"].iloc[-2]), 4) if len(hist) >= 2 else price
            change    = round(((price - prev) / prev) * 100, 2) if prev else 0
            volume    = int(hist["Volume"].iloc[-1]) if "Volume" in hist else 0
            avg_vol   = int(info.get("averageVolume", volume) or volume)
            unusual   = abs(change) > 3 or (avg_vol > 0 and volume > avg_vol * 2)
            name      = info.get("shortName") or info.get("longName") or ticker
            # earnings date
            try:
                cal = t.calendar
                if cal is not None and not cal.empty:
                    ed = cal.iloc[0].get("Earnings Date")
                    earnings_date = str(ed)[:10] if ed else None
                else:
                    earnings_date = None
            except:
                earnings_date = None
            results.append({
                "ticker":       ticker,
                "name":         name,
                "price":        price,
                "changePct":    change,
                "volume":       volume,
                "avgVolume":    avg_vol,
                "unusual":      unusual,
                "earningsDate": earnings_date,
                "type":         w.get("type", "Stock"),
            })
            print(f"  ✓ {ticker}: ${price} ({change:+.2f}%)")
        except Exception as e:
            print(f"  [Error] {ticker}: {e}")
    return results

def get_top_movers(universe_tickers, top_n=8):
    """Scan a broad ticker universe and return the top_n biggest movers by
    absolute % change, regardless of what's on any watchlist."""
    if not universe_tickers:
        return []
    data = fetch_market_data([{"ticker": t, "type": "Stock"} for t in universe_tickers])
    return sorted(data, key=lambda d: abs(d.get("changePct", 0)), reverse=True)[:top_n]

def fetch_breaking_news(watchlist_tickers):
    prompt = f"""Search the web for breaking market-moving news right now (today {datetime.date.today().isoformat()}).
Return ONLY a valid JSON array — no markdown, no backticks, no explanation.
Find 4 HIGH impact headlines covering macro/Fed, earnings, sector, geopolitical, and news about: {watchlist_tickers or ['SPY','QQQ','BTC-USD']}.
Each object: id (string), title (string, max 90 chars), summary (string, max 200 chars), category (macro/earnings/sector/geo/watchlist), impact (HIGH/MEDIUM/LOW), sentiment (bullish/bearish/neutral), tickers (array of strings), source (string), watchlistHit (boolean).
Keep summaries SHORT to avoid truncation. Start response with [ and end with ]"""
    resp = client_ai.messages.create(
        model="claude-sonnet-4-6", max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    text = text.replace("```json","").replace("```","").strip()
    s, e = text.find("["), text.rfind("]")
    if s == -1 or e == -1:
        return []
    try:
        items = json.loads(text[s:e+1])
        return [i for i in items if i.get("impact") == "HIGH"]
    except json.JSONDecodeError:
        return []

def check_alerts(market_data, alerts):
    fired, data_map = [], {d["ticker"]: d for d in market_data}
    for a in alerts:
        d = data_map.get(a["ticker"])
        if not d:
            continue
        price, pct = d["price"], d["changePct"]
        triggered, reason = False, ""
        c = a["condition"]
        if   c == "price_above"    and price >= a["value"]:  triggered=True; reason=f"Price ${price:.2f} crossed above ${a['value']}"
        elif c == "price_below"    and price <= a["value"]:  triggered=True; reason=f"Price ${price:.2f} dropped below ${a['value']}"
        elif c == "pct_move_up"    and pct   >= a["value"]:  triggered=True; reason=f"Up {pct:+.2f}% (threshold +{a['value']}%)"
        elif c == "pct_move_down"  and pct   <= -a["value"]: triggered=True; reason=f"Down {pct:+.2f}% (threshold -{a['value']}%)"
        elif c == "earnings_soon"  and d.get("earningsDate"):triggered=True; reason=f"Earnings approaching: {d['earningsDate']}"
        if triggered:
            fired.append({"ticker": a["ticker"], "reason": reason,
                          "price": price, "changePct": pct, "name": d.get("name",""),
                          "firedAt": datetime.datetime.now().isoformat()})
    return fired

def ai_briefing(market_data, watchlist_tickers, question=None):
    prompt = question if question else f"""Analyze this market data and give a sharp 3-bullet trading briefing for a trader watching: {watchlist_tickers}
Data: {json.dumps(market_data[:8], indent=2)}
Format: bullet points, mention specific tickers, max 3 bullets, one punchy opening sentence. Discord formatting."""
    resp = client_ai.messages.create(
        model="claude-sonnet-4-6", max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text.strip()

def ai_news_briefing(watchlist_tickers):
    prompt = f"""Search for the latest breaking market news right now for a trader watching: {watchlist_tickers or 'general market'}.
Give a sharp Discord-formatted briefing:
- One sentence on overall market tone
- 3 bullet points of most actionable headlines with tickers
- 1 thing to watch in the next hour
Keep it tight. Use Discord markdown."""
    resp = client_ai.messages.create(
        model="claude-sonnet-4-6", max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()

def ai_generate_signals(market_data, watchlist_tickers):
    """Ask Claude to analyze current market data and return structured trade
    ideas with genuine confidence scores (0-10). Anything scoring at or above
    the control center's AUTO_EXECUTE_THRESHOLD will place a real trade with
    no further human review — so the model is instructed to score honestly,
    not to inflate confidence. Ideas may be equities or single-leg options
    (long calls/puts, covered calls, cash-secured puts — no multi-leg spreads,
    Robinhood's MCP doesn't support those)."""
    if not market_data:
        return []
    today = datetime.date.today().isoformat()
    prompt = f"""You are a disciplined trading analyst reviewing live market data for: {watchlist_tickers}
Today's date: {today}
Data: {json.dumps(market_data[:10], indent=2)}

For each ticker where you see a genuinely actionable setup, return a trade idea.
Skip tickers with no clear edge — do not force an idea for every ticker.
Score confidence honestly on a 0-10 scale where 8+ means "I would act on this with real money right now."
Most setups should score well below 8. Only score 8+ when the signal is unusually clean and well-supported.

Ideas can be equity (simple buy/sell of shares) or single-leg options (long call, long put,
covered call, cash-secured put). Only propose options when there's a clear reason options fit
better than shares (e.g. defined risk, leverage on high conviction, income on a stalled name).
Never propose multi-leg spreads — only single-leg option positions are supported.

Return ONLY a valid JSON array, no markdown, no backticks, no explanation. Each object:
For equity: {{"symbol": "TICKER", "asset_type": "equity", "direction": "buy"|"sell", "thesis": "one sentence, max 200 chars", "score": number}}
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

# ── Auto scan task ────────────────────────────────────────────────────────────
@tasks.loop(seconds=SCAN_INTERVAL)
async def auto_scan():
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Auto scan running...")
    watchlist  = load(FILES["watchlist"], [])
    trading_wl = load(FILES["trading_watchlist"], [])
    alerts     = load(FILES["alerts"],    [])
    portfolio  = load(FILES["portfolio"], [])
    fired_hist = load(FILES["fired"],     [])
    seen_news  = set(load(FILES["seen_news"], []))
    wl_tickers = [w["ticker"] for w in watchlist]
    trading_tickers = [w["ticker"] for w in trading_wl]
    embeds     = []

    try:
        market_data = fetch_market_data(watchlist) if wl_tickers else []
        fired = check_alerts(market_data, alerts)
        for f in fired:
            embeds.append(alert_embed(f["ticker"], f["reason"], f["price"], f["changePct"], f["name"]))
            fired_hist.append(f)
        for m in [d for d in market_data if d.get("unusual")]:
            embeds.append(mover_embed(m["ticker"], m.get("name",""), m["price"], m["changePct"]))
    except Exception as e:
        print(f"[Error] Market: {e}")
        market_data = []

    try:
        trading_data = fetch_market_data(trading_wl) if trading_tickers else []
        top_movers = get_top_movers(MARKET_UNIVERSE, TOP_MOVERS_COUNT)
        # Merge, avoiding duplicates if a mover is already on the trading watchlist
        seen_tickers = {d["ticker"] for d in trading_data}
        combined_data = trading_data + [m for m in top_movers if m["ticker"] not in seen_tickers]
        combined_tickers = trading_tickers + [m["ticker"] for m in top_movers if m["ticker"] not in seen_tickers]
        if top_movers:
            movers_str = ", ".join(f"{m['ticker']} {m['changePct']:+.2f}%" for m in top_movers)
            print(f"  Top movers: {movers_str}")

        ai_signals = ai_generate_signals(combined_data, combined_tickers)
        for sig in ai_signals:
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
            desc = f"{sig.get('symbol')} {sig.get('direction')}"
            if sig.get("asset_type") == "option":
                desc += f" {sig.get('option_type')} ${sig.get('strike')} exp {sig.get('expiration')}"
            print(f"  → AI signal: {desc} score={sig.get('score')}")
    except Exception as e:
        print(f"[Error] AI signal generation: {e}")

    try:
        news_items = fetch_breaking_news(wl_tickers)
        for item in news_items:
            nid = item.get("id", item["title"][:40])
            if nid not in seen_news:
                embeds.append(news_embed(item, wl_tickers))
                seen_news.add(nid)
        save(FILES["seen_news"], list(seen_news)[-500:])
    except Exception as e:
        print(f"[Error] News: {e}")

    try:
        analysis = ai_briefing(market_data, wl_tickers)
        embeds.append(opportunity_embed(analysis))
    except Exception as e:
        print(f"[Error] AI scan: {e}")

    if embeds:
        send_webhook(embeds)
        print(f"  → Sent {len(embeds)} embeds")

    save(FILES["fired"], fired_hist[-200:])

# ── Bot events ────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✓ Logged in as {bot.user}")
    auto_scan.start()

# ── Commands ──────────────────────────────────────────────────────────────────
@bot.command(name="scan")
async def cmd_scan(ctx):
    msg = await ctx.send("⏳ Scanning markets...")
    watchlist  = load(FILES["watchlist"], [])
    alerts     = load(FILES["alerts"],    [])
    fired_hist = load(FILES["fired"],     [])
    wl_tickers = [w["ticker"] for w in watchlist]
    embeds     = []
    try:
        market_data = fetch_market_data(watchlist) if wl_tickers else []
        fired = check_alerts(market_data, alerts)
        for f in fired:
            embeds.append(alert_embed(f["ticker"], f["reason"], f["price"], f["changePct"], f["name"]))
            fired_hist.append(f)
            direction = "buy" if f["reason"].startswith(("Price", "Up")) else "sell"
            send_signal_to_control_center(f["ticker"], direction, f["reason"], score=5, source="alert")
        for m in [d for d in market_data if d.get("unusual")]:
            embeds.append(mover_embed(m["ticker"], m.get("name",""), m["price"], m["changePct"]))
        analysis = ai_briefing(market_data, wl_tickers)
        embeds.append(opportunity_embed(analysis))
        save(FILES["fired"], fired_hist[-200:])
        await msg.delete()
        if embeds:
            for i in range(0, len(embeds), 10):
                await ctx.send(embeds=[discord.Embed.from_dict(e) for e in embeds[i:i+10]])
        else:
            await ctx.send("✅ Scan complete — nothing notable right now.")
    except Exception as e:
        await msg.edit(content=f"❌ Scan error: {e}")

@bot.command(name="news")
async def cmd_news(ctx):
    msg = await ctx.send("📡 Fetching breaking news...")
    try:
        briefing = ai_news_briefing([w["ticker"] for w in load(FILES["watchlist"], [])])
        await msg.delete()
        embed = discord.Embed(title="📰 Breaking Market News", description=briefing, color=0x3498db)
        embed.set_footer(text=f"Market Bot · {datetime.datetime.now().strftime('%H:%M')}")
        await ctx.send(embed=embed)
    except Exception as e:
        await msg.edit(content=f"❌ Error: {e}")

@bot.command(name="briefing")
async def cmd_briefing(ctx):
    msg = await ctx.send("🧠 Generating AI briefing...")
    try:
        watchlist  = load(FILES["watchlist"], [])
        wl_tickers = [w["ticker"] for w in watchlist]
        market_data = fetch_market_data(watchlist) if wl_tickers else []
        result = ai_briefing(market_data, wl_tickers)
        await msg.delete()
        embed = discord.Embed(title="🧠 AI Market Briefing", description=result, color=0x5865f2)
        embed.set_footer(text=f"Market Bot · {datetime.datetime.now().strftime('%H:%M')}")
        await ctx.send(embed=embed)
    except Exception as e:
        await msg.edit(content=f"❌ Error: {e}")

@bot.command(name="ask")
async def cmd_ask(ctx, *, question: str):
    msg = await ctx.send("🤔 Thinking...")
    try:
        watchlist   = load(FILES["watchlist"], [])
        wl_tickers  = [w["ticker"] for w in watchlist]
        market_data = fetch_market_data(watchlist) if wl_tickers else []
        full_q = f"""You are a sharp trading bot assistant. Answer this question from a trader: "{question}"
Their watchlist: {wl_tickers}
Current market snapshot: {json.dumps(market_data[:6], indent=2)}
Be direct, specific, and actionable. Use Discord markdown. Max 300 words."""
        resp = client_ai.messages.create(
            model="claude-sonnet-4-6", max_tokens=600,
            messages=[{"role": "user", "content": full_q}]
        )
        answer = resp.content[0].text.strip()
        await msg.delete()
        embed = discord.Embed(title=f"💬 {question[:80]}", description=answer, color=0xf1c40f)
        embed.set_footer(text=f"Market Bot · {datetime.datetime.now().strftime('%H:%M')}")
        await ctx.send(embed=embed)
    except Exception as e:
        await msg.edit(content=f"❌ Error: {e}")

@bot.command(name="watchlist")
async def cmd_watchlist(ctx):
    watchlist = load(FILES["watchlist"], [])
    if not watchlist:
        await ctx.send("📋 Your watchlist is empty. Use `!add TICKER` to add tickers.")
        return
    lines = "\n".join([f"• **{w['ticker']}** — {w.get('type','Stock')}" for w in watchlist])
    embed = discord.Embed(title="📋 Your Watchlist", description=lines, color=0x2ecc71)
    embed.set_footer(text=f"{len(watchlist)} tickers · Use !add TICKER or !remove TICKER")
    await ctx.send(embed=embed)

@bot.command(name="add")
async def cmd_add(ctx, ticker: str):
    ticker = ticker.upper()
    watchlist = load(FILES["watchlist"], [])
    if any(w["ticker"] == ticker for w in watchlist):
        await ctx.send(f"⚠️ **{ticker}** is already on your watchlist.")
        return
    watchlist.append({"ticker": ticker, "type": "Stock"})
    save(FILES["watchlist"], watchlist)
    await ctx.send(f"✅ Added **{ticker}** to your watchlist.")

@bot.command(name="remove")
async def cmd_remove(ctx, ticker: str):
    ticker = ticker.upper()
    watchlist = load(FILES["watchlist"], [])
    new = [w for w in watchlist if w["ticker"] != ticker]
    if len(new) == len(watchlist):
        await ctx.send(f"⚠️ **{ticker}** not found in watchlist.")
        return
    save(FILES["watchlist"], new)
    await ctx.send(f"🗑️ Removed **{ticker}** from your watchlist.")

@bot.command(name="tradingwatchlist")
async def cmd_tradingwatchlist(ctx):
    watchlist = load(FILES["trading_watchlist"], [])
    if not watchlist:
        await ctx.send("📋 Trading watchlist is empty. Use `!tradingadd TICKER` to add tickers the AI will scan and trade.")
        return
    lines = "\n".join([f"• **{w['ticker']}** — {w.get('type','Stock')}" for w in watchlist])
    embed = discord.Embed(title="🤖 Trading Watchlist (AI auto-scan/trade)", description=lines, color=0xe74c3c)
    embed.set_footer(text=f"{len(watchlist)} tickers · these are scanned and may auto-execute trades")
    await ctx.send(embed=embed)

@bot.command(name="tradingadd")
async def cmd_tradingadd(ctx, ticker: str):
    ticker = ticker.upper()
    watchlist = load(FILES["trading_watchlist"], [])
    if any(w["ticker"] == ticker for w in watchlist):
        await ctx.send(f"⚠️ **{ticker}** is already on the trading watchlist.")
        return
    watchlist.append({"ticker": ticker, "type": "Stock"})
    save(FILES["trading_watchlist"], watchlist)
    await ctx.send(f"✅ Added **{ticker}** to the trading watchlist — AI will scan it and may auto-execute trades.")

@bot.command(name="tradingremove")
async def cmd_tradingremove(ctx, ticker: str):
    ticker = ticker.upper()
    watchlist = load(FILES["trading_watchlist"], [])
    new = [w for w in watchlist if w["ticker"] != ticker]
    if len(new) == len(watchlist):
        await ctx.send(f"⚠️ **{ticker}** not found in trading watchlist.")
        return
    save(FILES["trading_watchlist"], new)
    await ctx.send(f"🗑️ Removed **{ticker}** from the trading watchlist.")

@bot.command(name="alerts")
async def cmd_alerts(ctx):
    alerts = load(FILES["alerts"], [])
    if not alerts:
        await ctx.send("🔔 No alerts set. Use `!alert TICKER condition value`")
        return
    lines = "\n".join([f"• **{a['ticker']}** — {a['condition']} {a['value']}" for a in alerts])
    embed = discord.Embed(title="🔔 Your Alerts", description=lines, color=0xe67e22)
    embed.set_footer(text=f"{len(alerts)} alerts · Use !alert TICKER condition value to add")
    await ctx.send(embed=embed)

@bot.command(name="alert")
async def cmd_alert(ctx, ticker: str, condition: str, value: float):
    ticker = ticker.upper()
    valid = ["price_above","price_below","pct_move_up","pct_move_down","earnings_soon"]
    if condition not in valid:
        await ctx.send(f"⚠️ Invalid condition. Use: {', '.join(valid)}")
        return
    alerts = load(FILES["alerts"], [])
    alerts.append({"ticker": ticker, "condition": condition, "value": value})
    save(FILES["alerts"], alerts)
    await ctx.send(f"✅ Alert set: **{ticker}** {condition} {value}")

@bot.command(name="removealert")
async def cmd_removealert(ctx, ticker: str, condition: str = None):
    ticker = ticker.upper()
    alerts = load(FILES["alerts"], [])
    if condition:
        new = [a for a in alerts if not (a["ticker"] == ticker and a["condition"] == condition)]
    else:
        new = [a for a in alerts if a["ticker"] != ticker]
    removed = len(alerts) - len(new)
    save(FILES["alerts"], new)
    await ctx.send(f"🗑️ Removed {removed} alert(s) for **{ticker}**.")

@bot.command(name="testsignal")
async def cmd_testsignal(ctx, ticker: str = "TEST", direction: str = "buy"):
    ticker = ticker.upper()
    send_signal_to_control_center(
        ticker, direction,
        thesis="Manual test signal from !testsignal",
        score=5, source="manual_test",
    )
    await ctx.send(f"📡 Sent test signal: **{ticker}** {direction} (score 5) → check the dashboard's Pending Signals.")

@bot.command(name="help")
async def cmd_help(ctx):
    embed = discord.Embed(title="📖 Market Bot Commands", color=0x5865f2)
    embed.add_field(name="🔍 Scanning", value="`!scan` — run a full market scan now\n`!news` — get breaking headlines\n`!briefing` — AI market summary\n`!ask [question]` — ask the AI anything", inline=False)
    embed.add_field(name="📋 Watchlist", value="`!watchlist` — show your tickers\n`!add TICKER` — add a ticker\n`!remove TICKER` — remove a ticker", inline=False)
    embed.add_field(name="🔔 Alerts", value="`!alerts` — show your alerts\n`!alert TICKER condition value` — set an alert\n`!removealert TICKER` — remove alerts\n\nConditions: `price_above` `price_below` `pct_move_up` `pct_move_down`", inline=False)
    embed.set_footer(text=f"Auto-scan runs every {SCAN_INTERVAL//60} minutes")
    await ctx.send(embed=embed)

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("[ERROR] DISCORD_BOT_TOKEN not set in .env")
    else:
        print("Starting Market Bot...")
        bot.run(BOT_TOKEN)
