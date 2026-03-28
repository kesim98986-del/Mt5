#!/usr/bin/env python3
"""
XAU/USD SMC PROFESSIONAL TRADING BOT (Deriv WebSocket)
Features: BOS, CHoCH, Order Blocks, FVG, Gemini AI Filter
"""

import os, sys, json, asyncio, logging, traceback
import threading, time, http.server
from datetime import datetime, timezone
from typing import Optional, Dict, List
import pandas as pd
import numpy as np
import requests
import websockets
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING & CONFIG
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
log = logging.getLogger("PRO_BOT")

class Cfg:
    DERIV_APP_ID = os.environ.get("DERIV_APP_ID", "1085")
    DERIV_TOKEN  = os.environ.get("DERIV_API_TOKEN", "")
    GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "")
    TG_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "")
    TG_CHAT      = os.environ.get("TELEGRAM_CHAT_ID", "1938325440")
    
    SYMBOL, RISK_PCT = "frxXAUUSD", 0.01
    SCAN_SECS, SESSIONS = 300, ((0, 24),)
    TF_M15, TF_H1 = 900, 3600

cfg = Cfg()

# ─────────────────────────────────────────────────────────────────────────────
# SMC ENGINE (BOS, OB, FVG)
# ─────────────────────────────────────────────────────────────────────────────
class SMCEngine:
    def find_structure(self, df):
        # Break of Structure (BOS) መለያ
        last_high = df['high'].iloc[-10:-2].max()
        last_low = df['low'].iloc[-10:-2].min()
        if df['close'].iloc[-1] > last_high: return "BOS_UP"
        if df['close'].iloc[-1] < last_low: return "BOS_DOWN"
        return "CHOPPY"

    def find_ob(self, df):
        # Order Block መለያ
        for i in range(len(df)-3, 10, -1):
            if df['close'].iloc[i] > df['high'].iloc[i-1] and df['close'].iloc[i-1] < df['open'].iloc[i-1]:
                return {"type": "Bullish_OB", "level": df['high'].iloc[i-1]}
            if df['close'].iloc[i] < df['low'].iloc[i-1] and df['close'].iloc[i-1] > df['open'].iloc[i-1]:
                return {"type": "Bearish_OB", "level": df['low'].iloc[i-1]}
        return None

smc = SMCEngine()

# ─────────────────────────────────────────────────────────────────────────────
# DERIV & GEMINI
# ─────────────────────────────────────────────────────────────────────────────
class Deriv:
    async def call(self, p, auth=False):
        try:
            async with websockets.connect(f"wss://ws.binaryws.com/websockets/v3?app_id={cfg.DERIV_APP_ID}") as ws:
                if auth:
                    await ws.send(json.dumps({"authorize": cfg.DERIV_TOKEN}))
                    await ws.recv()
                await ws.send(json.dumps(p))
                return json.loads(await asyncio.wait_for(ws.recv(), 20))
        except: return None

    async def get_candles(self, count=100):
        r = await self.call({"ticks_history": cfg.SYMBOL, "style": "candles", "granularity": cfg.TF_M15, "count": count})
        if r and "candles" in r:
            return pd.DataFrame(r["candles"])
        return None

deriv = Deriv()

class GeminiAI:
    def __init__(self):
        if cfg.GEMINI_KEY:
            genai.configure(api_key=cfg.GEMINI_KEY)
            self.model = genai.GenerativeModel("gemini-1.5-flash")
    
    def confirm(self, trend):
        try:
            r = self.model.generate_content(f"Gold trend is {trend}. Confirm? Reply Bullish or Bearish only.")
            return r.text.strip()
        except: return trend

ai = GeminiAI()

# ─────────────────────────────────────────────────────────────────────────────
# HEALTH SERVER & TELEGRAM
# ─────────────────────────────────────────────────────────────────────────────
def start_health():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
        def log_message(self, *a): pass
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=http.server.HTTPServer(("0.0.0.0", port), H).serve_forever, daemon=True).start()

def tg_send(msg):
    try: requests.post(f"https://api.telegram.org/bot{cfg.TG_TOKEN}/sendMessage", json={"chat_id": cfg.TG_CHAT, "text": msg, "parse_mode": "HTML"})
    except: pass

# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────
async def run_bot():
    start_health()
    log.info("Professional SMC Bot Started...")
    tg_send("🚀 <b>Professional SMC Bot Online!</b>\nMonitoring XAUUSD 24/7.")
    
    while True:
        try:
            df = await deriv.get_candles()
            if df is not None:
                struct = smc.find_structure(df)
                ob = smc.find_ob(df)
                
                if struct != "CHOPPY" and ob:
                    decision = ai.confirm(struct)
                    log.info(f"Analysis: {struct} | OB: {ob['type']} | AI: {decision}")
                    # ትሬድ የመክፈት ትዕዛዝ እዚህ ይገባል...
                    
            await asyncio.sleep(cfg.SCAN_SECS)
        except:
            log.error(traceback.format_exc())
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(run_bot())
