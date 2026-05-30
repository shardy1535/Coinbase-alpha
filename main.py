"""
COINBASE ALPHA SUITE v4 — DUAL EXCHANGE MOMENTUM TRADING
Trades independently on Coinbase AND Kraken simultaneously
No arb, no coordination — just momentum signals on each platform
"""

import os, time, threading, requests, json, hmac, hashlib, base64, secrets, urllib.parse
from datetime import datetime, timezone

try:
    import jwt as pyjwt
    JWT_OK = True
except ImportError:
    JWT_OK = False
    print("WARNING: PyJWT missing")

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT     = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_KEY     = os.getenv("ANTHROPIC_API_KEY", "")
ETHERSCAN_KEY     = os.getenv("ETHERSCAN_API_KEY", "")
CB_API_KEY        = os.getenv("COINBASE_API_KEY", "")
CB_SECRET         = os.getenv("COINBASE_API_SECRET", "")
KR_API_KEY        = os.getenv("KRAKEN_API_KEY", "")
KR_SECRET         = os.getenv("KRAKEN_API_SECRET", "")

MIN_CONFIDENCE    = int(os.getenv("MIN_AI_CONFIDENCE", "55"))
MAX_DAILY_LOSS    = float(os.getenv("MAX_DAILY_LOSS_USD", "100"))
KILL_SWITCH       = os.getenv("KILL_SWITCH", "false").lower() == "true"
SIGNAL_SECS       = int(os.getenv("SIGNAL_CHECK_SECONDS", "60"))
POSITION_SECS     = int(os.getenv("POSITION_CHECK_SECONDS", "60"))

# ─────────────────────────────────────────
# COINBASE COINS (traded on Coinbase)
# ─────────────────────────────────────────
COINBASE_COINS = [
    ("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL"),
    ("ripple", "XRP"), ("cardano", "ADA"), ("avalanche-2", "AVAX"),
    ("chainlink", "LINK"), ("polkadot", "DOT"), ("dogecoin", "DOGE"),
    ("uniswap", "UNI"), ("litecoin", "LTC"), ("stellar", "XLM"),
    ("cosmos", "ATOM"), ("filecoin", "FIL"), ("algorand", "ALGO"),
    ("aave", "AAVE"), ("the-graph", "GRT"), ("near", "NEAR"),
    ("fantom", "FTM"), ("injective-protocol", "INJ"), ("sui", "SUI"),
    ("arbitrum", "ARB"), ("optimism", "OP"), ("aptos", "APT"),
    ("render-token", "RNDR"), ("fetch-ai", "FET"), ("immutable-x", "IMX"),
    ("loopring", "LRC"), ("storj", "STORJ"), ("ankr", "ANKR"),
    ("mina-protocol", "MINA"), ("balancer", "BAL"), ("bancor", "BNT"),
]

# ─────────────────────────────────────────
# KRAKEN COINS (traded on Kraken)
# ─────────────────────────────────────────
KRAKEN_COINS = [
    ("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL"),
    ("ripple", "XRP"), ("cardano", "ADA"), ("avalanche-2", "AVAX"),
    ("chainlink", "LINK"), ("polkadot", "DOT"), ("dogecoin", "DOGE"),
    ("litecoin", "LTC"), ("stellar", "XLM"), ("cosmos", "ATOM"),
    ("filecoin", "FIL"), ("algorand", "ALGO"), ("aave", "AAVE"),
    ("the-graph", "GRT"), ("monero", "XMR"), ("eos", "EOS"),
    ("tezos", "XTZ"), ("maker", "MKR"), ("compound-governance-token", "COMP"),
    ("curve-dao-token", "CRV"), ("synthetix-network-token", "SNX"),
    ("1inch", "1INCH"), ("ocean-protocol", "OCEAN"), ("near", "NEAR"),
    ("mina-protocol", "MINA"), ("balancer", "BAL"), ("bancor", "BNT"),
    ("numeraire", "NMR"), ("melon", "MLN"), ("oasis-network", "ROSE"),
    ("api3", "API3"), ("perpetual-protocol", "PERP"), ("uma", "UMA"),
    ("loopring", "LRC"), ("storj", "STORJ"), ("ankr", "ANKR"),
    ("kava", "KAVA"), ("celo", "CELO"), ("band-protocol", "BAND"),
]

ALL_COINS = list({sym: cg for cg, sym in COINBASE_COINS + KRAKEN_COINS}.items())
ALL_COINS = [(v, k) for k, v in ALL_COINS]

