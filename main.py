#!/usr/bin/env python3
"""
XAU/USD SMC Trading Bot — Deriv WebSocket Edition
===================================================
100% Free | Works on Railway Linux | No PC needed
Uses: Deriv WebSocket API + Gemini AI + Telegram

Environment variables to set in Railway:
  DERIV_APP_ID      = 32PJyBLsBCEcvQiGBkoFG
  DERIV_API_TOKEN   = your_token_from_notes
  GEMINI_API_KEY    = your_gemini_key
  TELEGRAM_TOKEN    = your_telegram_bot_token
  TELEGRAM_CHAT_ID  = 1938325440
"""

import os, sys, json, asyncio, logging, traceback
import threading, time
import http.server
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple

import pandas as pd
import numpy as np
import requests
import websockets
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8")
    ]
)
log = logging.getLogger("XAUUSD")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
class Cfg:
    DERIV_APP_ID    = os.environ.get("DERIV_APP_ID",     "32PJyBLsBCEcvQiGBkoFG")
    DERIV_TOKEN     = os.environ.get("DERIV_API_TOKEN",  "")
    GEMINI_KEY      = os.environ.get("GEMINI_API_KEY",   "")
    TG_TOKEN        = os.environ.get("TELEGRAM_TOKEN",   "")
    TG_CHAT         = os.environ.get("TELEGRAM_CHAT_ID", "1938325440")

    # Deriv symbol for Gold
    # XAU/USD on Deriv = "frxXAUUSD"
    SYMBOL          = "frxXAUUSD"

    # Risk
    RISK_PCT        = 0.01      # 1% per trade
    DAILY_DD_LIM    = 0.03      # 3% daily drawdown halt
    TP1_RR          = 2.0
    TP2_RR          = 4.0

    # SMC
    OB_LOOKBACK     = 30
    SCAN_SECS       = 300       # 5 minutes

    # Sessions UTC
    SESSIONS        = [(8, 17), (13, 21)]

    # Gemini
    GEMINI_MODEL    = "gemini-1.5-flash"

    # Deriv WebSocket
    WS_URL          = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"

    # Granularity in seconds for candles
    # 900 = M15, 3600 = H1, 14400 = H4
    TF_M15          = 900
    TF_H1           = 3600
    TF_H4           = 14400

cfg = Cfg()

# ─────────────────────────────────────────────────────────────────────────────
# HEALTH SERVER (Railway needs port response)
# ─────────────────────────────────────────────────────────────────────────────
def start_health_server():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"XAUUSD Bot Running OK")
        def log_message(self, *a): pass
    port = int(os.environ.get("PORT", 8080))
    srv  = http.server.HTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info(f"Health server on port {port}")

# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────────────────────
class Telegram:
    URL = "https://api.telegram.org/bot{}/sendMessage"

    def __init__(self):
        self._url = self.URL.format(cfg.TG_TOKEN)
        self._ok  = bool(cfg.TG_TOKEN and cfg.TG_CHAT)

    def send(self, text: str):
        if not self._ok:
            log.debug(f"TG (no token): {text[:60]}")
            return
        try:
            requests.post(
                self._url,
                json={"chat_id": cfg.TG_CHAT, "text": text, "parse_mode": "HTML"},
                timeout=10
            )
        except Exception as e:
            log.warning(f"Telegram error: {e}")

    def startup(self, balance: float, currency: str):
        self.send(
            f"🤖 <b>XAUUSD Bot Started!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Balance  : <b>{balance:.2f} {currency}</b>\n"
            f"Risk/trade: {balance * cfg.RISK_PCT:.2f} {currency}\n"
            f"Symbol   : XAU/USD (Gold)\n"
            f"Scanning : Every 5 minutes\n"
            f"Sessions : London + New York"
        )

    def setup(self, direction: str, reason: str, price: float, rsi: float):
        e = "🟢" if direction == "buy" else "🔴"
        self.send(
            f"{e} <b>SMC SETUP — {direction.upper()}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Price  : {price:.5f}\n"
            f"RSI    : {rsi:.1f}\n"
            f"Reason : {reason}\n"
            f"⏳ Checking Gemini AI..."
        )

    def trade_open(self, direction: str, amount: float, entry: float,
                   contract_id: str, tp1: float, tp2: float):
        e = "🚀" if direction == "buy" else "💥"
        self.send(
            f"{e} <b>TRADE OPENED!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"ID       : {contract_id}\n"
            f"Direction: <b>{direction.upper()}</b>\n"
            f"Amount   : ${amount:.2f}\n"
            f"Entry    : {entry:.5f}\n"
            f"TP1 (1:2): {tp1:.5f}\n"
            f"TP2 (1:4): {tp2:.5f}"
        )

    def tp1_hit(self, contract_id: str, pnl: float):
        self.send(
            f"🎯 <b>TP1 HIT!</b>\n"
            f"Contract : {contract_id}\n"
            f"P&L so far: +{pnl:.2f}"
        )

    def closed(self, contract_id: str, pnl: float, reason: str):
        e = "✅" if pnl >= 0 else "❌"
        self.send(
            f"{e} <b>TRADE CLOSED</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Contract : {contract_id}\n"
            f"Reason   : {reason}\n"
            f"P&L      : {pnl:+.2f} USD"
        )

    def gemini_skip(self, direction: str, response: str):
        self.send(
            f"🤖 <b>Gemini Skipped {direction.upper()}</b>\n"
            f"AI Response: <i>{response}</i>"
        )

    def error(self, msg: str):
        self.send(f"⚠️ <b>Bot Error</b>\n<code>{str(msg)[:400]}</code>")

    def dd_halt(self, pct: float):
        self.send(
            f"🛑 <b>DAILY LIMIT HIT</b>\n"
            f"Drawdown: {pct:.2f}%\n"
            f"Bot halted until tomorrow."
        )

    def session_msg(self, msg: str):
        self.send(f"ℹ️ {msg}")

