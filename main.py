"""
COINBASE ALPHA SUITE — PAPER TRADE SIMULATOR + AUTO EXECUTE
Tracks every opportunity, simulates execution, reports P&L
"""

import time
import threading
import requests
import os
import json
from datetime import datetime, timezone

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT     = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_KEY     = os.getenv("ANTHROPIC_API_KEY", "")
ETHERSCAN_KEY     = os.getenv("ETHERSCAN_API_KEY", "")

MIN_ARB_PCT       = float(os.getenv("MIN_ARB_PCT", "0.5"))
ARB_CHECK_SECS    = int(os.getenv("ARB_CHECK_SECONDS", "30"))
SIGNAL_CHECK_SECS = int(os.getenv("SIGNAL_CHECK_SECONDS", "300"))
WHALE_CHECK_SECS  = int(os.getenv("WHALE_CHECK_SECONDS", "60"))
MIN_WHALE_USD     = float(os.getenv("MIN_WHALE_USD", "500000"))
MIN_CONFIDENCE    = int(os.getenv("MIN_AI_CONFIDENCE", "65"))
MAX_POSITION_USD  = float(os.getenv("MAX_POSITION_USD", "100"))
MAX_DAILY_LOSS    = float(os.getenv("MAX_DAILY_LOSS_USD", "100"))
KILL_SWITCH       = os.getenv("KILL_SWITCH", "false").lower() == "true"
TAKE_PROFIT_PCT   = float(os.getenv("TAKE_PROFIT_PCT", "6.0"))
STOP_LOSS_PCT     = float(os.getenv("STOP_LOSS_PCT", "3.0"))
POSITION_CHECK_SECS = int(os.getenv("POSITION_CHECK_SECONDS", "60"))
STARTING_BALANCE  = float(os.getenv("STARTING_BALANCE", "400"))

FEES = {"coinbase": 0.0010, "binance": 0.0010, "kraken": 0.0026}

LARGE_CAPS = [
    ("bitcoin", "BTC"),
    ("ethereum", "ETH"),
]

SMALL_CAPS = [
    ("solana", "SOL"),
    ("ripple", "XRP"),
    ("cardano", "ADA"),
    ("avalanche-2", "AVAX"),
    ("chainlink", "LINK"),
    ("polkadot", "DOT"),
    ("dogecoin", "DOGE"),
    ("uniswap", "UNI"),
    ("litecoin", "LTC"),
    ("stellar", "XLM"),
    ("cosmos", "ATOM"),
    ("filecoin", "FIL"),
    ("tron", "TRX"),
    ("algorand", "ALGO"),
    ("vechain", "VET"),
    ("internet-computer", "ICP"),
    ("shiba-inu", "SHIB"),
    ("pepe", "PEPE"),
    ("aave", "AAVE"),
    ("the-sandbox", "SAND"),
    ("decentraland", "MANA"),
    ("axie-infinity", "AXS"),
    ("the-graph", "GRT"),
    ("enjincoin", "ENJ"),
    ("basic-attention-token", "BAT"),
    ("zilliqa", "ZIL"),
    ("neo", "NEO"),
    ("dash", "DASH"),
    ("zcash", "ZEC"),
]

ALL_COINS = LARGE_CAPS + SMALL_CAPS

# ─────────────────────────────────────────
# STATE
# ─────────────────────────────────────────
open_positions   = {}   # symbol -> {entry, size_usd, target, stop, side, time}
daily_pnl        = 0.0
paper_balance    = STARTING_BALANCE
daily_trades_log = []   # closed trades today
arb_sim_log      = []   # arb opportunities simulated today
all_time_pnl     = 0.0
all_time_trades  = 0

# ─────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────

def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print(f"[TG] {msg[:80]}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        print(f"Telegram error: {e}")

def get_prices(coin_list):
    try:
        ids = ",".join(cg for cg, _ in coin_list)
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ids, "vs_currencies": "usd",
                    "include_24hr_change": "true", "include_1h_change": "true"},
            timeout=15
        )
        return resp.json()
    except Exception as e:
        print(f"  Price fetch error: {e}")
        return {}

def get_current_price(symbol: str):
    cg_map = {sym: cg for cg, sym in ALL_COINS}
    cg_id  = cg_map.get(symbol)
    if not cg_id:
        return None
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": cg_id, "vs_currencies": "usd"}, timeout=8
        )
        return resp.json().get(cg_id, {}).get("usd")
    except:
        return None

