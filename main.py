import os
import time
import threading
import requests
import json
import hmac
import hashlib
import base64
import secrets
import urllib.parse
from datetime import datetime, timezone

try:
    import jwt as pyjwt
    JWT_AVAILABLE = True
except ImportError:
    JWT_AVAILABLE = False

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

MIN_ARB_PCT         = float(os.getenv("MIN_ARB_PCT", "0.5"))
ARB_CHECK_SECS      = int(os.getenv("ARB_CHECK_SECONDS", "30"))
SIGNAL_CHECK_SECS   = int(os.getenv("SIGNAL_CHECK_SECONDS", "300"))
WHALE_CHECK_SECS    = int(os.getenv("WHALE_CHECK_SECONDS", "60"))
MIN_WHALE_USD       = float(os.getenv("MIN_WHALE_USD", "500000"))
MIN_CONFIDENCE      = int(os.getenv("MIN_AI_CONFIDENCE", "65"))
MAX_POSITION_USD    = float(os.getenv("MAX_POSITION_USD", "100"))
MAX_DAILY_LOSS      = float(os.getenv("MAX_DAILY_LOSS_USD", "100"))
KILL_SWITCH         = os.getenv("KILL_SWITCH", "false").lower() == "true"
TAKE_PROFIT_PCT     = float(os.getenv("TAKE_PROFIT_PCT", "6.0"))
STOP_LOSS_PCT       = float(os.getenv("STOP_LOSS_PCT", "3.0"))
POSITION_CHECK_SECS = int(os.getenv("POSITION_CHECK_SECONDS", "60"))
STARTING_BALANCE    = float(os.getenv("STARTING_BALANCE", "400"))
LIVE_TRADING        = bool(COINBASE_API_KEY and KRAKEN_API_KEY)

FEES = {"coinbase": 0.0010, "binance": 0.0010, "kraken": 0.0026}

ALL_POSSIBLE_COINS = [
    # Large caps
    ("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL"),
    ("ripple", "XRP"), ("cardano", "ADA"), ("avalanche-2", "AVAX"),
    ("chainlink", "LINK"), ("polkadot", "DOT"), ("dogecoin", "DOGE"),
    ("uniswap", "UNI"), ("litecoin", "LTC"), ("stellar", "XLM"),
    ("cosmos", "ATOM"), ("filecoin", "FIL"), ("algorand", "ALGO"),
    ("aave", "AAVE"), ("the-graph", "GRT"), ("dash", "DASH"),
    ("zcash", "ZEC"), ("monero", "XMR"), ("eos", "EOS"),
    ("tezos", "XTZ"), ("maker", "MKR"), ("compound-governance-token", "COMP"),
    ("curve-dao-token", "CRV"), ("synthetix-network-token", "SNX"),
    ("1inch", "1INCH"), ("ocean-protocol", "OCEAN"),
    # Mid caps
    ("near", "NEAR"), ("fantom", "FTM"), ("injective-protocol", "INJ"),
    ("sui", "SUI"), ("arbitrum", "ARB"), ("optimism", "OP"),
    ("celestia", "TIA"), ("sei-network", "SEI"), ("aptos", "APT"),
    ("render-token", "RNDR"), ("fetch-ai", "FET"), ("akash-network", "AKT"),
    ("band-protocol", "BAND"), ("kava", "KAVA"), ("celo", "CELO"),
    ("loopring", "LRC"), ("storj", "STORJ"), ("ankr", "ANKR"),
    ("iotex", "IOTX"), ("skale", "SKL"), ("cartesi", "CTSI"),
    ("gnosis", "GNO"), ("rari-governance-token", "RGT"),
    # Small caps verified on Kraken (conservative list)
    ("numeraire", "NMR"), ("melon", "MLN"), ("oasis-network", "ROSE"),
    ("api3", "API3"), ("balancer", "BAL"), ("bancor", "BNT"),
    ("immutable-x", "IMX"), ("mina-protocol", "MINA"),
    ("pax-gold", "PAXG"), ("perpetual-protocol", "PERP"),
    ("quant-network", "QNT"), ("uma", "UMA"),
    ("loopring", "LRC"), ("storj", "STORJ"), ("ankr", "ANKR"),
    ("kava", "KAVA"), ("celo", "CELO"), ("band-protocol", "BAND"),
]

