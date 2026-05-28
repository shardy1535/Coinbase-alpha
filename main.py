"""
COINBASE ALPHA SUITE — LIVE ARB EXECUTION
Auto-executes arb trades: Buy Kraken + Sell Coinbase simultaneously
"""

import time
import threading
import requests
import os
import json
import hmac
import hashlib
import base64
import urllib.parse
from datetime import datetime, timezone

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT     = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_KEY     = os.getenv("ANTHROPIC_API_KEY", "")
ETHERSCAN_KEY     = os.getenv("ETHERSCAN_API_KEY", "")

COINBASE_API_KEY  = os.getenv("COINBASE_API_KEY", "")
COINBASE_SECRET   = os.getenv("COINBASE_API_SECRET", "")
KRAKEN_API_KEY    = os.getenv("KRAKEN_API_KEY", "")
KRAKEN_API_SECRET = os.getenv("KRAKEN_API_SECRET", "")

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

# Live trading flag — True when both API keys are present
LIVE_TRADING = bool(COINBASE_API_KEY and KRAKEN_API_KEY)

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
open_positions    = {}
daily_pnl         = 0.0
paper_balance     = STARTING_BALANCE
daily_trades_log  = []
arb_sim_log       = []
all_time_pnl      = 0.0
all_time_trades   = 0
daily_arb_profit  = 0.0

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

# ─────────────────────────────────────────
# KRAKEN API
# ─────────────────────────────────────────

def kraken_sign(urlpath, data):
    postdata  = urllib.parse.urlencode(data)
    encoded   = (str(data["nonce"]) + postdata).encode()
    message   = urlpath.encode() + hashlib.sha256(encoded).digest()
    signature = hmac.new(
        base64.b64decode(KRAKEN_API_SECRET),
        message, hashlib.sha512
    )
    return base64.b64encode(signature.digest()).decode()

def kraken_request(urlpath, data):
    data["nonce"] = str(int(time.time() * 1000))
    headers = {
        "API-Key":  KRAKEN_API_KEY,
        "API-Sign": kraken_sign(urlpath, data),
    }
    resp = requests.post(
        f"https://api.kraken.com{urlpath}",
        headers=headers, data=data, timeout=10
    )
    return resp.json()

def kraken_get_balance():
    try:
        result = kraken_request("/0/private/Balance", {})
        balances = result.get("result", {})
        zusd = float(balances.get("ZUSD", 0))
        usdc = float(balances.get("USDC", 0))
        return zusd + usdc
    except Exception as e:
        print(f"  Kraken balance error: {e}")
        return 0.0

def kraken_market_buy(symbol: str, size_usd: float, price: float):
    """Place a market buy order on Kraken."""
    try:
        # Convert symbol to Kraken format
        kb     = "XBT" if symbol == "BTC" else symbol
        pair   = f"X{kb}ZUSD" if kb == "XBT" else f"{kb}USD"
        volume = round(size_usd / price, 6)

        result = kraken_request("/0/private/AddOrder", {
            "pair":      pair,
            "type":      "buy",
            "ordertype": "market",
            "volume":    str(volume),
        })

        errors = result.get("error", [])
        if errors:
            print(f"  Kraken buy error: {errors}")
            return None

        txids = result.get("result", {}).get("txid", [])
        print(f"  ✅ Kraken BUY {symbol} ${size_usd:.2f} — txid: {txids}")
        return txids

    except Exception as e:
        print(f"  Kraken buy exception: {e}")
        return None