tg = Telegram()

# ─────────────────────────────────────────────────────────────────────────────
# DERIV WEBSOCKET CLIENT
# ─────────────────────────────────────────────────────────────────────────────
class DerivClient:
    """
    Handles all Deriv WebSocket API calls.
    Each method opens a fresh connection, sends request, gets response.
    Simple and reliable for a 5-minute scan bot.
    """

    async def _call(self, payload: dict) -> Optional[dict]:
        """Send one request, return response."""
        try:
            async with websockets.connect(cfg.WS_URL, ping_interval=20) as ws:
                await ws.send(json.dumps(payload))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if "error" in resp:
                    log.error(f"Deriv API error: {resp['error']['message']}")
                    return None
                return resp
        except asyncio.TimeoutError:
            log.error("Deriv API timeout")
            return None
        except Exception as e:
            log.error(f"Deriv WebSocket error: {e}")
            return None

    async def _auth_call(self, payload: dict) -> Optional[dict]:
        """Send authorized request (with token)."""
        try:
            async with websockets.connect(cfg.WS_URL, ping_interval=20) as ws:
                # Authorize first
                auth = {"authorize": cfg.DERIV_TOKEN}
                await ws.send(json.dumps(auth))
                auth_resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if "error" in auth_resp:
                    log.error(f"Auth error: {auth_resp['error']['message']}")
                    return None

                # Then send actual request
                await ws.send(json.dumps(payload))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if "error" in resp:
                    log.error(f"Deriv error: {resp['error']['message']}")
                    return None
                return resp
        except Exception as e:
            log.error(f"Deriv auth call error: {e}")
            return None

    async def get_account(self) -> Optional[Dict]:
        """Get account balance and info."""
        resp = await self._auth_call({"balance": 1, "account": "current"})
        if resp and "balance" in resp:
            return {
                "balance"  : resp["balance"]["balance"],
                "currency" : resp["balance"]["currency"],
                "loginid"  : resp["balance"].get("loginid", ""),
            }
        return None

    async def get_candles(self, symbol: str, granularity: int, count: int) -> Optional[pd.DataFrame]:
        """
        Fetch OHLC candles from Deriv.
        granularity: seconds per candle (900=M15, 3600=H1, 14400=H4)
        """
        payload = {
            "ticks_history": symbol,
            "style"        : "candles",
            "granularity"  : granularity,
            "count"        : count,
            "end"          : "latest",
        }
        resp = await self._call(payload)
        if not resp or "candles" not in resp:
            return None

        rows = []
        for c in resp["candles"]:
            rows.append({
                "time" : pd.Timestamp(c["epoch"], unit="s", tz="UTC"),
                "open" : float(c["open"]),
                "high" : float(c["high"]),
                "low"  : float(c["low"]),
                "close": float(c["close"]),
            })
        if not rows:
            return None
        return pd.DataFrame(rows).set_index("time")

    async def get_price(self, symbol: str) -> Optional[Dict]:
        """Get current bid/ask price."""
        payload = {"ticks": symbol}
        resp = await self._call(payload)
        if resp and "tick" in resp:
            t = resp["tick"]
            return {
                "bid"  : float(t.get("bid",   t.get("quote", 0))),
                "ask"  : float(t.get("ask",   t.get("quote", 0))),
                "quote": float(t.get("quote", 0)),
                "time" : t.get("epoch", 0),
            }
        return None

    async def buy_contract(self, direction: str, amount: float,
                           symbol: str, duration: int = 1440) -> Optional[Dict]:
        """
        Place a trade on Deriv.
        Deriv uses 'CALL' for buy and 'PUT' for sell.
        duration: contract duration in minutes (1440 = 1 day)

        Note: Deriv's CFD/Multiplier contracts work differently from MT5 lots.
        We use 'multipliers' for Gold which is closest to Forex-style trading.
        """
        contract_type = "MULTUP" if direction == "buy" else "MULTDOWN"

        payload = {
            "buy"         : 1,
            "price"       : amount,
            "parameters"  : {
                "contract_type" : contract_type,
                "symbol"        : symbol,
                "amount"        : amount,
                "currency"      : "USD",
                "multiplier"    : 100,   # 100x multiplier for Gold
                "basis"         : "stake",
                "limit_order"   : {
                    "take_profit": amount * cfg.TP1_RR,
                    "stop_loss"  : amount,
                }
            }
        }

        resp = await self._auth_call(payload)
        if resp and "buy" in resp:
            b = resp["buy"]
            return {
                "contract_id"  : str(b.get("contract_id", "")),
                "buy_price"    : float(b.get("buy_price", amount)),
                "start_time"   : b.get("start_time", 0),
                "longcode"     : b.get("longcode", ""),
            }
        return None

    async def get_open_contracts(self) -> List[Dict]:
        """Get all open contracts."""
        resp = await self._auth_call({"portfolio": 1})
        if not resp or "portfolio" not in resp:
            return []
        contracts = resp["portfolio"].get("contracts", [])
        return [
            {
                "contract_id": str(c["contract_id"]),
                "symbol"     : c.get("symbol", ""),
                "pnl"        : float(c.get("profit_loss", 0)),
                "buy_price"  : float(c.get("buy_price", 0)),
            }
            for c in contracts
            if c.get("symbol") == cfg.SYMBOL
        ]

    async def sell_contract(self, contract_id: str, price: float = 0) -> bool:
        """Close/sell an open contract."""
        resp = await self._auth_call({
            "sell" : contract_id,
            "price": price,
        })
        return resp is not None and "sell" in resp

    async def ping(self) -> bool:
        """Check if Deriv API is reachable."""
        resp = await self._call({"ping": 1})
        return resp is not None and resp.get("ping") == "pong"