def paper_execute_buy(symbol, size_usd, price, reason="signal"):
    """Simulate a buy — deducts from paper balance, opens position."""
    global paper_balance
    if KILL_SWITCH:
        return None
    if daily_pnl <= -MAX_DAILY_LOSS:
        send_telegram(f"🛑 *Daily loss limit hit* (${abs(daily_pnl):.2f})\nBot paused for today.")
        return None
    if symbol in open_positions:
        return None
    if paper_balance < size_usd:
        print(f"  ⚠ Insufficient paper balance (${paper_balance:.2f}) for ${size_usd} trade")
        return None

    paper_balance -= size_usd
    print(f"  📝 PAPER BUY: {symbol} ${size_usd} @ ${price:.4f} | Balance: ${paper_balance:.2f}")
    return {"order_id": f"PAPER-{symbol}-{int(time.time())}", "paper": True}

def paper_execute_sell(symbol, size_usd, entry, exit_price, reason):
    """Simulate a sell — returns funds + profit to paper balance."""
    global paper_balance, daily_pnl, all_time_pnl, all_time_trades
    pnl = size_usd * ((exit_price - entry) / entry)
    paper_balance += size_usd + pnl
    daily_pnl     += pnl
    all_time_pnl  += pnl
    all_time_trades += 1
    print(f"  📝 PAPER SELL: {symbol} @ ${exit_price:.4f} | PnL: ${pnl:+.2f} | Balance: ${paper_balance:.2f}")
    return pnl

# ─────────────────────────────────────────
# POSITION MONITOR
# ─────────────────────────────────────────

def run_position_monitor():
    global open_positions, daily_trades_log
    print("  ✅ Position monitor started")
    while True:
        try:
            if open_positions:
                now = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"\n[POSITIONS {now}] Checking {len(open_positions)} positions...")
                to_close = []

                for symbol, pos in list(open_positions.items()):
                    current = get_current_price(symbol)
                    if not current:
                        continue

                    entry   = pos["entry"]
                    pct_chg = ((current - entry) / entry) * 100
                    size    = pos["size_usd"]
                    held_mins = (time.time() - pos["opened_at"]) / 60
                    print(f"  {symbol}: ${entry:.4f}→${current:.4f} ({pct_chg:+.2f}%) held {held_mins:.0f}m")

                    reason = None
                    if pct_chg >= TAKE_PROFIT_PCT:
                        reason = f"✅ TAKE PROFIT +{pct_chg:.1f}%"
                    elif pct_chg <= -STOP_LOSS_PCT:
                        reason = f"❌ STOP LOSS {pct_chg:.1f}%"
                    elif held_mins >= 1440:  # 24hr max hold
                        reason = f"⏰ TIME EXIT {pct_chg:+.1f}%"

                    if reason:
                        pnl = paper_execute_sell(symbol, size, entry, current, reason)
                        daily_trades_log.append({
                            "symbol": symbol,
                            "side":   pos["side"],
                            "entry":  entry,
                            "exit":   current,
                            "pnl":    pnl,
                            "pct":    pct_chg,
                            "reason": reason,
                            "held_mins": held_mins,
                        })
                        to_close.append(symbol)

                        emoji = "✅" if pnl > 0 else "❌"
                        send_telegram(
                            f"{emoji} *POSITION CLOSED — {symbol}*\n\n"
                            f"📋 {reason}\n"
                            f"📥 Entry: ${entry:.4f}\n"
                            f"📤 Exit: ${current:.4f}\n"
                            f"⏱ Held: {held_mins:.0f} minutes\n"
                            f"💰 P&L: *${pnl:+.2f}* ({pct_chg:+.1f}%)\n\n"
                            f"📊 Today's P&L: ${daily_pnl:+.2f}\n"
                            f"💼 Paper Balance: ${paper_balance:.2f}\n\n"
                            f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                        )

                for symbol in to_close:
                    del open_positions[symbol]

        except Exception as e:
            print(f"[POSITIONS] Error: {e}")
        time.sleep(POSITION_CHECK_SECS)

# ─────────────────────────────────────────
# DAILY SUMMARY
# ─────────────────────────────────────────