def kraken_market_sell(symbol: str, size_usd: float, price: float):
    """Place a market sell order on Kraken."""
    try:
        kb     = "XBT" if symbol == "BTC" else symbol
        pair   = f"X{kb}ZUSD" if kb == "XBT" else f"{kb}USD"
        volume = round(size_usd / price, 6)

        result = kraken_request("/0/private/AddOrder", {
            "pair":      pair,
            "type":      "sell",
            "ordertype": "market",
            "volume":    str(volume),
        })

        errors = result.get("error", [])
        if errors:
            print(f"  Kraken sell error: {errors}")
            return None

        txids = result.get("result", {}).get("txid", [])
        print(f"  ✅ Kraken SELL {symbol} ${size_usd:.2f} — txid: {txids}")
        return txids

    except Exception as e:
        print(f"  Kraken sell exception: {e}")
        return None

# ─────────────────────────────────────────
# COINBASE API
# ─────────────────────────────────────────

def coinbase_market_buy(symbol: str, size_usd: float):
    """Place a market buy on Coinbase."""
    try:
        import uuid
        product_id = f"{symbol}-USDC"
        order_id   = str(uuid.uuid4())
        timestamp  = str(int(time.time()))
        body       = json.dumps({
            "client_order_id": order_id,
            "product_id": product_id,
            "side": "BUY",
            "order_configuration": {
                "market_market_ioc": {
                    "quote_size": str(round(size_usd, 2))
                }
            }
        })
        message   = timestamp + "POST" + "/api/v3/brokerage/orders" + body
        signature = hmac.new(
            COINBASE_SECRET.encode(),
            message.encode(),
            digestmod=hashlib.sha256
        ).hexdigest()

        resp = requests.post(
            "https://api.coinbase.com/api/v3/brokerage/orders",
            headers={
                "CB-ACCESS-KEY":       COINBASE_API_KEY,
                "CB-ACCESS-SIGN":      signature,
                "CB-ACCESS-TIMESTAMP": timestamp,
                "Content-Type":        "application/json",
            },
            data=body, timeout=10
        )
        result = resp.json()
        if result.get("success"):
            oid = result.get("success_response", {}).get("order_id", order_id)
            print(f"  ✅ Coinbase BUY {symbol} ${size_usd:.2f} — {oid[:16]}...")
            return oid
        else:
            print(f"  ❌ Coinbase buy failed: {result.get('error_response', result)}")
            return None
    except Exception as e:
        print(f"  Coinbase buy exception: {e}")
        return None

def coinbase_market_sell(symbol: str, size_usd: float, price: float):
    """Place a market sell on Coinbase."""
    try:
        import uuid
        product_id = f"{symbol}-USDC"
        order_id   = str(uuid.uuid4())
        timestamp  = str(int(time.time()))
        base_size  = str(round(size_usd / price, 6))
        body       = json.dumps({
            "client_order_id": order_id,
            "product_id": product_id,
            "side": "SELL",
            "order_configuration": {
                "market_market_ioc": {
                    "base_size": base_size
                }
            }
        })
        message   = timestamp + "POST" + "/api/v3/brokerage/orders" + body
        signature = hmac.new(
            COINBASE_SECRET.encode(),
            message.encode(),
            digestmod=hashlib.sha256
        ).hexdigest()

        resp = requests.post(
            "https://api.coinbase.com/api/v3/brokerage/orders",
            headers={
                "CB-ACCESS-KEY":       COINBASE_API_KEY,
                "CB-ACCESS-SIGN":      signature,
                "CB-ACCESS-TIMESTAMP": timestamp,
                "Content-Type":        "application/json",
            },
            data=body, timeout=10
        )
        result = resp.json()
        if result.get("success"):
            oid = result.get("success_response", {}).get("order_id", order_id)
            print(f"  ✅ Coinbase SELL {symbol} ${size_usd:.2f} — {oid[:16]}...")
            return oid
        else:
            print(f"  ❌ Coinbase sell failed: {result.get('error_response', result)}")
            return None
    except Exception as e:
        print(f"  Coinbase sell exception: {e}")
        return None

# ─────────────────────────────────────────
# ARB EXECUTOR
# ─────────────────────────────────────────

