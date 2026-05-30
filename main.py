"""
COINBASE ALPHA SUITE v5 — 5-MINUTE MOMENTUM SCANNER
Detects coins moving 0.5-3% in 5 minutes and trades them
Target: +5% profit, -1% stop loss
"""

import os, time, threading, requests, json, hmac, hashlib, base64, secrets, urllib.parse
from datetime import datetime, timezone
from collections import defaultdict

try:
    import jwt as pyjwt
    JWT_OK = True
except ImportError:
    JWT_OK = False

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT   = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
ETHERSCAN_KEY   = os.getenv("ETHERSCAN_API_KEY", "")
CB_API_KEY      = os.getenv("COINBASE_API_KEY", "")
CB_SECRET       = os.getenv("COINBASE_API_SECRET", "")
KR_API_KEY      = os.getenv("KRAKEN_API_KEY", "")
KR_SECRET       = os.getenv("KRAKEN_API_SECRET", "")

MIN_CONFIDENCE  = int(os.getenv("MIN_AI_CONFIDENCE", "45"))
MAX_DAILY_LOSS  = float(os.getenv("MAX_DAILY_LOSS_USD", "50"))
KILL_SWITCH     = os.getenv("KILL_SWITCH", "false").lower() == "true"
SCAN_SECS       = int(os.getenv("SCAN_SECONDS", "15"))        # scan every 15s
MOMENTUM_WINDOW = int(os.getenv("MOMENTUM_WINDOW", "5"))      # 5 minute window
MIN_MOVE_PCT    = float(os.getenv("MIN_MOVE_PCT", "0.5"))     # min 0.5% move
MAX_MOVE_PCT    = float(os.getenv("MAX_MOVE_PCT", "3.0"))     # max 3% (already ran)
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "7.0"))  # sell at +7%
STOP_LOSS_PCT   = float(os.getenv("STOP_LOSS_PCT", "1.0"))    # stop at -1%
MAX_HOLD_MINS   = int(os.getenv("MAX_HOLD_MINUTES", "120"))   # 2hr max hold
POSITION_USD    = float(os.getenv("MAX_POSITION_USD", "100")) # $100 per trade

# ─────────────────────────────────────────
# COINS
# ─────────────────────────────────────────
COINBASE_COINS = [
    ("bitcoin","BTC"),("ethereum","ETH"),("solana","SOL"),
    ("ripple","XRP"),("cardano","ADA"),("avalanche-2","AVAX"),
    ("chainlink","LINK"),("polkadot","DOT"),("dogecoin","DOGE"),
    ("uniswap","UNI"),("litecoin","LTC"),("stellar","XLM"),
    ("cosmos","ATOM"),("filecoin","FIL"),("algorand","ALGO"),
    ("aave","AAVE"),("the-graph","GRT"),("near","NEAR"),
    ("fantom","FTM"),("injective-protocol","INJ"),("sui","SUI"),
    ("arbitrum","ARB"),("optimism","OP"),("aptos","APT"),
    ("render-token","RNDR"),("fetch-ai","FET"),("immutable-x","IMX"),
    ("loopring","LRC"),("storj","STORJ"),("ankr","ANKR"),
    ("mina-protocol","MINA"),("balancer","BAL"),("bancor","BNT"),
]

KRAKEN_COINS = [
    ("bitcoin","BTC"),("ethereum","ETH"),("solana","SOL"),
    ("ripple","XRP"),("cardano","ADA"),("avalanche-2","AVAX"),
    ("chainlink","LINK"),("polkadot","DOT"),("dogecoin","DOGE"),
    ("litecoin","LTC"),("stellar","XLM"),("cosmos","ATOM"),
    ("filecoin","FIL"),("algorand","ALGO"),("aave","AAVE"),
    ("the-graph","GRT"),("monero","XMR"),("eos","EOS"),
    ("tezos","XTZ"),("maker","MKR"),("compound-governance-token","COMP"),
    ("curve-dao-token","CRV"),("synthetix-network-token","SNX"),
    ("1inch","1INCH"),("ocean-protocol","OCEAN"),("near","NEAR"),
    ("mina-protocol","MINA"),("balancer","BAL"),("bancor","BNT"),
    ("numeraire","NMR"),("api3","API3"),("uma","UMA"),
    ("loopring","LRC"),("storj","STORJ"),("ankr","ANKR"),
    ("kava","KAVA"),("celo","CELO"),("band-protocol","BAND"),
    ("oasis-network","ROSE"),("perpetual-protocol","PERP"),
    ("quant-network","QNT"),("injective-protocol","INJ"),
]

