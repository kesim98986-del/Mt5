#!/usr/bin/env python3
"""
XAU/USD SMC Trading Bot — Deriv WebSocket Edition (FIXED)
===================================================
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
    # 1085 የDeriv ዲፎልት App ID ነው (የራስህ ከሌለህ ይሄ ይሰራል)
    DERIV_APP_ID    = os.environ.get("DERIV_APP_ID", "1085")
    DERIV_TOKEN     = os.environ.get("DERIV_API_TOKEN", "")
    GEMINI_KEY      = os.environ.get("GEMINI_API_KEY", "")
    TG_TOKEN        = os.environ.get("TELEGRAM_TOKEN", "")
    TG_CHAT         = os.environ.get("TELEGRAM_CHAT_ID", "1938325440")

    SYMBOL          = "frxXAUUSD"
    RISK_PCT        = 0.01
    DAILY_DD_LIM    = 0.03
    TP1_RR          = 2.0
    TP2_RR          = 4.0
    OB_LOOKBACK     = 30
    SCAN_SECS       = 300
    SESSIONS =      = ((0, 24),)
    GEMINI_MODEL    = "gemini-1.5-flash"

    # የWebSocket URL ከApp ID ጋር ተስተካክሏል
    @property
    def WS_URL(self):
        return f"wss://ws.binaryws.com/websockets/v3?app_id={self.DERIV_APP_ID}"

    TF_M15          = 900
    TF_H1           = 3600
    TF_H4           = 14400

cfg = Cfg()

# ─────────────────────────────────────────────────────────────────────────────
# HEALTH SERVER (Railway needs this)
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
        if not self._ok: return
        try:
            requests.post(self._url, json={"chat_id": cfg.TG_CHAT, "text": text, "parse_mode": "HTML"}, timeout=10)
        except Exception as e: log.warning(f"Telegram error: {e}")

    def startup(self, balance: float, currency: str):
        self.send(f"🤖 <b>XAUUSD Bot Started!</b>\nBalance: {balance:.2f} {currency}")

    def setup(self, direction, reason, price, rsi):
        self.send(f"🟢 <b>SMC SETUP: {direction.upper()}</b>\nPrice: {price}\nReason: {reason}")

    def trade_open(self, direction, amount, entry, cid, tp1, tp2):
        self.send(f"🚀 <b>TRADE OPENED!</b>\nID: {cid}\nEntry: {entry}\nTP1: {tp1}")

    def tp1_hit(self, cid, pnl):
        self.send(f"🎯 <b>TP1 HIT!</b>\nID: {cid}\nP&L: +{pnl:.2f}")

    def closed(self, cid, pnl, reason):
        self.send(f"✅ <b>CLOSED</b>\nID: {cid}\nP&L: {pnl:+.2f}\n{reason}")

    def gemini_skip(self, direction, response):
        self.send(f"🤖 <b>Gemini Skipped</b>: {response}")

    def error(self, msg):
        self.send(f"⚠️ <b>Error</b>: <code>{str(msg)[:200]}</code>")

    def dd_halt(self, pct):
        self.send(f"🛑 <b>HALTED</b>: {pct:.2f}% Drawdown")

tg = Telegram()

# ─────────────────────────────────────────────────────────────────────────────
# DERIV CLIENT (ተስተካክሏል)
# ─────────────────────────────────────────────────────────────────────────────
class DerivClient:
    async def _call(self, payload: dict) -> Optional[dict]:
        try:
            async with websockets.connect(cfg.WS_URL, ping_interval=20) as ws:
                await ws.send(json.dumps(payload))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if "error" in resp:
                    log.error(f"Deriv error: {resp['error']['message']}")
                    return None
                return resp
        except Exception as e:
            log.error(f"WS error: {e}")
            return None

    async def _auth_call(self, payload: dict) -> Optional[dict]:
        try:
            async with websockets.connect(cfg.WS_URL, ping_interval=20) as ws:
                await ws.send(json.dumps({"authorize": cfg.DERIV_TOKEN}))
                auth_resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if "error" in auth_resp:
                    log.error(f"Auth failed: {auth_resp['error']['message']}")
                    return None
                await ws.send(json.dumps(payload))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                return resp if "error" not in resp else None
        except Exception as e:
            log.error(f"Auth call error: {e}")
            return None

    async def get_account(self):
        resp = await self._auth_call({"balance": 1})
        if resp and "balance" in resp:
            return {"balance": resp["balance"]["balance"], "currency": resp["balance"]["currency"], "loginid": resp["balance"].get("loginid", "")}
        return None

    async def get_candles(self, symbol, granularity, count):
        p = {"ticks_history": symbol, "style": "candles", "granularity": granularity, "count": count, "end": "latest"}
        resp = await self._call(p)
        if not resp or "candles" not in resp: return None
        rows = [{"time": pd.Timestamp(c["epoch"], unit="s", tz="UTC"), "open": float(c["open"]), "high": float(c["high"]), "low": float(c["low"]), "close": float(c["close"])} for c in resp["candles"]]
        return pd.DataFrame(rows).set_index("time") if rows else None

    async def get_price(self, symbol):
        resp = await self._call({"ticks": symbol})
        if resp and "tick" in resp:
            t = resp["tick"]
            return {"bid": float(t.get("bid", t.get("quote", 0))), "ask": float(t.get("ask", t.get("quote", 0))), "quote": float(t.get("quote", 0))}
        return None

    async def buy_contract(self, direction, amount, symbol):
        contract_type = "MULTUP" if direction == "buy" else "MULTDOWN"
        payload = {"buy": 1, "price": amount, "parameters": {"contract_type": contract_type, "symbol": symbol, "amount": amount, "currency": "USD", "multiplier": 100, "basis": "stake"}}
        resp = await self._auth_call(payload)
        if resp and "buy" in resp:
            return {"contract_id": str(resp["buy"]["contract_id"]), "buy_price": float(resp["buy"]["buy_price"])}
        return None

    async def get_open_contracts(self):
        resp = await self._auth_call({"portfolio": 1})
        if not resp or "portfolio" not in resp: return []
        return [{"contract_id": str(c["contract_id"]), "symbol": c.get("symbol", ""), "pnl": float(c.get("profit_loss", 0)), "buy_price": float(c.get("buy_price", 0))} for c in resp["portfolio"].get("contracts", []) if c.get("symbol") == cfg.SYMBOL]

    async def ping(self):
        r = await self._call({"ping": 1})
        return r is not None and r.get("ping") == "pong"

deriv = DerivClient()

# ─────────────────────────────────────────────────────────────────────────────
# SMC ENGINE
# ─────────────────────────────────────────────────────────────────────────────
class SMC:
    @staticmethod
    def rsi(close, period=14):
        delta = close.diff(); gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean(); loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
        return 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    def analyse(self, df_m15, df_h1):
        # እዚህ ጋር የእርስዎ SMC Logic ይቀጥላል...
        # ለሙከራ እንዲሆን አጭር logic:
        rsi_v = self.rsi(df_m15["close"]).iloc[-1]
        px = df_m15["close"].iloc[-1]
        if rsi_v < 30: return {"direction": "buy", "entry": px, "sl": px*0.99, "reason": "Oversold RSI", "rsi": rsi_v, "ob": {"top": px, "bottom": px*0.99}, "h1": {"structure": "bullish"}}
        if rsi_v > 70: return {"direction": "sell", "entry": px, "sl": px*1.01, "reason": "Overbought RSI", "rsi": rsi_v, "ob": {"top": px*1.01, "bottom": px}, "h1": {"structure": "bearish"}}
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

    def confirm(self, sig, h4_summary):
        if not self._ok: return "Bullish" if sig["direction"] == "buy" else "Bearish"
        prompt = f"Gold Trade: {sig['direction']}. RSI: {sig['rsi']}. Context: {h4_summary}. One word only: Bullish or Bearish?"
        try:
            r = self._model.generate_content(prompt)
            return r.text.strip()
        except: return "Neutral"

gemini = Gemini()

# ─────────────────────────────────────────────────────────────────────────────
# BOT RUNNER
# ─────────────────────────────────────────────────────────────────────────────
class Bot:
    def __init__(self):
        self._open_ids = set(); self._tp1_done = set()

    async def cycle(self):
        # 1. Manage positions
        contracts = await deriv.get_open_contracts()
        curr_ids = {c["contract_id"] for c in contracts}
        self._open_ids = curr_ids
        
        # 2. Analysis
        df_m15 = await deriv.get_candles(cfg.SYMBOL, cfg.TF_M15, 100)
        df_h1 = await deriv.get_candles(cfg.SYMBOL, cfg.TF_H1, 50)
        if df_m15 is None or df_h1 is None: return

        sig = smc.analyse(df_m15, df_h1)
        if sig and not self._open_ids:
            acc = await deriv.get_account()
            if acc:
                stake = round(acc["balance"] * cfg.RISK_PCT, 2)
                res = await deriv.buy_contract(sig["direction"], stake, cfg.SYMBOL)
                if res:
                    tg.trade_open(sig["direction"], stake, sig["entry"], res["contract_id"], 0, 0)

    async def run(self):
        start_health_server()
        if not await deriv.ping():
            log.error("API Connection Failed!"); return
        
        acc = await deriv.get_account()
        if acc: tg.startup(acc["balance"], acc["currency"])

        while True:
            try: await self.cycle()
            except: log.error(traceback.format_exc())
            await asyncio.sleep(cfg.SCAN_SECS)

if __name__ == "__main__":
    asyncio.run(Bot().run())