# ─────────────────────────────────────────
# STATE
# ─────────────────────────────────────────
cb_positions    = {}   # Coinbase open positions
kr_positions    = {}   # Kraken open positions
daily_pnl_cb    = 0.0
daily_pnl_kr    = 0.0
all_time_pnl    = 0.0
all_time_trades = 0
daily_trades    = []
cb_balance      = 400.0  # tracked paper balance
kr_balance      = 400.0

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
                    "include_24hr_change": "true",
                    "include_1h_change": "true",
                    "include_market_cap": "true"},
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

mcap_cache = {}

def get_dynamic_settings(symbol, market_cap=0):
    """Returns tp%, sl%, position_size, max_hold_minutes based on market cap."""
    if market_cap >= 10_000_000_000:
        return 6.0, 3.0, 100.0, 1440   # large cap
    elif market_cap >= 1_000_000_000:
        return 5.0, 2.5, 100.0, 240    # mid cap
    elif market_cap >= 100_000_000:
        return 4.0, 2.0, 75.0,  120    # small cap
    else:
        return 7.0, 3.0, 50.0,  30     # micro cap

# ─────────────────────────────────────────
# COINBASE API (JWT auth)
# ─────────────────────────────────────────

def cb_headers(method, path):
    if not JWT_OK or not CB_API_KEY or not CB_SECRET:
        return {}
    try:
        uri     = f"{method} api.coinbase.com{path}"
        private = CB_SECRET.replace("\\n", "\n")
        token   = pyjwt.encode(
            {"sub": CB_API_KEY, "iss": "cdp",
             "nbf": int(time.time()), "exp": int(time.time()) + 120,
             "uri": uri},
            private, algorithm="ES256",
            headers={"kid": CB_API_KEY, "nonce": secrets.token_hex(10)},
        )
        return {"Authorization": f"Bearer {token}",
                "Content-Type": "application/json"}
    except Exception as e:
        print(f"  CB JWT error: {e}")
        return {}

def cb_place_order(symbol, side, size_usd, price=None):
    """Place real order on Coinbase. Returns order_id or None."""
    import uuid
    if not CB_API_KEY:
        print(f"  📝 CB PAPER {side} {symbol} ${size_usd}")
        return f"PAPER-CB-{symbol}-{int(time.time())}"
    try:
        path = "/api/v3/brokerage/orders"
        # Try USD first, then USDC
        for quote in ["USD", "USDC"]:
            if side == "BUY":
                cfg = {"quote_size": str(round(size_usd, 2))}
            else:
                if not price or price == 0: return None
                # For sells, use base_size (number of coins)
                base_amt = round(size_usd / price, 6)
                cfg = {"base_size": str(base_amt)}

            body = json.dumps({
                "client_order_id": str(uuid.uuid4()),
                "product_id": f"{symbol}-{quote}",
                "side": side,
                "order_configuration": {"market_market_ioc": cfg}
            })
            hdrs = cb_headers("POST", path)
            if not hdrs:
                print(f"  ❌ CB auth failed")
                return None

            resp   = requests.post(f"https://api.coinbase.com{path}",
                                   headers=hdrs, data=body, timeout=10)
            result = resp.json()

            if result.get("success"):
                oid = result.get("success_response", {}).get("order_id", "")
                print(f"  ✅ CB {side} {symbol}-{quote} ${size_usd:.2f} | {oid[:16]}...")
                return oid
            else:
                err = result.get("error_response", {})
                preview = result.get("preview_failure_reason", "")
                print(f"  ⚠ CB {side} {symbol}-{quote} failed: {err.get('message','')[:50]} {preview}")
                # Try next quote currency

        print(f"  ❌ CB {side} {symbol} failed on both USD and USDC")
        return None
    except Exception as e:
        print(f"  ❌ CB order exception: {e}")
        return None