ALL_CG_IDS = list({cg: sym for cg, sym in COINBASE_COINS + KRAKEN_COINS}.items())

# ─────────────────────────────────────────
# STATE
# ─────────────────────────────────────────
cb_positions   = {}
kr_positions   = {}
price_history  = defaultdict(list)  # symbol -> [(timestamp, price), ...]
all_time_pnl   = 0.0
all_time_trades = 0
daily_trades   = []
daily_loss     = 0.0

# ─────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print(f"[TG] {msg[:100]}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        print(f"Telegram error: {e}")

def get_kraken_prices() -> dict:
    """Get real-time prices from Kraken — all coins in batches."""
    prices = {}
    # Build pair list
    pair_to_sym = {}
    pairs = []
    for _, symbol in KRAKEN_COINS:
        kb   = "XBT" if symbol == "BTC" else symbol
        pair = f"X{kb}ZUSD" if kb == "XBT" else f"{kb}USD"
        pairs.append(pair)
        pair_to_sym[pair] = symbol
        # Also map alternate formats Kraken uses
        pair_to_sym[f"{kb}USD"]  = symbol
        pair_to_sym[f"{kb}ZUSD"] = symbol
        pair_to_sym[f"X{kb}USD"] = symbol

    # Fetch in batches of 10
    for i in range(0, len(pairs), 10):
        batch = pairs[i:i+10]
        try:
            resp = requests.get(
                "https://api.kraken.com/0/public/Ticker",
                params={"pair": ",".join(batch)},
                timeout=8
            )
            result = resp.json().get("result", {})
            for pair_name, data in result.items():
                last_price = float(data["c"][0])
                # Try to match pair name to symbol
                sym = pair_to_sym.get(pair_name)
                if not sym:
                    # Try stripping X/Z prefixes
                    clean = pair_name.lstrip("X").lstrip("Z").rstrip("ZUSD").rstrip("USD")
                    if clean == "XBT": clean = "BTC"
                    sym = pair_to_sym.get(clean)
                if sym:
                    prices[sym] = last_price
        except Exception as e:
            print(f"  Kraken batch error: {e}")
        time.sleep(0.1)

    return prices