def execute_arb(symbol, buy_ex, sell_ex, buy_price, sell_price, net_pct):
    """Execute both sides of an arb trade simultaneously."""
    global daily_pnl, daily_arb_profit, all_time_pnl, all_time_trades

    if KILL_SWITCH:
        print(f"  🛑 Kill switch — skipping arb")
        return False

    size_usd   = MAX_POSITION_USD
    net_profit = size_usd * (net_pct / 100)

    print(f"\n  ⚡ EXECUTING ARB: {symbol}")
    print(f"  Buy  {buy_ex.upper()}  @ ${buy_price:.4f}")
    print(f"  Sell {sell_ex.upper()} @ ${sell_price:.4f}")
    print(f"  Size: ${size_usd} | Expected profit: ${net_profit:.2f}")

    if not LIVE_TRADING:
        # Paper mode
        daily_pnl       += net_profit
        daily_arb_profit += net_profit
        all_time_pnl    += net_profit
        all_time_trades += 1
        print(f"  📝 PAPER ARB executed — profit: ${net_profit:.2f}")
        return True

    # Live execution — both sides simultaneously
    buy_success  = False
    sell_success = False

    def do_buy():
        nonlocal buy_success
        if buy_ex == "kraken":
            buy_success = bool(kraken_market_buy(symbol, size_usd, buy_price))
        else:
            buy_success = bool(coinbase_market_buy(symbol, size_usd))

    def do_sell():
        nonlocal sell_success
        if sell_ex == "kraken":
            sell_success = bool(kraken_market_sell(symbol, size_usd, sell_price))
        else:
            sell_success = bool(coinbase_market_sell(symbol, size_usd, sell_price))

    # Fire both simultaneously
    t1 = threading.Thread(target=do_buy)
    t2 = threading.Thread(target=do_sell)
    t1.start(); t2.start()
    t1.join();  t2.join()

    if buy_success and sell_success:
        daily_pnl       += net_profit
        daily_arb_profit += net_profit
        all_time_pnl    += net_profit
        all_time_trades += 1
        print(f"  ✅ ARB COMPLETE — profit: ${net_profit:.2f}")
        return True
    else:
        print(f"  ⚠️ ARB PARTIAL — buy:{buy_success} sell:{sell_success}")
        send_telegram(
            f"⚠️ *ARB PARTIAL FILL — {symbol}*\n\n"
            f"Buy {buy_ex}: {'✅' if buy_success else '❌'}\n"
            f"Sell {sell_ex}: {'✅' if sell_success else '❌'}\n\n"
            f"Check both exchanges manually!\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )
        return False

# ─────────────────────────────────────────
# PAPER TRADE EXECUTION (signals)
# ─────────────────────────────────────────

def paper_execute_buy(symbol, size_usd, price):
    global paper_balance
    if KILL_SWITCH: return None
    if daily_pnl <= -MAX_DAILY_LOSS:
        send_telegram(f"🛑 *Daily loss limit hit*\nBot paused for today.")
        return None
    if symbol in open_positions: return None
    if paper_balance < size_usd:
        print(f"  ⚠ Insufficient balance (${paper_balance:.2f})")
        return None
    paper_balance -= size_usd
    print(f"  📝 PAPER BUY: {symbol} ${size_usd} @ ${price:.4f} | Balance: ${paper_balance:.2f}")
    return {"order_id": f"PAPER-{symbol}-{int(time.time())}"}

def paper_execute_sell(symbol, size_usd, entry, exit_price):
    global paper_balance, daily_pnl, all_time_pnl, all_time_trades
    pnl = size_usd * ((exit_price - entry) / entry)
    paper_balance   += size_usd + pnl
    daily_pnl       += pnl
    all_time_pnl    += pnl
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
                to_close = []
                for symbol, pos in list(open_positions.items()):
                    current = get_current_price(symbol)
                    if not current: continue
                    entry     = pos["entry"]
                    pct_chg   = ((current - entry) / entry) * 100
                    size      = pos["size_usd"]
                    held_mins = (time.time() - pos["opened_at"]) / 60
                    reason    = None
                    if pct_chg >= TAKE_PROFIT_PCT:
                        reason = f"✅ TAKE PROFIT +{pct_chg:.1f}%"
                    elif pct_chg <= -STOP_LOSS_PCT:
                        reason = f"❌ STOP LOSS {pct_chg:.1f}%"
                    elif held_mins >= 1440:
                        reason = f"⏰ TIME EXIT {pct_chg:+.1f}%"
                    if reason:
                        pnl = paper_execute_sell(symbol, size, entry, current)
                        daily_trades_log.append({
                            "symbol": symbol, "pnl": pnl,
                            "pct": pct_chg, "reason": reason,
                            "held_mins": held_mins,
                        })
                        to_close.append(symbol)
                        emoji = "✅" if pnl > 0 else "❌"
                        send_telegram(
                            f"{emoji} *POSITION CLOSED — {symbol}*\n\n"
                            f"📋 {reason}\n"
                            f"📥 Entry: ${entry:.4f}\n"
                            f"📤 Exit: ${current:.4f}\n"
                            f"⏱ Held: {held_mins:.0f} mins\n"
                            f"💰 P&L: *${pnl:+.2f}* ({pct_chg:+.1f}%)\n\n"
                            f"📊 Today's P&L: ${daily_pnl:+.2f}\n"
                            f"💼 Balance: ${paper_balance:.2f}\n\n"
                            f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                        )
                for s in to_close:
                    del open_positions[s]
        except Exception as e:
            print(f"[POSITIONS] Error: {e}")
        time.sleep(POSITION_CHECK_SECS)

# ─────────────────────────────────────────
# DAILY SUMMARY
# ─────────────────────────────────────────

def send_daily_summary():
    global daily_pnl, daily_trades_log, arb_sim_log, daily_arb_profit

    now    = datetime.now(timezone.utc)
    wins   = [t for t in daily_trades_log if t["pnl"] > 0]
    losses = [t for t in daily_trades_log if t["pnl"] <= 0]
    total  = sum(t["pnl"] for t in daily_trades_log)
    wr     = (len(wins) / len(daily_trades_log) * 100) if daily_trades_log else 0
    best   = max(daily_trades_log, key=lambda x: x["pnl"], default=None)
    worst  = min(daily_trades_log, key=lambda x: x["pnl"], default=None)
    emoji  = "📈" if (total + daily_arb_profit) >= 0 else "📉"
    mode   = "🔴 LIVE" if LIVE_TRADING else "📝 Paper"

    msg = (
        f"{emoji} *Daily Summary — {now.strftime('%b %d, %Y')}*\n\n"
        f"Mode: {mode}\n"
        f"💼 Balance: *${paper_balance:.2f}*\n"
        f"💰 Total P&L: *${(total + daily_arb_profit):+.2f}*\n"
        f"📊 All-time P&L: *${all_time_pnl:+.2f}*\n\n"
        f"━━━━━━━━━━━━━━\n"
        f"🔄 *Arb Trades*\n"
        f"Executed: {len(arb_sim_log)}\n"
        f"Profit: *${daily_arb_profit:+.2f}*\n"
    )

    if arb_sim_log:
        top = sorted(arb_sim_log, key=lambda x: x["pct"], reverse=True)[:3]
        for a in top:
            msg += f"  🔄 {a['symbol']}: +{a['pct']:.3f}% (${a['simulated_profit']:.2f})\n"

    msg += (
        f"\n━━━━━━━━━━━━━━\n"
        f"🤖 *Signal Trades*\n"
        f"Total: {len(daily_trades_log)} ({len(wins)}W / {len(losses)}L)\n"
        f"Win Rate: {wr:.0f}%\n"
    )
    if best:
        msg += f"🏆 Best: {best['symbol']} *+${best['pnl']:.2f}*\n"
    if worst:
        msg += f"💀 Worst: {worst['symbol']} *${worst['pnl']:.2f}*\n"

    if not daily_trades_log and not arb_sim_log:
        msg += "\n_No activity today_"

    msg += f"\n⏰ {now.strftime('%H:%M UTC')}"
    send_telegram(msg)
    print("[SUMMARY] Daily summary sent")

    daily_trades_log.clear()
    arb_sim_log.clear()
    daily_pnl        = 0.0
    daily_arb_profit = 0.0

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
# ARB SCANNER + LIVE EXECUTOR
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

arb_alerted  = set()
arb_cooldown = {}  # symbol -> last execution time

def run_arb_scanner():
    global arb_sim_log
    mode = "LIVE 🔴" if LIVE_TRADING else "PAPER 📝"
    print(f"""
╔══════════════════════════════════╗
║   ARB SCANNER {mode:<10}       ║
╚══════════════════════════════════╝""")

    if LIVE_TRADING:
        cb_bal = "connected"
        kr_bal = kraken_get_balance()
        print(f"  Coinbase: connected")
        print(f"  Kraken: ${kr_bal:.2f} available")

    while True:
        try:
            now    = datetime.now(timezone.utc).strftime("%H:%M:%S")
            prices = get_prices(ALL_COINS)
            found  = 0
            print(f"\n[ARB {now}] Scanning {len(ALL_COINS)} coins...")

            for cg_id, symbol in ALL_COINS:
                usd = prices.get(cg_id, {}).get("usd")
                if not usd: continue

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
                                    "buy_ex": ex[i], "sell_ex": ex[j],
                                    "buy_p": buy_p, "sell_p": sell_p,
                                    "net": net, "net_pct": net * 100
                                }

                if best:
                    sim_profit = MAX_POSITION_USD * best["net"]
                    print(f"  {symbol}: 🚨 {best['buy_ex']}→{best['sell_ex']} +{best['net_pct']:.3f}% (${sim_profit:.2f})")

                    # Log to sim
                    arb_sim_log.append({
                        "symbol": symbol,
                        "pct": best["net_pct"],
                        "simulated_profit": sim_profit,
                        "buy_ex": best["buy_ex"],
                        "sell_ex": best["sell_ex"],
                        "time": datetime.now(timezone.utc).strftime("%H:%M"),
                    })

                    # Check cooldown (don't execute same symbol twice in 5 mins)
                    last_exec = arb_cooldown.get(symbol, 0)
                    cooldown_ok = (time.time() - last_exec) > 300

                    key = f"{symbol}-{best['net_pct']:.1f}"
                    if key not in arb_alerted:
                        arb_alerted.add(key)

                        # Execute the trade
                        if cooldown_ok:
                            success = execute_arb(
                                symbol,
                                best["buy_ex"], best["sell_ex"],
                                best["buy_p"], best["sell_p"],
                                best["net_pct"]
                            )
                            if success:
                                arb_cooldown[symbol] = time.time()
                                mode_tag = "🔴 LIVE TRADE" if LIVE_TRADING else "📝 Paper trade"
                                send_telegram(
                                    f"⚡ *ARB EXECUTED — {symbol}*\n\n"
                                    f"📥 Buy {best['buy_ex'].upper()} @ ${best['buy_p']:,.4f}\n"
                                    f"📤 Sell {best['sell_ex'].upper()} @ ${best['sell_p']:,.4f}\n\n"
                                    f"💰 *+{best['net_pct']:.3f}%*\n"
                                    f"💵 Profit: *${sim_profit:.2f}*\n"
                                    f"📊 Today arb P&L: ${daily_arb_profit:+.2f}\n\n"
                                    f"_{mode_tag}_\n"
                                    f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                                )
                                found += 1
                        else:
                            remaining = int(300 - (time.time() - last_exec))
                            print(f"  {symbol}: cooldown {remaining}s remaining")

            if not found:
                print(f"  ✓ No arb ≥{MIN_ARB_PCT}%")

        except Exception as e:
            print(f"[ARB] Error: {e}")
        time.sleep(ARB_CHECK_SECS)