def cb_get_balance():
    """Get available USD/USDC balance on Coinbase."""
    if not CB_API_KEY: return 400.0
    try:
        path  = "/api/v3/brokerage/accounts"
        hdrs  = cb_headers("GET", path)
        resp  = requests.get(f"https://api.coinbase.com{path}",
                             headers=hdrs, timeout=10)
        data     = resp.json()
        accounts = data.get("accounts", [])
        total    = 0.0
        for a in accounts:
            currency = a.get("available_balance", {}).get("currency", "")
            value    = float(a.get("available_balance", {}).get("value", 0))
            # Catch USD, USDC, and any fiat account
            if currency in ("USD", "USDC") or a.get("type") == "ACCOUNT_TYPE_FIAT":
                total += value
                print(f"  CB account: {a.get('name','?')} {currency} ${value:.2f}")
        if total == 0:
            # Log all accounts for debugging
            print(f"  CB accounts found: {len(accounts)}")
            for a in accounts[:10]:
                print(f"    {a.get('name','?')} | type:{a.get('type','?')} | {a.get('available_balance',{})}")
            # Fallback — use configured starting balance
            print(f"  CB balance fallback: using $400 starting balance")
            return float(os.getenv("STARTING_BALANCE", "400"))
        return total
    except Exception as e:
        print(f"  CB balance error: {e}")
        return 0.0

# ─────────────────────────────────────────
# KRAKEN API
# ─────────────────────────────────────────

def kr_sign(urlpath, data):
    postdata  = urllib.parse.urlencode(data)
    encoded   = (str(data["nonce"]) + postdata).encode()
    message   = urlpath.encode() + hashlib.sha256(encoded).digest()
    mac       = hmac.new(base64.b64decode(KR_SECRET), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()

def kr_request(urlpath, data):
    data["nonce"] = str(int(time.time() * 1000))
    hdrs = {"API-Key": KR_API_KEY, "API-Sign": kr_sign(urlpath, data)}
    resp = requests.post(f"https://api.kraken.com{urlpath}",
                         headers=hdrs, data=data, timeout=10)
    return resp.json()

def kr_place_order(symbol, side, size_usd, price):
    """Place real order on Kraken. Returns txid or None."""
    if not KR_API_KEY:
        print(f"  📝 KR PAPER {side} {symbol} ${size_usd}")
        return f"PAPER-KR-{symbol}-{int(time.time())}"
    try:
        kb     = "XBT" if symbol == "BTC" else symbol
        pair   = f"X{kb}ZUSD" if kb == "XBT" else f"{kb}USD"
        volume = str(round(size_usd / price, 6))
        result = kr_request("/0/private/AddOrder", {
            "pair": pair, "type": side.lower(),
            "ordertype": "market", "volume": volume
        })
        errors = result.get("error", [])
        if errors:
            print(f"  ❌ KR {side} {symbol} error: {errors}")
            return None
        txids = result.get("result", {}).get("txid", [])
        print(f"  ✅ KR {side} {symbol} ${size_usd:.2f} | {txids}")
        return txids[0] if txids else None
    except Exception as e:
        print(f"  ❌ KR order exception: {e}")
        return None

def kr_get_balance():
    """Get available USD balance on Kraken."""
    if not KR_API_KEY: return 400.0
    try:
        result = kr_request("/0/private/Balance", {})
        bal = result.get("result", {})
        return float(bal.get("ZUSD", 0)) + float(bal.get("USD", 0))
    except Exception as e:
        print(f"  KR balance error: {e}")
        return 0.0

# ─────────────────────────────────────────
# AI SIGNAL
# ─────────────────────────────────────────

def ask_claude(symbol, price, ch1h, ch24, mcap, fg_val, fg_lb):
    if not ANTHROPIC_KEY: return "NEUTRAL", 0, ""
    try:
        cap_type = (
            "large cap" if mcap >= 10e9 else
            "mid cap"   if mcap >= 1e9  else
            "small cap" if mcap >= 100e6 else
            "micro cap"
        )
        prompt = (
            f"Crypto momentum trade {symbol}/USD ({cap_type}):\n"
            f"Price:${price:.4f} 1h:{ch1h:.2f}% 24h:{ch24:.2f}%\n"
            f"MarketCap:${mcap/1e6:.0f}M Fear&Greed:{fg_val}/100 ({fg_lb})\n\n"
            f"Good entry for momentum trade?\n"
            f'JSON only: {{"signal":"LONG or SHORT or NEUTRAL","confidence":0-100,"reasoning":"one sentence"}}'
        )
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 150,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=20
        )
        data = resp.json()
        if "content" not in data:
            return "NEUTRAL", 0, ""
        raw    = data["content"][0]["text"]
        s      = json.loads(raw.replace("```json","").replace("```","").strip())
        return s.get("signal","NEUTRAL"), s.get("confidence",0), s.get("reasoning","")
    except Exception as e:
        print(f"  Claude error: {e}")
        return "NEUTRAL", 0, ""