def get_all_prices() -> dict:
    """
    Bulletproof multi-source price fetcher.
    Sources: Kraken (real-time) + Binance (real-time) + CoinGecko (backup)
    Each source independent — failure of one doesn't affect others.
    """
    prices  = {}
    wanted  = {sym for _, sym in COINBASE_COINS + KRAKEN_COINS}
    cg_map  = {sym: cg for cg, sym in COINBASE_COINS + KRAKEN_COINS}

    # ── SOURCE 1: Kraken (real-time, best for our trading coins) ──
    try:
        pair_map = {}
        for _, sym in KRAKEN_COINS:
            kb   = "XBT" if sym == "BTC" else sym
            pair = f"X{kb}ZUSD" if kb == "XBT" else f"{kb}USD"
            pair_map[pair] = sym

        all_pairs = list(pair_map.keys())
        for i in range(0, len(all_pairs), 10):
            batch = all_pairs[i:i+10]
            try:
                resp   = requests.get(
                    "https://api.kraken.com/0/public/Ticker",
                    params={"pair": ",".join(batch)},
                    timeout=6
                )
                result = resp.json().get("result", {})
                for pair_name, data in result.items():
                    price = float(data["c"][0])
                    sym   = pair_map.get(pair_name)
                    if not sym:
                        for p, s in pair_map.items():
                            if pair_name.replace("X","").replace("Z","").startswith(
                               p.replace("X","").replace("Z","")[:3]):
                                sym = s
                                break
                    if sym and sym not in prices:
                        prices[sym] = price
            except:
                pass
            time.sleep(0.05)
    except Exception as e:
        print(f"  Kraken error: {e}")

    # ── SOURCE 2: Binance (real-time, individual calls for reliability) ──
    binance_syms = [s for s in wanted if s not in prices]
    binance_count = 0
    for sym in binance_syms[:25]:
        try:
            bn   = "XBT" if sym == "BTC" else sym
            resp = requests.get(
                "https://api.binance.com/api/v3/ticker/bookTicker",
                params={"symbol": f"{bn}USDT"},
                timeout=3
            )
            d = resp.json()
            if isinstance(d, dict) and "bidPrice" in d and "askPrice" in d:
                bid = float(d["bidPrice"])
                ask = float(d["askPrice"])
                if bid > 0 and ask > 0:
                    prices[sym] = (bid + ask) / 2
                    binance_count += 1
        except:
            pass

    # ── SOURCE 3: CoinGecko (backup for any remaining missing coins) ──
    missing = [s for s in wanted if s not in prices]
    cg_count = 0
    if missing:
        try:
            ids  = ",".join(cg_map[s] for s in missing if s in cg_map)
            resp = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": ids, "vs_currencies": "usd"},
                timeout=10
            )
            data = resp.json()
            for sym in missing:
                cg_id = cg_map.get(sym)
                if cg_id:
                    price = data.get(cg_id, {}).get("usd")
                    if price:
                        prices[sym] = float(price)
                        cg_count += 1
        except Exception as e:
            print(f"  CoinGecko error: {e}")

    kr_count = len(prices) - binance_count - cg_count
    print(f"  Prices: {kr_count} Kraken + {binance_count} Binance + {cg_count} CoinGecko = {len(prices)} coins")
    return prices

def get_all_prices():
    """
    Get real-time prices from multiple sources.
    Priority: Binance (fastest) → Kraken → Coinbase
    Merges all sources for maximum coin coverage.
    """
    prices = {}

    # Binance — fastest, most coins
    binance = get_binance_prices()
    prices.update(binance)

    # Kraken — fills gaps
    kraken = get_kraken_prices()
    for sym, price in kraken.items():
        if sym not in prices:
            prices[sym] = price

    # Coinbase — fills remaining gaps
    coinbase = get_coinbase_prices()
    for sym, price in coinbase.items():
        if sym not in prices:
            prices[sym] = price

    print(f"  Prices: {len(binance)} Binance + {len(kraken)} Kraken + {len(coinbase)} CB = {len(prices)} total")
    return prices

def update_price_history(prices: dict):
    """Store price snapshot with timestamp."""
    now = time.time()
    for symbol, price in prices.items():
        price_history[symbol].append((now, price))
        # Keep only last 10 minutes of data
        price_history[symbol] = [
            (t, p) for t, p in price_history[symbol]
            if now - t <= 600
        ]

def get_momentum(symbol: str) -> float | None:
    """Improved: Returns precise % change over target window using closest timestamp."""
    history = price_history.get(symbol, [])
    if len(history) < 3:
        return None

    now = time.time()
    target_time = now - (MOMENTUM_WINDOW * 60)

    # Find closest price to target_time
    closest = None
    min_time_diff = float('inf')

    for t, p in history:
        if p <= 0:
            continue
        time_diff = abs(t - target_time)
        if time_diff < min_time_diff:
            min_time_diff = time_diff
            closest = (t, p)

    if closest is None:
        return None

    # Current price (latest valid)
    current_t, current_p = history[-1]
    if current_p <= 0:
        return None

    # Reject if old price is too recent (need at least 50% of window)
    age = now - closest[0]
    min_age = MOMENTUM_WINDOW * 60 * 0.5
    if age < min_age:
        return None

    momentum = ((current_p - closest[1]) / closest[1]) * 100
    return momentum