def send_daily_summary():
    global daily_pnl, daily_trades_log, arb_sim_log, paper_balance

    now   = datetime.now(timezone.utc)
    wins  = [t for t in daily_trades_log if t["pnl"] > 0]
    losses= [t for t in daily_trades_log if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in daily_trades_log)
    wr    = (len(wins) / len(daily_trades_log) * 100) if daily_trades_log else 0
    best  = max(daily_trades_log, key=lambda x: x["pnl"], default=None)
    worst = min(daily_trades_log, key=lambda x: x["pnl"], default=None)
    emoji = "📈" if total >= 0 else "📉"

    # Arb simulation total
    arb_total = sum(a["simulated_profit"] for a in arb_sim_log)
    arb_count = len(arb_sim_log)

    msg = (
        f"{emoji} *Daily Summary — {now.strftime('%b %d, %Y')}*\n\n"
        f"💼 Paper Balance: *${paper_balance:.2f}*\n"
        f"💰 Today's P&L: *${total:+.2f}*\n"
        f"📊 All-time P&L: *${all_time_pnl:+.2f}*\n\n"
        f"━━━━━━━━━━━━━━\n"
        f"🤖 *Signal Trades*\n"
        f"Total: {len(daily_trades_log)} ({len(wins)}W / {len(losses)}L)\n"
        f"Win Rate: {wr:.0f}%\n"
    )

    if best:
        msg += f"🏆 Best: {best['symbol']} *+${best['pnl']:.2f}* ({best['pct']:+.1f}%)\n"
    if worst:
        msg += f"💀 Worst: {worst['symbol']} *${worst['pnl']:.2f}* ({worst['pct']:+.1f}%)\n"

    if daily_trades_log:
        msg += "\n*Trade log:*\n"
        for t in daily_trades_log[-5:]:  # last 5 trades
            e = "✅" if t["pnl"] > 0 else "❌"
            msg += f"{e} {t['symbol']}: ${t['pnl']:+.2f} ({t['pct']:+.1f}%)\n"

    msg += (
        f"\n━━━━━━━━━━━━━━\n"
        f"🔄 *Arb Opportunities*\n"
        f"Found: {arb_count} opportunities\n"
        f"Simulated profit: *${arb_total:.2f}*\n"
    )

    if arb_sim_log:
        msg += "\n*Top arb today:*\n"
        top_arbs = sorted(arb_sim_log, key=lambda x: x["pct"], reverse=True)[:3]
        for a in top_arbs:
            msg += f"🔄 {a['symbol']}: +{a['pct']:.3f}% (${a['simulated_profit']:.2f})\n"

    if not daily_trades_log and arb_count == 0:
        msg += "\n_No activity today — market was quiet_"

    msg += f"\n\n_📝 Paper mode — no real money_"
    msg += f"\n⏰ {now.strftime('%H:%M UTC')}"

    send_telegram(msg)
    print("[SUMMARY] Daily summary sent")

    # Reset daily trackers
    daily_trades_log.clear()
    arb_sim_log.clear()
    daily_pnl = 0.0

def run_daily_summary():
    last_date = None
    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.hour == 0 and now.minute == 0 and now.date() != last_date:
                last_date = now.date()
                send_daily_summary()
        except Exception as e:
            print(f"[SUMMARY] Error: {e}")
        time.sleep(55)

# ─────────────────────────────────────────
# ARB SCANNER + SIMULATOR
# ─────────────────────────────────────────

def binance_price(symbol):
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

def kraken_price(symbol):
    try:
        kb   = "XBT" if symbol == "BTC" else symbol
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

arb_alerted = set()