def get_fear_greed():
    try:
        resp = requests.get("https://api.alternative.me/fng/", timeout=8)
        d = resp.json()["data"][0]
        return int(d["value"]), d["value_classification"]
    except:
        return 50, "Neutral"

# ─────────────────────────────────────────
# POSITION MONITOR
# ─────────────────────────────────────────

def check_positions(positions, exchange, place_order_fn):
    global all_time_pnl, all_time_trades, daily_trades
    to_close = []

    for symbol, pos in list(positions.items()):
        cur = get_current_price(symbol)
        if not cur: continue

        entry = pos["entry"]
        pct   = ((cur - entry) / entry) * 100
        mins  = (time.time() - pos["opened_at"]) / 60
        tp, sl, _, max_mins = get_dynamic_settings(symbol, pos.get("mcap", 0))

        reason = None
        if pct >= tp:        reason = f"✅ TAKE PROFIT +{pct:.2f}%"
        elif pct <= -sl:     reason = f"❌ STOP LOSS {pct:.2f}%"
        elif mins >= max_mins: reason = f"⏰ TIME EXIT {pct:+.2f}% ({mins:.0f}m)"

        if reason:
            print(f"  [{exchange}] Closing {symbol}: {reason}")
            size     = pos["size_usd"]
            coin_qty = pos.get("coin_qty", size / cur)
            # Place real sell order using actual coin quantity
            order = place_order_fn(symbol, "SELL", size, cur)
            pnl   = size * (pct / 100)

            if order:
                all_time_pnl    += pnl
                all_time_trades += 1
                daily_trades.append({
                    "symbol": symbol, "exchange": exchange,
                    "pnl": pnl, "pct": pct, "reason": reason
                })
                to_close.append(symbol)
                send_telegram(
                    f"{'✅' if pnl>0 else '❌'} *CLOSED — {symbol}* [{exchange}]\n\n"
                    f"📋 {reason}\n"
                    f"📥 Entry: ${entry:.4f}\n"
                    f"📤 Exit: ${cur:.4f}\n"
                    f"⏱ Held: {mins:.0f} mins\n"
                    f"💰 P&L: *${pnl:+.2f}* ({pct:+.2f}%)\n"
                    f"📊 All-time: ${all_time_pnl:+.2f}\n"
                    f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                )
            else:
                print(f"  ⚠ [{exchange}] Sell order failed for {symbol} — retrying next cycle")

    for s in to_close:
        del positions[s]

def run_position_monitor():
    print("  ✅ Position monitor started")
    while True:
        try:
            if cb_positions:
                check_positions(cb_positions, "Coinbase", cb_place_order)
            if kr_positions:
                check_positions(kr_positions, "Kraken", 
                               lambda sym, side, size, price: kr_place_order(sym, side, size, price))
        except Exception as e:
            print(f"[POSITIONS] Error: {e}")
        time.sleep(30)  # check every 30 seconds

# ─────────────────────────────────────────
# SIGNAL ANALYZER — runs for each exchange
# ─────────────────────────────────────────