# ─────────────────────────────────────────
# COINBASE API
# ─────────────────────────────────────────

def cb_headers(method, path):
    if not CB_API_KEY or not CB_SECRET:
        return {}
    is_cdp = CB_API_KEY.startswith("organizations/")
    if is_cdp and JWT_OK:
        try:
            uri     = f"{method} api.coinbase.com{path}"
            private = CB_SECRET.replace("\\n", "\n").strip()
            if "-----BEGIN EC PRIVATE KEY-----" not in private:
                private = f"-----BEGIN EC PRIVATE KEY-----\n{private}\n-----END EC PRIVATE KEY-----"
            token = pyjwt.encode(
                {"sub": CB_API_KEY, "iss": "cdp",
                 "nbf": int(time.time()), "exp": int(time.time()) + 120,
                 "uri": uri},
                private, algorithm="ES256",
                headers={"kid": CB_API_KEY, "nonce": secrets.token_hex(10)},
            )
            if isinstance(token, bytes):
                token = token.decode("utf-8")
            return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        except Exception as e:
            print(f"  CB JWT error: {e}")
            return {}
    else:
        try:
            timestamp = str(int(time.time()))
            message   = timestamp + method + path
            signature = hmac.new(CB_SECRET.encode(), message.encode(), digestmod=hashlib.sha256).hexdigest()
            return {
                "CB-ACCESS-KEY": CB_API_KEY,
                "CB-ACCESS-SIGN": signature,
                "CB-ACCESS-TIMESTAMP": timestamp,
                "CB-ACCESS-PASSPHRASE": os.getenv("COINBASE_PASSPHRASE", ""),
                "Content-Type": "application/json",
            }
        except Exception as e:
            print(f"  CB HMAC error: {e}")
            return {}

def cb_get_balance():
    if not CB_API_KEY: return 400.0
    try:
        path  = "/api/v3/brokerage/accounts"
        hdrs  = cb_headers("GET", path)
        resp  = requests.get(f"https://api.coinbase.com{path}", headers=hdrs, timeout=10)
        accounts = resp.json().get("accounts", [])
        total = 0.0
        for a in accounts:
            currency = a.get("available_balance", {}).get("currency", "")
            value    = float(a.get("available_balance", {}).get("value", 0))
            if currency in ("USD", "USDC") or a.get("type") == "ACCOUNT_TYPE_FIAT":
                total += value
        if total == 0:
            return float(os.getenv("STARTING_BALANCE", "400"))
        return total
    except Exception as e:
        print(f"  CB balance error: {e}")
        return float(os.getenv("STARTING_BALANCE", "400"))

def cb_place_order(symbol, side, size_usd, price=None):
    import uuid
    if not CB_API_KEY:
        print(f"  📝 CB PAPER {side} {symbol} ${size_usd}")
        return f"PAPER-CB-{symbol}-{int(time.time())}"
    try:
        path = "/api/v3/brokerage/orders"
        for quote in ["USD", "USDC"]:
            if side == "BUY":
                cfg = {"quote_size": str(round(size_usd, 2))}
            else:
                if not price or price == 0: return None
                cfg = {"base_size": str(round(size_usd / price, 6))}
            body = json.dumps({
                "client_order_id": str(uuid.uuid4()),
                "product_id": f"{symbol}-{quote}",
                "side": side,
                "order_configuration": {"market_market_ioc": cfg}
            })
            hdrs   = cb_headers("POST", path)
            if not hdrs: return None
            resp   = requests.post(f"https://api.coinbase.com{path}", headers=hdrs, data=body, timeout=10)
            result = resp.json()
            if result.get("success"):
                oid = result.get("success_response", {}).get("order_id", "")
                print(f"  ✅ CB {side} {symbol}-{quote} ${size_usd:.2f}")
                return oid
            err = result.get("error_response", {}).get("message", "")
            print(f"  ⚠ CB {side} {symbol}-{quote}: {err[:60]}")
        return None
    except Exception as e:
        print(f"  ❌ CB order error: {e}")
        return None

