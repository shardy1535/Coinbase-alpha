"""
COINBASE ALPHA SUITE — SINGLE FILE VERSION
All modules combined so you only need to upload ONE file.
"""

import time
import threading
import requests
import os
from datetime import datetime, timezone

# ─────────────────────────────────────────
# CONFIG — set these in Railway Variables
# ─────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT    = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
MIN_ARB_PCT      = float(os.getenv("MIN_ARB_PCT", "0.5"))
ARB_CHECK_SECS   = int(os.getenv("ARB_CHECK_SECONDS", "30"))
SIGNAL_CHECK_SECS = int(os.getenv("SIGNAL_CHECK_SECONDS", "300"))
WHALE_CHECK_SECS = int(os.getenv("WHALE_CHECK_SECONDS", "60"))
MIN_WHALE_USD    = float(os.getenv("MIN_WHALE_USD", "500000"))
MIN_CONFIDENCE   = int(os.getenv("MIN_AI_CONFIDENCE", "75"))
MAX_POSITION_USD = float(os.getenv("MAX_POSITION_USD", "25"))
KILL_SWITCH      = os.getenv("KILL_SWITCH", "false").lower() == "true"

FEES = {"coinbase": 0.0010, "binance": 0.0010, "kraken": 0.0026}
COINS = [("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL")]

# ─────────────────────────────────────────
# SHARED UTILITIES
# ─────────────────────────────────────────

def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print(f"[TELEGRAM] {msg[:100]}...")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        print(f"Telegram error: {e}")

def get_prices():
    try:
        ids = ",".join(cg for cg, _ in COINS)
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ids, "vs_currencies": "usd",
                    "include_24hr_change": "true", "include_24hr_vol": "true"},
            timeout=10
        )
        return resp.json()
    except Exception as e:
        print(f"  Price fetch error: {e}")
        return {}

def binance_price(symbol: str):
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/ticker/bookTicker",
            params={"symbol": f"{symbol}USDT"}, timeout=5
        )
        if resp.status_code == 200:
            d = resp.json()
            return {"bid": float(d["bidPrice"]), "ask": float(d["askPrice"])}
    except:
        pass
    return None

def kraken_price(symbol: str):
    try:
        kb = "XBT" if symbol == "BTC" else symbol
        pair = f"X{kb}ZUSD" if kb == "XBT" else f"{kb}USD"
        resp = requests.get(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": pair}, timeout=5
        )
        result = resp.json().get("result", {})
        if result:
            t = list(result.values())[0]
            return {"bid": float(t["b"][0]), "ask": float(t["a"][0])}
    except:
        pass
    return None

# ─────────────────────────────────────────
# MODULE 1 — ARB SCANNER
# ─────────────────────────────────────────

arb_alerted = set()

def run_arb_scanner():
    print("""
╔══════════════════════════════════╗
║   ARB SCANNER 🔄                ║
╚══════════════════════════════════╝""")
    while True:
        try:
            now = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"\n[ARB {now}] Scanning...")
            prices = get_prices()
            found = 0

            for cg_id, symbol in COINS:
                usd = prices.get(cg_id, {}).get("usd")
                if not usd:
                    print(f"  {symbol}: no price")
                    continue

                cg = {"bid": usd * 0.9995, "ask": usd * 1.0005}
                bnb = binance_price(symbol)
                krk = kraken_price(symbol)

                exchanges = {"coinbase": cg}
                if bnb: exchanges["binance"] = bnb
                if krk: exchanges["kraken"] = krk

                ex = list(exchanges.keys())
                best = None

                for i in range(len(ex)):
                    for j in range(len(ex)):
                        if i == j: continue
                        buy_p  = exchanges[ex[i]]["ask"]
                        sell_p = exchanges[ex[j]]["bid"]
                        net    = ((sell_p - buy_p) / buy_p) - FEES[ex[i]] - FEES[ex[j]]
                        if net >= MIN_ARB_PCT / 100:
                            if best is None or net > best["net"]:
                                best = {"buy": ex[i], "sell": ex[j],
                                        "buy_p": buy_p, "sell_p": sell_p,
                                        "net": net, "net_pct": net * 100}

                if best:
                    key = f"{symbol}-{best['net_pct']:.1f}"
                    print(f"  {symbol}: 🚨 ARB {best['buy']}→{best['sell']} +{best['net_pct']:.3f}%")
                    if key not in arb_alerted:
                        arb_alerted.add(key)
                        p100 = best["net"] * 100
                        send_telegram(
                            f"🔄 *ARB OPPORTUNITY*\n\n"
                            f"🪙 *{symbol}/USD*\n"
                            f"📥 Buy {best['buy'].upper()} @ ${best['buy_p']:,.2f}\n"
                            f"📤 Sell {best['sell'].upper()} @ ${best['sell_p']:,.2f}\n\n"
                            f"💰 *+{best['net_pct']:.3f}% profit*\n"
                            f"$100 → *+${p100:.2f}*\n\n"
                            f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                        )
                        found += 1
                else:
                    print(f"  {symbol}: ${usd:,.0f} — no arb")

            if not found:
                print(f"  ✓ No arb ≥{MIN_ARB_PCT}%")

        except Exception as e:
            print(f"[ARB] Error: {e}")

        time.sleep(ARB_CHECK_SECS)