def analyze_and_trade(coin_list, positions, exchange, place_order_fn, balance_fn, prices, fg_val, fg_lb):
    """Analyze coins and place trades for one exchange."""
    balance = balance_fn()
    print(f"  [{exchange}] Balance: ${balance:.2f} | Open: {len(positions)}")

    for cg_id, symbol in coin_list:
        if symbol in positions:
            continue

        d    = prices.get(cg_id, {})
        usd  = d.get("usd")
        if not usd: continue

        ch24 = d.get("usd_24h_change", 0) or 0
        ch1h = d.get("usd_1h_change", 0) or 0
        mcap = d.get("usd_market_cap", 0) or 0

        # Skip low momentum
        if abs(ch1h) < 0.3 and abs(ch24) < 2.0:
            continue

        print(f"  [{exchange}] 🤖 {symbol} ${usd:.4f} (1h:{ch1h:+.1f}% 24h:{ch24:+.1f}%)")

        sig, conf, reason = ask_claude(symbol, usd, ch1h, ch24, mcap, fg_val, fg_lb)
        print(f"  [{exchange}] {symbol}: {sig} @ {conf}%")

        # Override — use pure momentum if Claude is too conservative
        if sig == "NEUTRAL" or conf < MIN_CONFIDENCE:
            # Force LONG if up 10%+ in 24h
            if ch24 >= 10.0:
                sig    = "LONG"
                conf   = 60
                reason = f"Pure momentum override: +{ch24:.1f}% 24h"
                print(f"  [{exchange}] {symbol}: MOMENTUM OVERRIDE → LONG")
            # Force SHORT if down 10%+ in 24h
            elif ch24 <= -10.0:
                sig    = "SHORT"
                conf   = 60
                reason = f"Pure momentum override: {ch24:.1f}% 24h"
                print(f"  [{exchange}] {symbol}: MOMENTUM OVERRIDE → SHORT")
            else:
                continue

        tp, sl, pos_size, max_mins = get_dynamic_settings(symbol, mcap)

        # Check we have enough balance
        if balance < pos_size:
            print(f"  [{exchange}] Insufficient balance ${balance:.2f} for ${pos_size} trade")
            continue

        if KILL_SWITCH:
            print(f"  🛑 Kill switch active")
            continue

        # Place real order
        order = place_order_fn(symbol, "BUY", pos_size, usd)

        if order:
            target = usd * (1 + tp/100) if sig == "LONG" else usd * (1 - tp/100)
            stop   = usd * (1 - sl/100) if sig == "LONG" else usd * (1 + sl/100)
            positions[symbol] = {
                "entry": usd, "size_usd": pos_size,
                "target": target, "stop": stop,
                "side": sig, "opened_at": time.time(),
                "mcap": mcap, "order_id": order,
            }
            cap_type = (
                "large cap" if mcap >= 10e9 else
                "mid cap"   if mcap >= 1e9  else
                "small cap" if mcap >= 100e6 else
                "micro cap"
            )
            send_telegram(
                f"{'🟢' if sig=='LONG' else '🔴'} *TRADE OPENED — {symbol}*\n"
                f"Exchange: *{exchange}*\n\n"
                f"{sig} @ {conf}% | {cap_type}\n"
                f"💰 Entry: ${usd:.4f}\n"
                f"🎯 Target: ${target:.4f} (+{tp}%)\n"
                f"🛑 Stop: ${stop:.4f} (-{sl}%)\n"
                f"⏱ Max hold: {max_mins} mins\n"
                f"💵 Size: ${pos_size:.0f}\n\n"
                f"📝 {reason}\n"
                f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
            )
            balance -= pos_size
        time.sleep(1)  # avoid rate limits

def run_signal_analyzer():
    print("""
╔══════════════════════════════════╗
║   SIGNAL ANALYZER 🤖            ║
║   Coinbase + Kraken independent  ║
╚══════════════════════════════════╝""")
    if not ANTHROPIC_KEY:
        print("  ⚠ No ANTHROPIC_API_KEY")
        return

    while True:
        try:
            now = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"\n[SIGNALS {now}]")
            fg_val, fg_lb = get_fear_greed()

            # Get prices for all coins
            all_coin_list = list({sym: cg for cg, sym in COINBASE_COINS + KRAKEN_COINS}.items())
            all_coin_list = [(v, k) for k, v in all_coin_list]
            prices = get_prices(all_coin_list)

            # Run for Coinbase
            print(f"  --- Coinbase ---")
            analyze_and_trade(
                COINBASE_COINS, cb_positions, "Coinbase",
                lambda sym, side, size, price: cb_place_order(sym, side, size, price),
                cb_get_balance, prices, fg_val, fg_lb
            )

            # Run for Kraken
            print(f"  --- Kraken ---")
            analyze_and_trade(
                KRAKEN_COINS, kr_positions, "Kraken",
                lambda sym, side, size, price: kr_place_order(sym, side, size, price),
                kr_get_balance, prices, fg_val, fg_lb
            )

        except Exception as e:
            print(f"[SIGNALS] Error: {e}")
        time.sleep(SIGNAL_SECS)

# ─────────────────────────────────────────
# DAILY SUMMARY
# ─────────────────────────────────────────

def run_daily_summary():
    global daily_trades, daily_pnl_cb, daily_pnl_kr
    last_date = None
    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.hour == 0 and now.minute == 0 and now.date() != last_date:
                last_date = now.date()
                wins   = [t for t in daily_trades if t["pnl"] > 0]
                losses = [t for t in daily_trades if t["pnl"] <= 0]
                total  = sum(t["pnl"] for t in daily_trades)
                wr     = (len(wins)/len(daily_trades)*100) if daily_trades else 0
                best   = max(daily_trades, key=lambda x: x["pnl"], default=None)

                msg = (
                    f"{'📈' if total>=0 else '📉'} *Daily Summary — {now.strftime('%b %d')}*\n\n"
                    f"💰 Today P&L: *${total:+.2f}*\n"
                    f"📊 All-time: *${all_time_pnl:+.2f}*\n"
                    f"🔢 Trades: {len(daily_trades)} ({len(wins)}W/{len(losses)}L)\n"
                    f"🎯 Win Rate: {wr:.0f}%\n"
                )
                if best:
                    msg += f"🏆 Best: {best['symbol']} [{best['exchange']}] *+${best['pnl']:.2f}*\n"
                if not daily_trades:
                    msg += "\n_No trades today_"
                msg += f"\n⏰ {now.strftime('%H:%M UTC')}"
                send_telegram(msg)
                daily_trades.clear()
        except Exception as e:
            print(f"[SUMMARY] Error: {e}")
        time.sleep(55)