# ─────────────────────────────────────────
# KRAKEN API
# ─────────────────────────────────────────

def kr_sign(urlpath, data):
    postdata = urllib.parse.urlencode(data)
    encoded  = (str(data["nonce"]) + postdata).encode()
    message  = urlpath.encode() + hashlib.sha256(encoded).digest()
    mac      = hmac.new(base64.b64decode(KR_SECRET), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()

def kr_request(urlpath, data):
    data["nonce"] = str(int(time.time() * 1000))
    hdrs = {"API-Key": KR_API_KEY, "API-Sign": kr_sign(urlpath, data)}
    resp = requests.post(f"https://api.kraken.com{urlpath}", headers=hdrs, data=data, timeout=10)
    return resp.json()

def kr_get_balance():
    if not KR_API_KEY: return 400.0
    try:
        result = kr_request("/0/private/Balance", {})
        bal = result.get("result", {})
        return float(bal.get("ZUSD", 0)) + float(bal.get("USD", 0))
    except Exception as e:
        print(f"  KR balance error: {e}")
        return 0.0

def kr_place_order(symbol, side, size_usd, price):
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
            print(f"  ❌ KR {side} {symbol}: {errors}")
            return None
        txids = result.get("result", {}).get("txid", [])
        print(f"  ✅ KR {side} {symbol} ${size_usd:.2f}")
        return txids[0] if txids else None
    except Exception as e:
        print(f"  ❌ KR order error: {e}")
        return None

# ─────────────────────────────────────────
# POSITION MONITOR
# ─────────────────────────────────────────

def run_position_monitor():
    global all_time_pnl, all_time_trades, daily_trades, daily_loss
    print("  ✅ Position monitor started (checking every 30s)")
    while True:
        try:
            if not cb_positions and not kr_positions:
                time.sleep(30)
                continue

            prices = get_all_prices()

            for positions, exchange, order_fn in [
                (cb_positions, "Coinbase", cb_place_order),
                (kr_positions, "Kraken",   kr_place_order),
            ]:
                to_close = []
                for symbol, pos in list(positions.items()):
                    cur = prices.get(symbol)
                    if not cur: continue

                    entry = pos["entry"]
                    pct   = ((cur - entry) / entry) * 100
                    mins  = (time.time() - pos["opened_at"]) / 60

                    # Update trailing stop
                    if cur > pos.get("peak_price", pos["entry"]):
                        pos["peak_price"] = cur
                        # Trail stop 1% below new peak
                        new_trail = cur * (1 - STOP_LOSS_PCT / 100)
                        if new_trail > pos["trail_stop"]:
                            pos["trail_stop"] = new_trail
                            locked = ((new_trail - pos["entry"]) / pos["entry"]) * 100
                            if locked > 0:
                                print(f"  [{exchange}] {symbol} trail stop → ${new_trail:.4f} (locks +{locked:.2f}%)")

                    reason = None
                    trail_stop = pos.get("trail_stop", pos["stop"])
                    locked_pct = ((trail_stop - pos["entry"]) / pos["entry"]) * 100

                    if pct >= TAKE_PROFIT_PCT:
                        reason = f"✅ TAKE PROFIT +{pct:.2f}%"
                    elif cur <= trail_stop and locked_pct > 0:
                        reason = f"🔒 TRAILING STOP — locking +{locked_pct:.2f}%"
                    elif cur <= pos["stop"]:
                        reason = f"❌ STOP LOSS {pct:.2f}%"
                    elif mins >= MAX_HOLD_MINS:
                        reason = f"⏰ TIME EXIT {pct:+.2f}% after {mins:.0f}m"

                    if reason:
                        print(f"  [{exchange}] Closing {symbol}: {reason}")
                        order = order_fn(symbol, "SELL", pos["size_usd"], cur)
                        pnl   = pos["size_usd"] * (pct / 100)

                        if order:
                            all_time_pnl    += pnl
                            all_time_trades += 1
                            daily_loss      += min(0, pnl)
                            daily_trades.append({
                                "symbol": symbol, "exchange": exchange,
                                "pnl": pnl, "pct": pct,
                            })
                            to_close.append(symbol)
                            send_telegram(
                                f"{'✅' if pnl>0 else '❌'} *CLOSED — {symbol}* [{exchange}]\n\n"
                                f"📋 {reason}\n"
                                f"📥 Entry: ${entry:.4f}\n"
                                f"📤 Exit: ${cur:.4f}\n"
                                f"⏱ Held: {mins:.0f} mins\n"
                                f"💰 P&L: *${pnl:+.2f}*\n"
                                f"📊 All-time: ${all_time_pnl:+.2f}\n"
                                f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                            )
                        else:
                            print(f"  ⚠ [{exchange}] Sell failed for {symbol}")

                for s in to_close:
                    del positions[s]

        except Exception as e:
            print(f"[POSITIONS] Error: {e}")
        time.sleep(30)

# ─────────────────────────────────────────
# 5-MINUTE MOMENTUM SCANNER
# ─────────────────────────────────────────

def check_momentum_and_trade(prices: dict):
    """Check all coins for 5-minute momentum and fire trades."""
    global daily_loss

    if KILL_SWITCH:
        return

    if daily_loss <= -MAX_DAILY_LOSS:
        print(f"  🛑 Daily loss limit hit (${abs(daily_loss):.2f})")
        return

    cb_bal = cb_get_balance()
    kr_bal = kr_get_balance()

    for coins, positions, exchange, order_fn, balance in [
        (COINBASE_COINS, cb_positions, "Coinbase", cb_place_order, cb_bal),
        (KRAKEN_COINS,   kr_positions, "Kraken",   kr_place_order, kr_bal),
    ]:
        for cg_id, symbol in coins:
            if symbol in positions:
                continue

            price = prices.get(symbol)
            if not price:
                continue

            momentum = get_momentum(symbol)
            if momentum is None:
                continue

            # Only trade coins moving 0.5-3% in last 5 minutes
            if momentum < MIN_MOVE_PCT or momentum > MAX_MOVE_PCT:
                continue

            # Check balance
            if balance < POSITION_USD:
                print(f"  [{exchange}] Low balance ${balance:.2f} — skipping {symbol}")
                continue

            print(f"  [{exchange}] 🎯 {symbol} moved {momentum:+.2f}% in {MOMENTUM_WINDOW}min @ ${price:.4f}")

            # Quick Claude check — optional quality filter
            sig, conf = "LONG", 60
            if ANTHROPIC_KEY:
                try:
                    prompt = (
                        f"{symbol} just moved {momentum:+.2f}% in {MOMENTUM_WINDOW} minutes.\n"
                        f"Current price: ${price:.4f}\n"
                        f"Is this momentum likely to continue for another 5% gain?\n"
                        f'JSON only: {{"signal":"LONG or NEUTRAL","confidence":0-100}}'
                    )
                    resp = requests.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={"x-api-key": ANTHROPIC_KEY,
                                 "anthropic-version": "2023-06-01",
                                 "content-type": "application/json"},
                        json={"model": "claude-haiku-4-5-20251001", "max_tokens": 80,
                              "messages": [{"role": "user", "content": prompt}]},
                        timeout=10
                    )
                    data = resp.json()
                    if "content" in data:
                        raw  = data["content"][0]["text"]
                        s    = json.loads(raw.replace("```json","").replace("```","").strip())
                        sig  = s.get("signal", "LONG")
                        conf = s.get("confidence", 60)
                except:
                    pass  # If Claude fails, still trade based on momentum

            if sig == "NEUTRAL" and conf < MIN_CONFIDENCE:
                print(f"  [{exchange}] {symbol}: Claude says skip @ {conf}%")
                continue

            # Place the order
            order = order_fn(symbol, "BUY", POSITION_USD, price)
            if order:
                target = price * (1 + TAKE_PROFIT_PCT / 100)
                stop   = price * (1 - STOP_LOSS_PCT / 100)
                positions[symbol] = {
                    "entry":      price,
                    "size_usd":   POSITION_USD,
                    "target":     target,
                    "stop":       stop,
                    "peak_price": price,        # tracks highest price seen
                    "trail_stop": stop,         # trailing stop — moves up with price
                    "opened_at":  time.time(),
                    "order_id":   order,
                }
                balance -= POSITION_USD
                send_telegram(
                    f"🟢 *TRADE OPENED — {symbol}* [{exchange}]\n\n"
                    f"📈 Momentum: *{momentum:+.2f}%* in {MOMENTUM_WINDOW} mins\n"
                    f"💰 Entry: ${price:.4f}\n"
                    f"🎯 Target: ${target:.4f} (+{TAKE_PROFIT_PCT}%)\n"
                    f"🛑 Stop: ${stop:.4f} (-{STOP_LOSS_PCT}%)\n"
                    f"⏱ Max hold: {MAX_HOLD_MINS} mins\n"
                    f"💵 Size: ${POSITION_USD:.0f}\n"
                    f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                )