def run_arb_scanner():
    global arb_sim_log
    print("""
╔══════════════════════════════════╗
║   ARB SCANNER + SIMULATOR 🔄    ║
╚══════════════════════════════════╝""")
    while True:
        try:
            now    = datetime.now(timezone.utc).strftime("%H:%M:%S")
            prices = get_prices(ALL_COINS)
            found  = 0
            print(f"\n[ARB {now}] Scanning {len(ALL_COINS)} coins...")

            for cg_id, symbol in ALL_COINS:
                usd = prices.get(cg_id, {}).get("usd")
                if not usd:
                    continue

                cg  = {"bid": usd * 0.9995, "ask": usd * 1.0005}
                bnb = binance_price(symbol)
                krk = kraken_price(symbol)

                exchanges = {"coinbase": cg}
                if bnb: exchanges["binance"] = bnb
                if krk: exchanges["kraken"]  = krk

                ex   = list(exchanges.keys())
                best = None

                for i in range(len(ex)):
                    for j in range(len(ex)):
                        if i == j: continue
                        buy_p  = exchanges[ex[i]]["ask"]
                        sell_p = exchanges[ex[j]]["bid"]
                        net    = ((sell_p - buy_p) / buy_p) - FEES[ex[i]] - FEES[ex[j]]
                        if net >= MIN_ARB_PCT / 100:
                            if best is None or net > best["net"]:
                                best = {
                                    "buy": ex[i], "sell": ex[j],
                                    "buy_p": buy_p, "sell_p": sell_p,
                                    "net": net, "net_pct": net * 100
                                }

                if best:
                    key = f"{symbol}-{best['net_pct']:.1f}"
                    sim_profit = MAX_POSITION_USD * (best["net"] )
                    print(f"  {symbol}: 🚨 ARB +{best['net_pct']:.3f}% → sim profit ${sim_profit:.2f}")

                    # Log to arb simulator
                    arb_sim_log.append({
                        "symbol": symbol,
                        "pct": best["net_pct"],
                        "simulated_profit": sim_profit,
                        "buy_ex": best["buy"],
                        "sell_ex": best["sell"],
                        "time": datetime.now(timezone.utc).strftime("%H:%M"),
                    })

                    if key not in arb_alerted:
                        arb_alerted.add(key)
                        send_telegram(
                            f"🔄 *ARB — {symbol}*\n\n"
                            f"📥 Buy {best['buy'].upper()} @ ${best['buy_p']:,.4f}\n"
                            f"📤 Sell {best['sell'].upper()} @ ${best['sell_p']:,.4f}\n"
                            f"💰 *+{best['net_pct']:.3f}%*\n\n"
                            f"📊 *Simulated on ${MAX_POSITION_USD:.0f}: +${sim_profit:.2f}*\n"
                            f"📊 Simulated on $400: +${400 * best['net']:.2f}\n\n"
                            f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                        )
                        found += 1

            if not found:
                print(f"  ✓ No arb ≥{MIN_ARB_PCT}%")

        except Exception as e:
            print(f"[ARB] Error: {e}")
        time.sleep(ARB_CHECK_SECS)

# ─────────────────────────────────────────
# SIGNAL ANALYZER + AUTO PAPER EXECUTE
# ─────────────────────────────────────────

def ask_claude(prompt):
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
                "max_tokens": 200,
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
    global open_positions
    print("""
╔══════════════════════════════════╗
║   SIGNAL ANALYZER 🤖            ║
╚══════════════════════════════════╝""")
    if not ANTHROPIC_KEY:
        print("  ⚠ No ANTHROPIC_API_KEY — disabled")
        return

    while True:
        try:
            now = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"\n[SIGNALS {now}] Scanning {len(SMALL_CAPS)} small caps...")
            prices        = get_prices(SMALL_CAPS)
            fg_val, fg_lb = get_fear_greed()

            for cg_id, symbol in SMALL_CAPS:
                if symbol in open_positions:
                    print(f"  {symbol}: position open — skip")
                    continue

                d   = prices.get(cg_id, {})
                usd = d.get("usd")
                if not usd:
                    continue

                ch24 = d.get("usd_24h_change", 0) or 0
                ch1h = d.get("usd_1h_change", 0) or 0

                if abs(ch1h) < 1.0 and abs(ch24) < 3.0:
                    print(f"  {symbol}: low momentum — skip")
                    continue

                print(f"  🤖 {symbol} ${usd:.4f} (1h:{ch1h:+.1f}% 24h:{ch24:+.1f}%)...")

                prompt = (
                    f"Crypto momentum trade for {symbol}/USD:\n"
                    f"Price: ${usd:.4f} | 1h: {ch1h:.2f}% | 24h: {ch24:.2f}%\n"
                    f"Fear & Greed: {fg_val}/100 ({fg_lb})\n\n"
                    f"Good entry for 5-7% gain? Respond ONLY as JSON:\n"
                    f'{{"signal":"LONG or SHORT or NEUTRAL","confidence":0-100,"reasoning":"one sentence"}}'
                )

                raw = ask_claude(prompt)
                if not raw:
                    continue

                try:
                    clean  = raw.replace("```json","").replace("```","").strip()
                    s      = json.loads(clean)
                    conf   = s.get("confidence", 0)
                    sig    = s.get("signal", "NEUTRAL")
                    reason = s.get("reasoning", "")
                    print(f"  {symbol}: {sig} @ {conf}%")

                    if sig != "NEUTRAL" and conf >= MIN_CONFIDENCE:
                        target = usd * (1 + TAKE_PROFIT_PCT/100) if sig == "LONG" else usd * (1 - TAKE_PROFIT_PCT/100)
                        stop   = usd * (1 - STOP_LOSS_PCT/100)   if sig == "LONG" else usd * (1 + STOP_LOSS_PCT/100)

                        # Auto paper execute
                        order = paper_execute_buy(symbol, MAX_POSITION_USD, usd, reason)
                        if order:
                            open_positions[symbol] = {
                                "entry":     usd,
                                "size_usd":  MAX_POSITION_USD,
                                "target":    target,
                                "stop":      stop,
                                "side":      sig,
                                "opened_at": time.time(),
                                "confidence": conf,
                            }
                            emoji = "🟢" if sig == "LONG" else "🔴"
                            send_telegram(
                                f"{emoji} *PAPER TRADE OPENED — {symbol}*\n\n"
                                f"📋 {sig} @ {conf}% confidence\n"
                                f"💰 Entry: ${usd:.4f}\n"
                                f"🎯 Target: ${target:.4f} (+{TAKE_PROFIT_PCT}%)\n"
                                f"🛑 Stop: ${stop:.4f} (-{STOP_LOSS_PCT}%)\n"
                                f"💵 Size: ${MAX_POSITION_USD:.0f}\n"
                                f"💼 Remaining balance: ${paper_balance:.2f}\n\n"
                                f"📝 {reason}\n\n"
                                f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                            )

                except Exception as e:
                    print(f"  {symbol}: parse error — {e}")

        except Exception as e:
            print(f"[SIGNALS] Error: {e}")
        time.sleep(SIGNAL_CHECK_SECS)

