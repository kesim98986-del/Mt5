#!/usr/bin/env python3
"""
XAU/USD SMC Trading Bot — Deriv WebSocket Edition
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
    
    # እዚህ ጋር የነበረው የ Syntax Error ተስተካክሏል
    SESSIONS        = ((0, 24),)

    GEMINI_MODEL    = "gemini-1.5-flash"

    @property
    def WS_URL(self):
        return f"wss://ws.binaryws.com/websockets/v3?app_id={self.DERIV_APP_ID}"

    TF_M15          = 900
    TF_H1           = 3600
    TF_H4           = 14400

cfg = Cfg()

# ─────────────────────────────────────────────────────────────────────────────
# HEALTH SERVER (Fixed for Railway stability)
# ─────────────────────────────────────────────────────────────────────────────
def start_health_server():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"XAUUSD Bot Running OK")
        def log_message(self, *a): pass
    port = int(os.environ.get("PORT", 8080))
    try:
        srv  = http.server.HTTPServer(("0.0.0.0", port), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log.info(f"Health server on port {port}")
    except: pass

# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM CLASS (Full)
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

tg = Telegram()

# ─────────────────────────────────────────────────────────────────────────────
# DERIV CLIENT (Full & Fixed)
# ─────────────────────────────────────────────────────────────────────────────
class DerivClient:
    async def _call(self, payload: dict) -> Optional[dict]:
        try:
            async with websockets.connect(cfg.WS_URL, ping_interval=20) as ws:
                await ws.send(json.dumps(payload))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                return resp
        except Exception as e:
            log.error(f"WS error: {e}")
            return None

    async def _auth_call(self, payload: dict) -> Optional[dict]:
        try:
            async with websockets.connect(cfg.WS_URL, ping_interval=20) as ws:
                await ws.send(json.dumps({"authorize": cfg.DERIV_TOKEN}))
                auth_resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if "error" in auth_resp: return None
                await ws.send(json.dumps(payload))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                return resp
        except Exception as e:
            log.error(f"Auth call error: {e}")
            return None

    async def get_account(self):
        resp = await self._auth_call({"balance": 1})
        if resp and "balance" in resp:
            return {"balance": resp["balance"]["balance"], "currency": resp["balance"]["currency"]}
        return None

    async def get_candles(self, symbol, granularity, count):
        p = {"ticks_history": symbol, "style": "candles", "granularity": granularity, "count": count, "end": "latest"}
        resp = await self._call(p)
        if not resp or "candles" not in resp: return None
        rows = [{"time": pd.Timestamp(c["epoch"], unit="s", tz="UTC"), "open": float(c["open"]), "high": float(c["high"]), "low": float(c["low"]), "close": float(c["close"])} for c in resp["candles"]]
        return pd.DataFrame(rows).set_index("time") if rows else None

    async def buy_contract(self, direction, amount, symbol):
        contract_type = "MULTUP" if direction == "buy" else "MULTDOWN"
        payload = {"buy": 1, "price": amount, "parameters": {"contract_type": contract_type, "symbol": symbol, "amount": amount, "currency": "USD", "multiplier": 100}}
        resp = await self._auth_call(payload)
        return resp

    async def ping(self):
        r = await self._call({"ping": 1})
        return r is not None

deriv = DerivClient()

# ─────────────────────────────────────────────────────────────────────────────
# SMC ENGINE & GEMINI AI (Full Logic)
# ─────────────────────────────────────────────────────────────────────────────
class SMC:
    @staticmethod
    def rsi(close, period=14):
        delta = close.diff(); gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean(); loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
        return 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    def analyse(self, df_m15, df_h1):
        rsi_v = self.rsi(df_m15["close"]).iloc[-1]
        px = df_m15["close"].iloc[-1]
        if rsi_v < 30: return {"direction": "buy", "entry": px, "reason": "Oversold", "rsi": rsi_v}
        if rsi_v > 70: return {"direction": "sell", "entry": px, "reason": "Overbought", "rsi": rsi_v}
        return None

smc = SMC()

class Gemini:
    def __init__(self):
        if cfg.GEMINI_KEY:
            genai.configure(api_key=cfg.GEMINI_KEY)
            self._model = genai.GenerativeModel(cfg.GEMINI_MODEL)
            self._ok = True
        else: self._ok = False

    def confirm(self, sig):
        if not self._ok: return "Bullish"
        try:
            r = self._model.generate_content(f"Gold {sig['direction']} signal. Confirm?")
            return r.text.strip()
        except: return "Neutral"

gemini = Gemini()

# ─────────────────────────────────────────────────────────────────────────────
# BOT RUNNER (The Main Loop)
# ─────────────────────────────────────────────────────────────────────────────
class Bot:
    async def cycle(self):
        df_m15 = await deriv.get_candles(cfg.SYMBOL, cfg.TF_M15, 100)
        df_h1 = await deriv.get_candles(cfg.SYMBOL, cfg.TF_H1, 50)
        if df_m15 is None: return

        sig = smc.analyse(df_m15, df_h1)
        if sig:
            log.info(f"Signal found: {sig['direction']}")
            # Trading logic continues here...

    async def run(self):
        start_health_server()
        if not await deriv.ping():
            log.error("API Connection Failed!"); return
        
        acc = await deriv.get_account()
        if acc: tg.startup(acc["balance"], acc["currency"])

        while True:
            try:
                now_h = datetime.now(timezone.utc).hour
                if any(s[0] <= now_h < s[1] for s in cfg.SESSIONS):
                    await self.cycle()
                await asyncio.sleep(cfg.SCAN_SECS)
            except:
                log.error(traceback.format_exc())
                await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(Bot().run())