def run_scanner():
    """Main scanning loop — fetch prices every 60s, check momentum."""
    print(f"""
╔══════════════════════════════════╗
║   5-MIN MOMENTUM SCANNER 🎯     ║
╚══════════════════════════════════╝
  Scan: every {SCAN_SECS}s
  Momentum window: {MOMENTUM_WINDOW} mins
  Entry: {MIN_MOVE_PCT}-{MAX_MOVE_PCT}% move
  Take profit: +{TAKE_PROFIT_PCT}%
  Stop loss: -{STOP_LOSS_PCT}%
  Max hold: {MAX_HOLD_MINS} mins
  CB coins: {len(COINBASE_COINS)} | KR coins: {len(KRAKEN_COINS)}
""")

    scan_count = 0
    while True:
        try:
            now = datetime.now(timezone.utc).strftime("%H:%M:%S")
            prices = get_all_prices()

            if not prices:
                print(f"[{now}] No prices — retrying")
                time.sleep(SCAN_SECS)
                continue

            # Update price history
            update_price_history(prices)
            scan_count += 1

            # Need at least MOMENTUM_WINDOW minutes of history before trading
            if scan_count < MOMENTUM_WINDOW:
                remaining = MOMENTUM_WINDOW - scan_count
                print(f"[{now}] Building price history... ({remaining} more scans needed)")
                time.sleep(SCAN_SECS)
                continue

            # Check momentum and trade
            print(f"[{now}] Scanning {len(prices)} coins for {MOMENTUM_WINDOW}min momentum...")
            
            # Debug — show top movers
            movers = []
            for symbol in list(prices.keys())[:20]:
                m = get_momentum(symbol)
                if m is not None and abs(m) > 0.01:
                    movers.append((symbol, m))
            movers.sort(key=lambda x: abs(x[1]), reverse=True)
            if movers:
                top = " | ".join(f"{s}:{m:+.3f}%" for s,m in movers[:8])
                print(f"  Top movers: {top}")
            else:
                # Show momentum values for debugging
                debug_syms = ["BTC","ETH","SOL","XRP","ALGO"]
                for sym in debug_syms:
                    h = price_history.get(sym, [])
                    if len(h) >= 3:
                        m = get_momentum(sym)
                        oldest = h[0][1]
                        newest = h[-1][1]
                        age_mins = (time.time() - h[0][0]) / 60
                        print(f"  {sym}: {len(h)} pts, ${oldest:.2f}→${newest:.2f}, momentum={m if m else "None (window too short)"}, age={age_mins:.1f}m")
            
            check_momentum_and_trade(prices)

            # Show active positions
            total_pos = len(cb_positions) + len(kr_positions)
            if total_pos:
                print(f"  Open: CB={len(cb_positions)} KR={len(kr_positions)} | P&L: ${all_time_pnl:+.2f}")

        except Exception as e:
            print(f"[SCANNER] Error: {e}")
        time.sleep(SCAN_SECS)