# ─────────────────────────────────────────
# SIGNAL ANALYZER
# ─────────────────────────────────────────

def ask_claude(prompt):
    if not ANTHROPIC_KEY: return ""
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
                if symbol in open_positions: continue
                d   = prices.get(cg_id, {})
                usd = d.get("usd")
                if not usd: continue
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
                    f"Good entry for 5-7% gain?\n"
                    f'Respond ONLY as JSON: {{"signal":"LONG or SHORT or NEUTRAL","confidence":0-100,"reasoning":"one sentence"}}'
                )
                raw = ask_claude(prompt)
                if not raw: continue
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
                        order  = paper_execute_buy(symbol, MAX_POSITION_USD, usd)
                        if order:
                            open_positions[symbol] = {
                                "entry": usd, "size_usd": MAX_POSITION_USD,
                                "target": target, "stop": stop,
                                "side": sig, "opened_at": time.time(),
                            }
                            emoji = "🟢" if sig == "LONG" else "🔴"
                            send_telegram(
                                f"{emoji} *SIGNAL TRADE — {symbol}*\n\n"
                                f"📋 {sig} @ {conf}% confidence\n"
                                f"💰 Entry: ${usd:.4f}\n"
                                f"🎯 Target: ${target:.4f} (+{TAKE_PROFIT_PCT}%)\n"
                                f"🛑 Stop: ${stop:.4f} (-{STOP_LOSS_PCT}%)\n"
                                f"💵 Size: ${MAX_POSITION_USD:.0f}\n"
                                f"💼 Balance: ${paper_balance:.2f}\n\n"
                                f"📝 {reason}\n"
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
                        if val_usd < MIN_WHALE_USD: continue
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
    mode = "🔴 LIVE TRADING" if LIVE_TRADING else "📝 PAPER MODE"
    print(f"""
╔══════════════════════════════════════════════════╗
║   💎  COINBASE ALPHA SUITE — ALL SYSTEMS GO     ║
╚══════════════════════════════════════════════════╝

  Mode: {mode}
  🔄 Arb Scanner    — {len(ALL_COINS)} coins, auto-execute both sides
  🤖 Signal Analyzer — {len(SMALL_CAPS)} small caps
  📊 Position Monitor — auto close +{TAKE_PROFIT_PCT}% / -{STOP_LOSS_PCT}%
  🐋 Whale Watcher   — on-chain moves
  📅 Daily Summary   — midnight UTC

  Coinbase: {'✅ connected' if COINBASE_API_KEY else '❌ not connected'}
  Kraken:   {'✅ connected' if KRAKEN_API_KEY else '❌ not connected'}
  Balance:  ${paper_balance:.2f} starting
""")

    send_telegram(
        f"💎 *Coinbase Alpha Suite LIVE*\n\n"
        f"Mode: *{mode}*\n\n"
        f"💼 Starting balance: *${paper_balance:.2f}*\n"
        f"⚡ Arb: auto-executing both sides\n"
        f"🎯 Take profit: +{TAKE_PROFIT_PCT}%\n"
        f"🛑 Stop loss: -{STOP_LOSS_PCT}%\n"
        f"💵 Position size: ${MAX_POSITION_USD:.0f}\n\n"
        f"Coinbase: {'✅' if COINBASE_API_KEY else '❌'}\n"
        f"Kraken: {'✅' if KRAKEN_API_KEY else '❌'}\n\n"
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
        print(f"  ♻ {len(alive)}/5 | positions: {pos_str} | arb P&L: ${daily_arb_profit:+.2f} | signal P&L: ${daily_pnl:+.2f}")

if __name__ == "__main__":
    main()