LARGE_CAPS = []
SMALL_CAPS = []
ALL_COINS  = []

# ─────────────────────────────────────────
# STATE
# ─────────────────────────────────────────
open_positions   = {}
daily_pnl        = 0.0
paper_balance    = STARTING_BALANCE
daily_trades_log = []
arb_sim_log      = []
all_time_pnl     = 0.0
all_time_trades  = 0
daily_arb_profit = 0.0

# ─────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────

def send_telegram(msg):
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
        print(f"  Price error: {e}")
        return {}

def get_current_price(symbol):
    cg_map = {sym: cg for cg, sym in ALL_COINS}
    cg_id  = cg_map.get(symbol)
    if not cg_id: return None
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": cg_id, "vs_currencies": "usd"}, timeout=8
        )
        return resp.json().get(cg_id, {}).get("usd")
    except:
        return None

# ─────────────────────────────────────────
# PAIR DISCOVERY
# ─────────────────────────────────────────

def discover_verified_pairs():
    global LARGE_CAPS, SMALL_CAPS, ALL_COINS
    print("\n🔍 Auto-discovering verified pairs on both exchanges...")

    kraken_available   = set()
    coinbase_available = set()

    try:
        resp = requests.get("https://api.kraken.com/0/public/AssetPairs", timeout=15)
        for _, pd in resp.json().get("result", {}).items():
            if pd.get("quote") in ("ZUSD", "USD"):
                coin = pd.get("base","").lstrip("X").lstrip("Z")
                if coin == "XBT": coin = "BTC"
                kraken_available.add(coin)
        print(f"  Kraken: {len(kraken_available)} USD pairs")
    except Exception as e:
        print(f"  Kraken discovery error: {e}")

    # Coinbase known supported coins (hardcoded — their API requires auth for full list)
    coinbase_available = {
        # Large/mid caps
        "BTC","ETH","SOL","XRP","ADA","AVAX","LINK","DOT","DOGE","UNI",
        "LTC","XLM","ATOM","FIL","ALGO","AAVE","GRT","DASH","ZEC","XMR",
        "EOS","XTZ","MKR","COMP","CRV","SNX","1INCH","OCEAN","MATIC",
        "APE","CHZ","ENJ","MANA","SAND","AXS","BAT","ZRX","OMG","NEAR",
        "ICP","FTM","SHIB","PEPE","ARB","OP","SUI","SEI","TIA","INJ",
        "APT","RNDR","FET","BAND","KAVA","CELO","LRC","STORJ",
        "ANKR","NMR","MLN","ROSE","API3","BAL","BNT",
        "IMX","MINA","PAXG","PERP","QNT","UMA","JASMY",
    }
    print(f"  Coinbase: {len(coinbase_available)} known pairs")

    verified = []
    for cg_id, symbol in ALL_POSSIBLE_COINS:
        if symbol in kraken_available and symbol in coinbase_available:
            verified.append((cg_id, symbol))

    large = ["BTC","ETH"]
    LARGE_CAPS[:] = [(cg,sym) for cg,sym in verified if sym in large]
    SMALL_CAPS[:] = [(cg,sym) for cg,sym in verified if sym not in large]
    ALL_COINS[:]  = verified

    syms = [s for _,s in verified]
    print(f"  ✅ Verified {len(verified)} coins on both: {syms}")
    send_telegram(
        f"🔍 *Pair Discovery Complete*\n\n"
        f"✅ Verified on both exchanges: *{len(verified)} coins*\n"
        f"{', '.join(syms)}\n\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )
    return len(verified)

# ─────────────────────────────────────────
# KRAKEN API
# ─────────────────────────────────────────

def kraken_sign(urlpath, data):
    postdata  = urllib.parse.urlencode(data)
    encoded   = (str(data["nonce"]) + postdata).encode()
    message   = urlpath.encode() + hashlib.sha256(encoded).digest()
    mac       = hmac.new(base64.b64decode(KRAKEN_API_SECRET), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()

def kraken_request(urlpath, data):
    data["nonce"] = str(int(time.time() * 1000))
    headers = {"API-Key": KRAKEN_API_KEY, "API-Sign": kraken_sign(urlpath, data)}
    resp = requests.post(f"https://api.kraken.com{urlpath}",
                         headers=headers, data=data, timeout=10)
    return resp.json()

def kraken_order(symbol, side, size_usd, price):
    try:
        kb     = "XBT" if symbol == "BTC" else symbol
        pair   = f"X{kb}ZUSD" if kb == "XBT" else f"{kb}USD"
        volume = str(round(size_usd / price, 6))
        result = kraken_request("/0/private/AddOrder", {
            "pair": pair, "type": side, "ordertype": "market", "volume": volume
        })
        errors = result.get("error", [])
        if errors:
            print(f"  Kraken {side} error: {errors}")
            return None
        txids = result.get("result", {}).get("txid", [])
        print(f"  ✅ Kraken {side.upper()} {symbol} — {txids}")
        return txids
    except Exception as e:
        print(f"  Kraken {side} exception: {e}")
        return None

# ─────────────────────────────────────────
# COINBASE JWT API
# ─────────────────────────────────────────

def coinbase_headers(method, path):
    if not JWT_AVAILABLE:
        print("  ❌ PyJWT not available")
        return {}
    try:
        uri     = f"{method} api.coinbase.com{path}"
        private = COINBASE_SECRET.replace("\\n", "\n")
        token   = pyjwt.encode(
            {"sub": COINBASE_API_KEY, "iss": "cdp",
             "nbf": int(time.time()), "exp": int(time.time()) + 120, "uri": uri},
            private, algorithm="ES256",
            headers={"kid": COINBASE_API_KEY, "nonce": secrets.token_hex(10)},
        )
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    except Exception as e:
        print(f"  JWT error: {e}")
        return {}

def coinbase_order(symbol, side, size_usd, price=None):
    import uuid
    try:
        path = "/api/v3/brokerage/orders"
        cfg  = ({"quote_size": str(round(size_usd, 2))} if side == "BUY"
                else {"base_size": str(round(size_usd / price, 6))})
        body = json.dumps({
            "client_order_id": str(uuid.uuid4()),
            "product_id": f"{symbol}-USDC",
            "side": side,
            "order_configuration": {"market_market_ioc": cfg}
        })
        hdrs   = coinbase_headers("POST", path)
        if not hdrs: return None
        resp   = requests.post(f"https://api.coinbase.com{path}",
                               headers=hdrs, data=body, timeout=10)
        result = resp.json()
        if result.get("success"):
            oid = result.get("success_response", {}).get("order_id", "")
            print(f"  ✅ Coinbase {side} {symbol} — {oid[:16]}...")
            return oid
        print(f"  ❌ Coinbase {side} failed: {result.get('error_response', result)}")
        return None
    except Exception as e:
        print(f"  Coinbase {side} exception: {e}")
        return None

# ─────────────────────────────────────────
# ARB EXECUTION
# ─────────────────────────────────────────

def execute_arb(symbol, buy_ex, sell_ex, buy_price, sell_price, net_pct):
    global daily_pnl, daily_arb_profit, all_time_pnl, all_time_trades
    if KILL_SWITCH: return False
    size       = MAX_POSITION_USD
    net_profit = size * (net_pct / 100)

    if not LIVE_TRADING:
        daily_pnl       += net_profit
        daily_arb_profit += net_profit
        all_time_pnl    += net_profit
        all_time_trades += 1
        print(f"  📝 PAPER ARB {symbol} profit: ${net_profit:.2f}")
        return True

    buy_ok = sell_ok = False

    def do_buy():
        nonlocal buy_ok
        if buy_ex == "kraken":
            buy_ok = bool(kraken_order(symbol, "buy", size, buy_price))
        else:
            buy_ok = bool(coinbase_order(symbol, "BUY", size))

    def do_sell():
        nonlocal sell_ok
        if sell_ex == "kraken":
            sell_ok = bool(kraken_order(symbol, "sell", size, sell_price))
        else:
            sell_ok = bool(coinbase_order(symbol, "SELL", size, sell_price))

    t1 = threading.Thread(target=do_buy)
    t2 = threading.Thread(target=do_sell)
    t1.start(); t2.start()
    t1.join();  t2.join()

    if buy_ok and sell_ok:
        daily_pnl       += net_profit
        daily_arb_profit += net_profit
        all_time_pnl    += net_profit
        all_time_trades += 1
        return True
    else:
        # Silently remove this coin from verified list — not available on both
        if not buy_ok or not sell_ok:
            print(f"  ⚠ {symbol} failed on one side — removing from verified list")
            global ALL_COINS, SMALL_CAPS, LARGE_CAPS
            ALL_COINS[:]  = [(cg,sym) for cg,sym in ALL_COINS  if sym != symbol]
            SMALL_CAPS[:] = [(cg,sym) for cg,sym in SMALL_CAPS if sym != symbol]
            LARGE_CAPS[:] = [(cg,sym) for cg,sym in LARGE_CAPS if sym != symbol]
            # Add to permanent cooldown
            arb_cooldown[symbol] = time.time() + 86400  # 24hr cooldown
        return False

# ─────────────────────────────────────────
# PAPER TRADE HELPERS
# ─────────────────────────────────────────

def paper_buy(symbol, size_usd, price):
    global paper_balance
    if KILL_SWITCH or symbol in open_positions: return None
    if daily_pnl <= -MAX_DAILY_LOSS:
        send_telegram("🛑 *Daily loss limit hit* — bot paused")
        return None
    if paper_balance < size_usd:
        print(f"  ⚠ Low balance ${paper_balance:.2f}")
        return None
    paper_balance -= size_usd
    return {"id": f"PAPER-{symbol}-{int(time.time())}"}

def paper_sell(symbol, size_usd, entry, exit_price):
    global paper_balance, daily_pnl, all_time_pnl, all_time_trades
    pnl = size_usd * ((exit_price - entry) / entry)
    paper_balance   += size_usd + pnl
    daily_pnl       += pnl
    all_time_pnl    += pnl
    all_time_trades += 1
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
                    cur  = get_current_price(symbol)
                    if not cur: continue
                    pct  = ((cur - pos["entry"]) / pos["entry"]) * 100
                    mins = (time.time() - pos["opened_at"]) / 60
                    reason = None
                    if pct >= TAKE_PROFIT_PCT:   reason = f"✅ TAKE PROFIT +{pct:.1f}%"
                    elif pct <= -STOP_LOSS_PCT:  reason = f"❌ STOP LOSS {pct:.1f}%"
                    elif mins >= 1440:           reason = f"⏰ TIME EXIT {pct:+.1f}%"
                    if reason:
                        pnl = paper_sell(symbol, pos["size_usd"], pos["entry"], cur)
                        daily_trades_log.append({"symbol":symbol,"pnl":pnl,"pct":pct,"reason":reason})
                        to_close.append(symbol)
                        send_telegram(
                            f"{'✅' if pnl>0 else '❌'} *CLOSED — {symbol}*\n\n"
                            f"📋 {reason}\n"
                            f"📥 Entry: ${pos['entry']:.4f}\n"
                            f"📤 Exit: ${cur:.4f}\n"
                            f"💰 P&L: *${pnl:+.2f}* ({pct:+.1f}%)\n"
                            f"📊 Today: ${daily_pnl:+.2f} | Balance: ${paper_balance:.2f}\n"
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
    wr     = (len(wins)/len(daily_trades_log)*100) if daily_trades_log else 0
    best   = max(daily_trades_log, key=lambda x: x["pnl"], default=None)
    mode   = "🔴 LIVE" if LIVE_TRADING else "📝 Paper"

    msg = (
        f"{'📈' if (total+daily_arb_profit)>=0 else '📉'} "
        f"*Daily Summary — {now.strftime('%b %d, %Y')}*\n\n"
        f"Mode: {mode}\n"
        f"💼 Balance: *${paper_balance:.2f}*\n"
        f"💰 Total P&L: *${(total+daily_arb_profit):+.2f}*\n"
        f"📊 All-time: *${all_time_pnl:+.2f}*\n\n"
        f"━━━━━━━━━━\n"
        f"🔄 *Arb*: {len(arb_sim_log)} trades | *${daily_arb_profit:+.2f}*\n"
        f"🤖 *Signals*: {len(daily_trades_log)} ({len(wins)}W/{len(losses)}L) | WR:{wr:.0f}%\n"
    )
    if best: msg += f"🏆 Best: {best['symbol']} *+${best['pnl']:.2f}*\n"
    if not daily_trades_log and not arb_sim_log:
        msg += "\n_No activity today_"
    msg += f"\n⏰ {now.strftime('%H:%M UTC')}"
    send_telegram(msg)

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
# ARB SCANNER
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
    except: pass
    return None

def kraken_price(symbol):
    try:
        kb   = "XBT" if symbol == "BTC" else symbol
        pair = f"X{kb}ZUSD" if kb == "XBT" else f"{kb}USD"
        resp = requests.get("https://api.kraken.com/0/public/Ticker",
                            params={"pair": pair}, timeout=5)
        result = resp.json().get("result", {})
        if result:
            t = list(result.values())[0]
            return {"bid": float(t["b"][0]), "ask": float(t["a"][0])}
    except: pass
    return None

arb_alerted  = set()
arb_cooldown = {}

def run_arb_scanner():
    print("""
╔══════════════════════════════════╗
║   ARB SCANNER 🔄                ║
╚══════════════════════════════════╝""")
    while True:
        try:
            if not ALL_COINS:
                time.sleep(10)
                continue
            now    = datetime.now(timezone.utc).strftime("%H:%M:%S")
            prices = get_prices(ALL_COINS)
            found  = 0
            print(f"\n[ARB {now}] Scanning {len(ALL_COINS)} verified coins...")

            for cg_id, symbol in ALL_COINS:
                usd = prices.get(cg_id, {}).get("usd")
                if not usd: continue
                cg  = {"bid": usd*0.9995, "ask": usd*1.0005}
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
                        net    = ((sell_p-buy_p)/buy_p) - FEES[ex[i]] - FEES[ex[j]]
                        if net >= MIN_ARB_PCT/100:
                            if best is None or net > best["net"]:
                                best = {"buy":ex[i],"sell":ex[j],
                                        "buy_p":buy_p,"sell_p":sell_p,
                                        "net":net,"net_pct":net*100}
                if best:
                    sim  = MAX_POSITION_USD * best["net"]
                    key  = f"{symbol}-{best['net_pct']:.1f}"
                    cool = (time.time() - arb_cooldown.get(symbol,0)) > 300
                    print(f"  {symbol}: 🚨 {best['buy']}→{best['sell']} +{best['net_pct']:.3f}% (${sim:.2f})")
                    arb_sim_log.append({"symbol":symbol,"pct":best["net_pct"],"simulated_profit":sim})
                    if key not in arb_alerted and cool:
                        arb_alerted.add(key)
                        ok = execute_arb(symbol, best["buy"], best["sell"],
                                         best["buy_p"], best["sell_p"], best["net_pct"])
                        if ok:
                            arb_cooldown[symbol] = time.time()
                            mode_tag = "🔴 LIVE" if LIVE_TRADING else "📝 Paper"
                            send_telegram(
                                f"⚡ *ARB EXECUTED — {symbol}*\n\n"
                                f"📥 Buy {best['buy'].upper()} @ ${best['buy_p']:,.4f}\n"
                                f"📤 Sell {best['sell'].upper()} @ ${best['sell_p']:,.4f}\n"
                                f"💰 *+{best['net_pct']:.3f}%* = *${sim:.2f}*\n"
                                f"📊 Today arb: ${daily_arb_profit:+.2f}\n"
                                f"_{mode_tag}_\n"
                                f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                            )
                            found += 1
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
            headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01",
                     "content-type":"application/json"},
            json={"model":"claude-haiku-4-5-20251001","max_tokens":200,
                  "messages":[{"role":"user","content":prompt}]},
            timeout=20
        )
        data = resp.json()
        return data["content"][0]["text"] if "content" in data else ""
    except Exception as e:
        print(f"  Claude error: {e}")
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
        print("  ⚠ No API key — disabled")
        return
    while True:
        try:
            if not SMALL_CAPS:
                time.sleep(10)
                continue
            now = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"\n[SIGNALS {now}] Scanning {len(SMALL_CAPS)} coins...")
            prices = get_prices(SMALL_CAPS)
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
                raw = ask_claude(
                    f"Crypto momentum trade {symbol}/USD:\n"
                    f"Price:${usd:.4f} 1h:{ch1h:.2f}% 24h:{ch24:.2f}%\n"
                    f"Fear&Greed:{fg_val}/100 ({fg_lb})\n"
                    f"Good entry for 5-7% gain?\n"
                    f'JSON only: {{"signal":"LONG or SHORT or NEUTRAL","confidence":0-100,"reasoning":"one sentence"}}'
                )
                if not raw: continue
                try:
                    s      = json.loads(raw.replace("```json","").replace("```","").strip())
                    conf   = s.get("confidence", 0)
                    sig    = s.get("signal", "NEUTRAL")
                    reason = s.get("reasoning", "")
                    print(f"  {symbol}: {sig} @ {conf}%")
                    if sig != "NEUTRAL" and conf >= MIN_CONFIDENCE:
                        target = usd*(1+TAKE_PROFIT_PCT/100) if sig=="LONG" else usd*(1-TAKE_PROFIT_PCT/100)
                        stop   = usd*(1-STOP_LOSS_PCT/100)   if sig=="LONG" else usd*(1+STOP_LOSS_PCT/100)
                        order  = paper_buy(symbol, MAX_POSITION_USD, usd)
                        if order:
                            open_positions[symbol] = {
                                "entry":usd,"size_usd":MAX_POSITION_USD,
                                "target":target,"stop":stop,
                                "side":sig,"opened_at":time.time()
                            }
                            send_telegram(
                                f"{'🟢' if sig=='LONG' else '🔴'} *TRADE — {symbol}*\n\n"
                                f"{sig} @ {conf}%\n"
                                f"💰 Entry: ${usd:.4f}\n"
                                f"🎯 Target: ${target:.4f} (+{TAKE_PROFIT_PCT}%)\n"
                                f"🛑 Stop: ${stop:.4f} (-{STOP_LOSS_PCT}%)\n"
                                f"💵 ${MAX_POSITION_USD:.0f} | Balance: ${paper_balance:.2f}\n"
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
                eth_price = requests.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids":"ethereum","vs_currencies":"usd"},timeout=8
                ).json()["ethereum"]["usd"]
            except: eth_price = 3000
            for address, label in WHALE_ADDRESSES.items():
                try:
                    txns = requests.get(
                        "https://api.etherscan.io/api",
                        params={"module":"account","action":"txlist","address":address,
                                "page":1,"offset":5,"sort":"desc","apikey":ETHERSCAN_KEY},
                        timeout=10
                    ).json().get("result",[])
                    if not isinstance(txns, list):
                        print(f"  {label[:20]}: limit")
                        continue
                    new = [t for t in txns if t.get("hash") not in seen_txns]
                    if not new:
                        print(f"  {label[:20]}: quiet")
                        continue
                    for tx in new:
                        seen_txns.add(tx["hash"])
                        val_eth = int(tx.get("value",0))/1e18
                        val_usd = val_eth * eth_price
                        if val_usd < MIN_WHALE_USD: continue
                        d = "OUT 📤" if tx["from"].lower()==address.lower() else "IN 📥"
                        print(f"  {label[:20]}: 🚨 ${val_usd:,.0f} {d}")
                        send_telegram(
                            f"🐋 *WHALE ALERT*\n\n{d} *{label}*\n"
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
  Coinbase: {'✅' if COINBASE_API_KEY else '❌'}
  Kraken:   {'✅' if KRAKEN_API_KEY else '❌'}
  Balance:  ${paper_balance:.2f}
""")

    # Auto-discover verified pairs
    num = discover_verified_pairs()

    send_telegram(
        f"💎 *Coinbase Alpha Suite LIVE*\n\n"
        f"Mode: *{mode}*\n"
        f"💼 Balance: *${paper_balance:.2f}*\n"
        f"🔍 Verified pairs: *{num}*\n"
        f"💵 Position: ${MAX_POSITION_USD:.0f}\n"
        f"🎯 TP: +{TAKE_PROFIT_PCT}% | SL: -{STOP_LOSS_PCT}%\n"
        f"Coinbase: {'✅' if COINBASE_API_KEY else '❌'} | "
        f"Kraken: {'✅' if KRAKEN_API_KEY else '❌'}\n"
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

    counter = 0
    while True:
        time.sleep(60)
        alive   = [t.name for t in threads if t.is_alive()]
        pos_str = f"{len(open_positions)} open" if open_positions else "none"
        print(f"  ♻ {len(alive)}/5 | pos:{pos_str} | arb:${daily_arb_profit:+.2f} | signal:${daily_pnl:+.2f} | pairs:{len(ALL_COINS)}")
        counter += 1
        if counter >= 360:
            counter = 0
            discover_verified_pairs()

if __name__ == "__main__":
    main()