# ─────────────────────────────────────────
# DAILY SUMMARY
# ─────────────────────────────────────────

def run_daily_summary():
    global daily_trades, daily_loss
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
                    f"💰 Today: *${total:+.2f}*\n"
                    f"📊 All-time: *${all_time_pnl:+.2f}*\n"
                    f"🔢 Trades: {len(daily_trades)} ({len(wins)}W/{len(losses)}L)\n"
                    f"🎯 Win Rate: {wr:.0f}%\n"
                )
                if best:
                    msg += f"🏆 Best: {best['symbol']} [{best['exchange']}] *+${best['pnl']:.2f}*\n"
                if not daily_trades:
                    msg += "\n_No trades today — market was quiet_"
                msg += f"\n⏰ {now.strftime('%H:%M UTC')}"
                send_telegram(msg)
                daily_trades.clear()
                daily_loss = 0.0
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
                resp = requests.get(
                    "https://api.binance.com/api/v3/ticker/price",
                    params={"symbol": "ETHUSDT"}, timeout=8
                )
                eth_price = float(resp.json()["price"])
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
║   💎  COINBASE ALPHA v5 — 5MIN MOMENTUM         ║
╚══════════════════════════════════════════════════╝
  Coinbase: {'✅' if CB_API_KEY else '❌'} | ${cb_bal:.2f}
  Kraken:   {'✅' if KR_API_KEY else '❌'} | ${kr_bal:.2f}
  Total:    ${cb_bal + kr_bal:.2f}
  Strategy: {MIN_MOVE_PCT}-{MAX_MOVE_PCT}% in {MOMENTUM_WINDOW}min → buy
  Target:   +{TAKE_PROFIT_PCT}% | Stop: -{STOP_LOSS_PCT}%
  Kill switch: {'🛑 ON' if KILL_SWITCH else '✅ OFF'}
  Note: Needs {MOMENTUM_WINDOW} scan cycles before first trade