deriv = DerivClient()

# ─────────────────────────────────────────────────────────────────────────────
# SMC ENGINE
# ─────────────────────────────────────────────────────────────────────────────
class SMC:

    @staticmethod
    def rsi(close: pd.Series, period: int = 14) -> pd.Series:
        delta    = close.diff()
        gain     = delta.clip(lower=0).ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        loss     = (-delta.clip(upper=0)).ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        return 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    def _swings(self, df: pd.DataFrame, left=3, right=3) -> Tuple[pd.Series, pd.Series]:
        sh = pd.Series(False, index=df.index)
        sl = pd.Series(False, index=df.index)
        for i in range(left, len(df) - right):
            if df["high"].iloc[i] == df["high"].iloc[i-left:i+right+1].max():
                sh.iloc[i] = True
            if df["low"].iloc[i] == df["low"].iloc[i-left:i+right+1].min():
                sl.iloc[i] = True
        return sh, sl

    def structure(self, df: pd.DataFrame) -> Dict:
        sh_m, sl_m = self._swings(df)
        shp = df["high"][sh_m].values
        slp = df["low"][sl_m].values
        r   = dict(structure="ranging", bos=False, choch=False,
                   last_sh=0.0, last_sl=0.0, prev_sh=0.0, prev_sl=0.0)
        if len(shp) < 2 or len(slp) < 2:
            return r
        r.update(last_sh=shp[-1], prev_sh=shp[-2],
                 last_sl=slp[-1], prev_sl=slp[-2])
        if shp[-1] > shp[-2] and slp[-1] > slp[-2]:
            r["structure"] = "bullish"
        elif shp[-1] < shp[-2] and slp[-1] < slp[-2]:
            r["structure"] = "bearish"
        cl, pc = df["close"].iloc[-1], df["close"].iloc[-2]
        if cl > shp[-1] > pc:
            r["bos"]   = shp[-1] > shp[-2]
            r["choch"] = shp[-1] <= shp[-2]
        if cl < slp[-1] < pc:
            r["bos"]   = slp[-1] < slp[-2]
            r["choch"] = slp[-1] >= slp[-2]
        return r

    def order_blocks(self, df: pd.DataFrame) -> List[Dict]:
        obs   = []
        chunk = df.tail(cfg.OB_LOOKBACK).reset_index(drop=True)
        for i in range(1, len(chunk) - 1):
            c, n = chunk.iloc[i], chunk.iloc[i+1]
            # Bullish OB
            if (c["close"] < c["open"] and
                n["close"] > n["open"] and n["close"] > c["high"]):
                ob = {"type":"bullish","top":c["open"],"bottom":c["close"]}
                if chunk["low"].iloc[i+1:].min() > ob["bottom"]:
                    obs.append(ob)
            # Bearish OB
            if (c["close"] > c["open"] and
                n["close"] < n["open"] and n["close"] < c["low"]):
                ob = {"type":"bearish","top":c["close"],"bottom":c["open"]}
                if chunk["high"].iloc[i+1:].max() < ob["top"]:
                    obs.append(ob)
        return obs

    def analyse(self, df_m15: pd.DataFrame, df_h1: pd.DataFrame) -> Optional[Dict]:
        h1  = self.structure(df_h1)
        if h1["structure"] == "ranging":
            return None
        obs   = self.order_blocks(df_m15)
        rsi_v = self.rsi(df_m15["close"]).iloc[-1]
        px    = df_m15["close"].iloc[-1]

        if h1["structure"] == "bullish":
            bobs = [o for o in obs if o["type"] == "bullish"
                    and o["bottom"] < px <= o["top"] * 1.003]
            if bobs and rsi_v < 70:
                ob = max(bobs, key=lambda x: x["top"])
                return {
                    "direction": "buy",
                    "entry"    : px,
                    "sl"       : ob["bottom"],
                    "reason"   : f"H1 Bullish | {'BOS' if h1['bos'] else 'CHOCH'} | OB {ob['bottom']:.4f}-{ob['top']:.4f}",
                    "rsi"      : rsi_v,
                    "ob"       : ob,
                    "h1"       : h1,
                }

        if h1["structure"] == "bearish":
            bobs = [o for o in obs if o["type"] == "bearish"
                    and o["bottom"] * 0.997 <= px < o["top"]]
            if bobs and rsi_v > 30:
                ob = min(bobs, key=lambda x: x["bottom"])
                return {
                    "direction": "sell",
                    "entry"    : px,
                    "sl"       : ob["top"],
                    "reason"   : f"H1 Bearish | {'BOS' if h1['bos'] else 'CHOCH'} | OB {ob['bottom']:.4f}-{ob['top']:.4f}",
                    "rsi"      : rsi_v,
                    "ob"       : ob,
                    "h1"       : h1,
                }
        return None