# ─────────────────────────────────────────
# MODULE 2 — AI SIGNAL ANALYZER
# ─────────────────────────────────────────

def ask_claude(prompt: str) -> str:
    if not ANTHROPIC_KEY:
        return ""
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=20
        )
        data = resp.json()
        if "content" in data:
            return data["content"][0]["text"]
        print(f"  Claude error: {data.get('error', data)}")
        return ""
    except Exception as e:
        print(f"  Claude exception: {e}")
        return ""

def get_fear_greed():
    try:
        resp = requests.get("https://api.alternative.me/fng/", timeout=8)
        d = resp.json()["data"][0]
        return int(d["value"]), d["value_classification"]
    except:
        return 50, "Neutral"

def run_signal_analyzer():
    print("""
╔══════════════════════════════════╗
║   SIGNAL ANALYZER 🤖            ║
╚══════════════════════════════════╝""")

    if not ANTHROPIC_KEY:
        print("  ⚠ No ANTHROPIC_API_KEY — signal analyzer disabled")
        return

    while True:
        try:
            now = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"\n[SIGNALS {now}] Running analysis...")

            prices = get_prices()
            fg_val, fg_label = get_fear_greed()

            for cg_id, symbol in COINS:
                d = prices.get(cg_id, {})
                usd = d.get("usd")
                if not usd:
                    print(f"  {symbol}: no price data")
                    continue

                ch24 = d.get("usd_24h_change", 0)
                print(f"  🤖 Analyzing {symbol} @ ${usd:,.2f} ({ch24:+.1f}% 24h)...")

                prompt = f"""You are a crypto trading analyst. Analyze {symbol}/USD:

Price: ${usd:,.2f}
24h Change: {ch24:.2f}%
Fear & Greed: {fg_val}/100 ({fg_label})

Respond ONLY as JSON, no other text:
{{"signal":"LONG or SHORT or NEUTRAL","confidence":0-100,"entry":{usd},"target":number,"stop":number,"reasoning":"one sentence","risk":"one sentence"}}"""

                raw = ask_claude(prompt)
                if not raw:
                    print(f"  {symbol}: no response from Claude")
                    continue

                try:
                    import json
                    clean = raw.replace("```json","").replace("```","").strip()
                    s = json.loads(clean)
                    conf = s.get("confidence", 0)
                    sig  = s.get("signal", "NEUTRAL")
                    print(f"  {symbol}: {sig} @ {conf}% confidence")

                    if sig != "NEUTRAL" and conf >= MIN_CONFIDENCE:
                        entry  = s.get("entry", usd)
                        target = s.get("target", usd)
                        stop   = s.get("stop", usd)
                        pct    = ((target - entry) / entry * 100) if entry else 0

                        send_telegram(
                            f"{'🟢' if sig == 'LONG' else '🔴'} *{symbol} SIGNAL: {sig}*\n\n"
                            f"💰 Entry: ${entry:,.2f}\n"
                            f"🎯 Target: ${target:,.2f} ({pct:+.1f}%)\n"
                            f"🛑 Stop: ${stop:,.2f}\n\n"
                            f"🧠 Confidence: {conf}%\n"
                            f"😱 Fear & Greed: {fg_val}/100\n\n"
                            f"📝 {s.get('reasoning','')}\n"
                            f"⚠️ {s.get('risk','')}\n\n"
                            f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                        )
                except Exception as e:
                    print(f"  {symbol}: parse error — {e}")
                    print(f"  Raw: {raw[:100]}")

        except Exception as e:
            print(f"[SIGNALS] Error: {e}")

        time.sleep(SIGNAL_CHECK_SECS)