""")

    send_telegram(
        f"💎 *Coinbase Alpha v5 — 5MIN MOMENTUM*\n\n"
        f"Coinbase: {'✅' if CB_API_KEY else '❌'} ${cb_bal:.2f}\n"
        f"Kraken: {'✅' if KR_API_KEY else '❌'} ${kr_bal:.2f}\n"
        f"Total: *${cb_bal + kr_bal:.2f}*\n\n"
        f"📈 Entry: {MIN_MOVE_PCT}-{MAX_MOVE_PCT}% move in {MOMENTUM_WINDOW} mins\n"
        f"🎯 Target: +{TAKE_PROFIT_PCT}%\n"
        f"🛑 Stop: -{STOP_LOSS_PCT}%\n"
        f"⏱ Max hold: {MAX_HOLD_MINS} mins\n"
        f"Kill switch: {'🛑 ON' if KILL_SWITCH else '✅ OFF'}\n\n"
        f"_Needs 5 mins to build price history before first trade_\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )

    threads = [
        threading.Thread(target=run_scanner,         daemon=True, name="Scanner"),
        threading.Thread(target=run_position_monitor,daemon=True, name="Positions"),
        threading.Thread(target=run_daily_summary,   daemon=True, name="Summary"),
        threading.Thread(target=run_whale_watcher,   daemon=True, name="Whale"),
    ]

    for t in threads:
        t.start()
        print(f"  ✅ Started: {t.name}")
        time.sleep(2)

    print("\n  All systems go.\n")

    while True:
        time.sleep(60)
        alive = [t.name for t in threads if t.is_alive()]
        cb_p  = len(cb_positions)
        kr_p  = len(kr_positions)
        ks    = "🛑" if KILL_SWITCH else "✅"
        print(f"  ♻ {len(alive)}/4 | CB:{cb_p} KR:{kr_p} | P&L:${all_time_pnl:+.2f} | KS:{ks}")

if __name__ == "__main__":
    main()