smc = SMC()

# ─────────────────────────────────────────────────────────────────────────────
# GEMINI AI
# ─────────────────────────────────────────────────────────────────────────────
class Gemini:
    def __init__(self):
        self._ok = False
        if cfg.GEMINI_KEY:
            genai.configure(api_key=cfg.GEMINI_KEY)
            self._model = genai.GenerativeModel(cfg.GEMINI_MODEL)
            self._ok = True
            log.info("✅ Gemini AI ready (free tier)")
        else:
            log.warning("No Gemini key — AI filter disabled")

    def confirm(self, sig: Dict, h4_summary: str) -> str:
        if not self._ok:
            return "Bullish" if sig["direction"] == "buy" else "Bearish"
        prompt = (
            f"XAU/USD Gold trade setup.\n"
            f"Proposed: {sig['direction'].upper()}\n"
            f"H1 Trend: {sig['h1']['structure']} | RSI: {sig['rsi']:.1f}\n"
            f"Price: {sig['entry']:.5f}\n"
            f"Order Block: {sig['ob']['bottom']:.5f} - {sig['ob']['top']:.5f}\n"
            f"H4 Context: {h4_summary}\n\n"
            f"Reply with EXACTLY ONE WORD ONLY: Bullish, Bearish, Neutral, or High Risk"
        )
        try:
            r = self._model.generate_content(
                prompt,
                generation_config=genai.GenerationConfig(
                    temperature=0.05, max_output_tokens=8
                )
            )
            raw = r.text.strip()
            for label in ["Bullish", "Bearish", "Neutral", "High Risk"]:
                if label.lower() in raw.lower():
                    log.info(f"🤖 Gemini: {label}")
                    return label
            return "Neutral"
        except Exception as e:
            log.error(f"Gemini error: {e}")
            return "Neutral"