# ─────────────────────────────────────────
# MODULE 3 — WHALE WATCHER
# ─────────────────────────────────────────

WHALE_ADDRESSES = {
    "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43": "Coinbase Hot Wallet",
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance Hot Wallet",
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549": "Binance Cold Wallet",
}
seen_txns = set()

def run_whale_watcher():
    print("""
╔══════════════════════════════════╗
║   WHALE WATCHER 🐋              ║
╚══════════════════════════════════╝""")
    while True:
        try:
            now = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"\n[WHALE {now}] Scanning {len(WHALE_ADDRESSES)} wallets...")

            try:
                pr = requests.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": "ethereum", "vs_currencies": "usd"}, timeout=8
                )
                eth_price = pr.json()["ethereum"]["usd"]
            except:
                eth_price = 3000

            for address, label in WHALE_ADDRESSES.items():
                try:
                    resp = requests.get(
                        "https://api.etherscan.io/api",
                        params={
                            "module": "account", "action": "txlist",
                            "address": address, "page": 1, "offset": 5,
                            "sort": "desc", "apikey": "YourFreeKeyFromEtherscan",
                        }, timeout=10
                    )
                    txns = resp.json().get("result", [])
                    if not isinstance(txns, list):
                        print(f"  {label[:20]}: API limit hit")
                        continue

                    new = [t for t in txns if t.get("hash") not in seen_txns]
                    if not new:
                        print(f"  {label[:20]}: quiet")
                        continue

                    for tx in new:
                        seen_txns.add(tx["hash"])
                        val_eth = int(tx.get("value", 0)) / 1e18
                        val_usd = val_eth * eth_price
                        if val_usd < MIN_WHALE_USD:
                            continue
                        direction = "OUT 📤" if tx["from"].lower() == address.lower() else "IN 📥"
                        print(f"  {label[:20]}: 🚨 ${val_usd:,.0f} {direction}")
                        send_telegram(
                            f"🐋 *WHALE ALERT*\n\n"
                            f"{direction} *{label}*\n"
                            f"💰 ${val_usd:,.0f} ({val_eth:.2f} ETH)\n\n"
                            f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                        )
                except Exception as e:
                    print(f"  {label[:20]}: error — {e}")

        except Exception as e:
            print(f"[WHALE] Error: {e}")

        time.sleep(WHALE_CHECK_SECS)

# ─────────────────────────────────────────
# MAIN — START EVERYTHING
# ─────────────────────────────────────────

def main():
    print("""
╔══════════════════════════════════════════════════╗
║   💎  COINBASE ALPHA SUITE — ALL SYSTEMS GO     ║
╚══════════════════════════════════════════════════╝

  Starting 3 modules:
  🔄 Arb Scanner    — cross-exchange price gaps
  🤖 Signal Analyzer — AI trade signals
  🐋 Whale Watcher  — large on-chain moves

  Telegram alerts enabled.
""")

    send_telegram(
        "💎 *Coinbase Alpha Suite is LIVE*\n\n"
        "🔄 Arb Scanner — running\n"
        "🤖 Signal Analyzer — running\n"
        "🐋 Whale Watcher — running\n\n"
        f"⏰ Started {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )

    threads = [
        threading.Thread(target=run_arb_scanner,     daemon=True, name="Arb"),
        threading.Thread(target=run_signal_analyzer, daemon=True, name="Signals"),
        threading.Thread(target=run_whale_watcher,   daemon=True, name="Whale"),
    ]

    for t in threads:
        t.start()
        print(f"  ✅ Started: {t.name}")
        time.sleep(2)

    print("\n  All systems go. Waiting for alerts...\n")

    while True:
        time.sleep(60)
        alive = [t.name for t in threads if t.is_alive()]
        print(f"  ♻ Heartbeat — {len(alive)}/3 modules running")

if __name__ == "__main__":
    main()