# ─────────────────────────────────────────
# WHALE WATCHER
# ─────────────────────────────────────────

WHALE_ADDRESSES = {
    "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43": "Coinbase Hot Wallet",
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance Hot Wallet",
}
seen_txns = set()

def run_whale_watcher():
    print("  ✅ Whale watcher started")
    while True:
        try:
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
                        params={"module":"account","action":"txlist",
                                "address":address,"page":1,"offset":5,
                                "sort":"desc","apikey":ETHERSCAN_KEY},
                        timeout=10
                    ).json().get("result",[])
                    if not isinstance(txns, list): continue
                    for tx in txns:
                        if tx.get("hash") in seen_txns: continue
                        seen_txns.add(tx["hash"])
                        val_eth = int(tx.get("value",0))/1e18
                        val_usd = val_eth * eth_price
                        if val_usd < 500000: continue
                        d = "OUT 📤" if tx["from"].lower()==address.lower() else "IN 📥"
                        send_telegram(
                            f"🐋 *WHALE — {label}*\n{d} ${val_usd:,.0f}\n"
                            f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                        )
                except: pass
        except Exception as e:
            print(f"[WHALE] Error: {e}")
        time.sleep(60)

# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

def main():
    cb_bal = cb_get_balance()
    kr_bal = kr_get_balance()

    print(f"""
╔══════════════════════════════════════════════════╗
║   💎  COINBASE ALPHA SUITE v4 — DUAL EXCHANGE   ║
╚══════════════════════════════════════════════════╝
  Coinbase: {'✅' if CB_API_KEY else '❌'} | Balance: ${cb_bal:.2f}
  Kraken:   {'✅' if KR_API_KEY else '❌'} | Balance: ${kr_bal:.2f}
  Total:    ${cb_bal + kr_bal:.2f}
  Coinbase coins: {len(COINBASE_COINS)}
  Kraken coins:   {len(KRAKEN_COINS)}
  Min confidence: {MIN_CONFIDENCE}%
  Kill switch: {'🛑 ON' if KILL_SWITCH else '✅ OFF'}
""")

    send_telegram(
        f"💎 *Coinbase Alpha v4 — DUAL EXCHANGE*\n\n"
        f"Coinbase: {'✅' if CB_API_KEY else '❌'} ${cb_bal:.2f}\n"
        f"Kraken: {'✅' if KR_API_KEY else '❌'} ${kr_bal:.2f}\n"
        f"Total: *${cb_bal + kr_bal:.2f}*\n\n"
        f"🪙 CB coins: {len(COINBASE_COINS)}\n"
        f"🪙 KR coins: {len(KRAKEN_COINS)}\n"
        f"🧠 Min confidence: {MIN_CONFIDENCE}%\n\n"
        f"Both exchanges trading independently\n"
        f"No arb — pure momentum signals\n\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )

    threads = [
        threading.Thread(target=run_signal_analyzer, daemon=True, name="Signals"),
        threading.Thread(target=run_position_monitor, daemon=True, name="Positions"),
        threading.Thread(target=run_daily_summary,   daemon=True, name="Summary"),
        threading.Thread(target=run_whale_watcher,   daemon=True, name="Whale"),
    ]

    for t in threads:
        t.start()
        print(f"  ✅ Started: {t.name}")
        time.sleep(2)

    print("\n  All systems go — trading on both exchanges\n")

    while True:
        time.sleep(60)
        alive = [t.name for t in threads if t.is_alive()]
        cb_pos = len(cb_positions)
        kr_pos = len(kr_positions)
        print(f"  ♻ {len(alive)}/4 | CB:{cb_pos} pos | KR:{kr_pos} pos | P&L: ${all_time_pnl:+.2f}")

if __name__ == "__main__":
    main()