gemini = Gemini()

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def in_session() -> bool:
    h = datetime.now(timezone.utc).hour
    return any(s <= h < e for s, e in cfg.SESSIONS)

def h4_summary(df: Optional[pd.DataFrame]) -> str:
    if df is None or len(df) < 20:
        return "H4 data unavailable"
    c     = df["close"]
    rsi_v = SMC.rsi(c).iloc[-1]
    sma   = c.rolling(20).mean().iloc[-1]
    curr  = c.iloc[-1]
    chg   = (curr - c.iloc[-20]) / c.iloc[-20] * 100
    trend = "BULLISH" if curr > sma else "BEARISH"
    return f"Price:{curr:.5f} SMA20:{sma:.5f} RSI:{rsi_v:.1f} Change:{chg:+.2f}% {trend}"

def risk_amount(balance: float) -> float:
    """1% of balance = trade stake amount."""
    return round(balance * cfg.RISK_PCT, 2)

# ─────────────────────────────────────────────────────────────────────────────
# MAIN BOT
# ─────────────────────────────────────────────────────────────────────────────
class Bot:
    def __init__(self):
        self._day_start  = 0.0
        self._day_date   = None
        self._dd_halted  = False
        self._open_ids   : set = set()
        self._tp1_done   : set = set()

    async def _daily_ok(self) -> bool:
        today = datetime.now(timezone.utc).date()
        if today != self._day_date:
            acc = await deriv.get_account()
            if acc:
                self._day_start = acc["balance"]
            self._day_date  = today
            self._dd_halted = False
            log.info(f"📅 Day reset | Balance: ${self._day_start:.2f}")

        if self._dd_halted:
            return False

        acc = await deriv.get_account()
        if acc and self._day_start > 0:
            dd = (self._day_start - acc["balance"]) / self._day_start
            if dd >= cfg.DAILY_DD_LIM:
                self._dd_halted = True
                log.warning(f"🛑 DD {dd*100:.1f}% — halted")
                tg.dd_halt(dd * 100)
                return False
        return True

    async def _manage_positions(self):
        """Check open contracts — alert on TP1."""
        contracts = await deriv.get_open_contracts()
        current_ids = {c["contract_id"] for c in contracts}

        # Detect closed
        for cid in list(self._open_ids):
            if cid not in current_ids:
                log.info(f"Contract {cid} closed")
                self._open_ids.discard(cid)
                self._tp1_done.discard(cid)

        # Check TP1
        for c in contracts:
            cid = c["contract_id"]
            self._open_ids.add(cid)
            pnl = c["pnl"]
            stake = c["buy_price"]
            if cid not in self._tp1_done and pnl >= stake * cfg.TP1_RR:
                log.info(f"🎯 TP1 hit on {cid} | P&L: +{pnl:.2f}")
                tg.tp1_hit(cid, pnl)
                self._tp1_done.add(cid)

    async def cycle(self):
        """One complete scan cycle."""

        # 1. Manage open positions
        await self._manage_positions()

        # 2. Daily DD check
        if not await self._daily_ok():
            log.info("⏸  Daily limit — skipping")
            return

        # 3. Session filter
        if not in_session():
            log.info("⏸  Outside London/NY session")
            return

        # 4. No stacking
        if self._open_ids:
            log.info(f"📊 {len(self._open_ids)} contract(s) open — no new trade")
            return

        # 5. Fetch candles
        log.info("📡 Fetching candles...")
        df_m15 = await deriv.get_candles(cfg.SYMBOL, cfg.TF_M15, 200)
        df_h1  = await deriv.get_candles(cfg.SYMBOL, cfg.TF_H1,  100)
        df_h4  = await deriv.get_candles(cfg.SYMBOL, cfg.TF_H4,  100)

        if df_m15 is None or df_h1 is None:
            log.warning("Missing candle data — Deriv API issue?")
            return

        # 6. SMC Analysis
        sig = smc.analyse(df_m15, df_h1)
        if sig is None:
            log.info("🔍 No SMC setup found this cycle")
            return

        log.info(f"📡 {sig['direction'].upper()} | {sig['reason']} | RSI:{sig['rsi']:.1f}")
        tg.setup(sig["direction"], sig["reason"], sig["entry"], sig["rsi"])

        # 7. Gemini confirmation
        ai_resp  = gemini.confirm(sig, h4_summary(df_h4))
        expected = "Bullish" if sig["direction"] == "buy" else "Bearish"
        if ai_resp != expected:
            log.info(f"🤖 Gemini '{ai_resp}' ≠ '{expected}' — skip")
            tg.gemini_skip(sig["direction"], ai_resp)
            return

        log.info(f"🤖 Gemini confirmed: {ai_resp} — executing!")

        # 8. Get balance and calculate stake
        acc = await deriv.get_account()
        if not acc:
            log.error("Cannot get balance — skipping")
            return

        stake = risk_amount(acc["balance"])
        log.info(f"💰 Balance: ${acc['balance']:.2f} | Stake: ${stake:.2f}")

        # 9. Calculate TP levels for alert
        price = await deriv.get_price(cfg.SYMBOL)
        entry = price["ask"] if sig["direction"] == "buy" else price["bid"] if price else sig["entry"]
        risk  = abs(entry - sig["sl"])
        tp1   = entry + risk * cfg.TP1_RR if sig["direction"] == "buy" else entry - risk * cfg.TP1_RR
        tp2   = entry + risk * cfg.TP2_RR if sig["direction"] == "buy" else entry - risk * cfg.TP2_RR

        # 10. Place trade
        result = await deriv.buy_contract(sig["direction"], stake, cfg.SYMBOL)
        if result:
            cid = result["contract_id"]
            self._open_ids.add(cid)
            log.info(f"✅ Trade opened | ID:{cid} | Stake:${stake:.2f}")
            tg.trade_open(sig["direction"], stake, entry, cid, tp1, tp2)
        else:
            log.error("Trade execution failed")
            tg.error("Trade execution failed — check Deriv account")

    async def run(self):
        log.info("=" * 55)
        log.info("  XAU/USD SMC BOT — Deriv Edition")
        log.info("  100% Free | Railway Linux")
        log.info("=" * 55)

        start_health_server()

        # Validate config
        if not cfg.DERIV_TOKEN:
            log.critical("DERIV_API_TOKEN not set in Railway env vars!")
            sys.exit(1)

        # Test connection
        log.info("Testing Deriv API connection...")
        if not await deriv.ping():
            log.critical("Cannot reach Deriv API!")
            sys.exit(1)
        log.info("✅ Deriv API reachable")

        # Get account info
        acc = await deriv.get_account()
        if not acc:
            log.critical("Cannot get account info — check DERIV_API_TOKEN")
            sys.exit(1)

        self._day_start = acc["balance"]
        self._day_date  = datetime.now(timezone.utc).date()

        log.info(f"✅ Account: {acc['loginid']}")
        log.info(f"💰 Balance: {acc['balance']:.2f} {acc['currency']}")
        log.info(f"📊 Risk/trade: {risk_amount(acc['balance']):.2f} {acc['currency']}")

        tg.startup(acc["balance"], acc["currency"])

        # Main loop
        while True:
            start = asyncio.get_event_loop().time()
            try:
                await self.cycle()
            except Exception as e:
                log.error(f"Cycle error:\n{traceback.format_exc()}")
                tg.error(str(e))

            elapsed = asyncio.get_event_loop().time() - start
            sleep_t = max(10, cfg.SCAN_SECS - elapsed)
            log.info(f"⏱  Next scan in {sleep_t:.0f}s")
            await asyncio.sleep(sleep_t)

# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(Bot().run())