# ─────────────────────────────────────────
# WHALE WATCHER
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
            print(f"\n[WHALE {now}] Scanning...")
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
                            "sort": "desc", "apikey": ETHERSCAN_KEY,
                        }, timeout=10
                    )
                    txns = resp.json().get("result", [])
                    if not isinstance(txns, list):
                        print(f"  {label[:20]}: API limit")
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
                            f"💰 ${val_usd:,.0f} ({val_eth:.2f} ETH)\n"
                            f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                        )
                except Exception as e:
                    print(f"  {label[:20]}: {e}")
        except Exception as e:
            print(f"[WHALE] Error: {e}")
        time.sleep(WHALE_CHECK_SECS)

# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

def main():
    global paper_balance
    print(f"""
╔══════════════════════════════════════════════════╗
║   💎  COINBASE ALPHA SUITE — ALL SYSTEMS GO     ║
╚══════════════════════════════════════════════════╝

  🔄 Arb Scanner       — {len(ALL_COINS)} coins + profit simulator
  🤖 Signal Analyzer   — {len(SMALL_CAPS)} small caps, auto paper execute
  📊 Position Monitor  — auto close +{TAKE_PROFIT_PCT}% / -{STOP_LOSS_PCT}%
  🐋 Whale Watcher     — on-chain moves
  📅 Daily Summary     — midnight UTC

  Starting paper balance: ${paper_balance:.2f}
  Position size: ${MAX_POSITION_USD:.0f}
  Min confidence: {MIN_CONFIDENCE}%
""")

    send_telegram(
        "💎 *Coinbase Alpha Suite LIVE*\n\n"
        f"💼 Starting paper balance: *${paper_balance:.2f}*\n"
        f"💵 Position size: ${MAX_POSITION_USD:.0f} per trade\n"
        f"🎯 Take profit: +{TAKE_PROFIT_PCT}%\n"
        f"🛑 Stop loss: -{STOP_LOSS_PCT}%\n"
        f"🤖 {len(SMALL_CAPS)} small caps being watched\n"
        f"🔄 Arb simulator tracking all opportunities\n\n"
        f"📊 Every trade auto-executed in paper mode\n"
        f"📅 Daily summary at midnight UTC\n\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )

    threads = [
        threading.Thread(target=run_arb_scanner,      daemon=True, name="Arb"),
        threading.Thread(target=run_signal_analyzer,  daemon=True, name="Signals"),
        threading.Thread(target=run_whale_watcher,    daemon=True, name="Whale"),
        threading.Thread(target=run_position_monitor, daemon=True, name="Positions"),
        threading.Thread(target=run_daily_summary,    daemon=True, name="Summary"),
    ]

    for t in threads:
        t.start()
        print(f"  ✅ Started: {t.name}")
        time.sleep(2)

    print("\n  All systems go.\n")

    while True:
        time.sleep(60)
        alive   = [t.name for t in threads if t.is_alive()]
        pos_str = f"{len(open_positions)} open" if open_positions else "none"
        print(f"  ♻ {len(alive)}/5 modules | positions: {pos_str} | today P&L: ${daily_pnl:+.2f} | balance: ${paper_balance:.2f}")

if __name__ == "__main__":
    main()
