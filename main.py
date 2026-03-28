import os, json, asyncio, logging, threading, http.server
from datetime import datetime
import pandas as pd
import numpy as np
import requests, websockets
import matplotlib.pyplot as plt # ለ Screenshot የሚያስፈልግ
from dotenv import load_dotenv

load_dotenv()

class Cfg:
    DERIV_APP_ID = os.environ.get("DERIV_APP_ID", "1085")
    DERIV_TOKEN  = os.environ.get("DERIV_API_TOKEN", "")
    TG_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "")
    TG_CHAT      = os.environ.get("TELEGRAM_CHAT_ID", "1938325440")
    SYMBOL = "frxXAUUSD"
    RISK_AMT = 10 # በእያንዳንዱ ትሬድ የሚመደበው ዶላር

cfg = Cfg()

# ─────────────────────────────────────────────────────────────────────────────
# SMC AUTO-TRADE STRATEGY (EA)
# ─────────────────────────────────────────────────────────────────────────────
class SMCEngine:
    def analyse_and_trade(self, df):
        """BOS እና FVG በመጠቀም ትሬድ የሚከፍት ክፍል"""
        close = df['close'].iloc[-1]
        highs = df['high'].rolling(5).max()
        lows = df['low'].rolling(5).min()
        
        # 1. Break of Structure (BOS)
        if close > highs.iloc[-2]: return "buy", close, close * 0.998 # SL
        if close < lows.iloc[-2]:  return "sell", close, close * 1.002 # SL
        return None, None, None

# ─────────────────────────────────────────────────────────────────────────────
# SCREENSHOT GENERATOR
# ─────────────────────────────────────────────────────────────────────────────
def save_chart_screenshot(df):
    plt.figure(figsize=(10, 5))
    plt.plot(df.index, df['close'], label='XAUUSD Price')
    plt.title(f"SMC Analysis - {datetime.now().strftime('%H:%M')}")
    plt.grid(True)
    plt.savefig("chart.png")
    plt.close()

# ─────────────────────────────────────────────────────────────────────────────
# MAIN BOT ENGINE
# ─────────────────────────────────────────────────────────────────────────────
class Bot:
    def __init__(self):
        self.smc = SMCEngine()
        self.url = f"https://api.telegram.org/bot{cfg.TG_TOKEN}"

    def send_log(self, text):
        requests.post(f"{self.url}/sendMessage", json={"chat_id": cfg.TG_CHAT, "text": text})

    def send_screenshot(self):
        with open("chart.png", "rb") as f:
            requests.post(f"{self.url}/sendPhoto", data={"chat_id": cfg.TG_CHAT}, files={"photo": f})

    async def run(self):
        # Health Server
        port = int(os.environ.get("PORT", 8080))
        threading.Thread(target=http.server.HTTPServer(("0.0.0.0", port), http.server.BaseHTTPRequestHandler).serve_forever, daemon=True).start()
        
        self.send_log("🚀 <b>EA Mode Activated!</b>\nMonitoring SMC Structures...")

        while True:
            try:
                # 1. ዳታ ከ Deriv መውሰድ
                async with websockets.connect(f"wss://ws.binaryws.com/websockets/v3?app_id={cfg.DERIV_APP_ID}") as ws:
                    await ws.send(json.dumps({"ticks_history": cfg.SYMBOL, "style": "candles", "count": 50, "granularity": 900}))
                    res = json.loads(await ws.recv())
                    
                    if "candles" in res:
                        df = pd.DataFrame(res["candles"])
                        side, entry, sl = self.smc.analyse_and_trade(df)
                        
                        # Screenshot መላክ [የጠየቅከው]
                        save_chart_screenshot(df)
                        self.send_screenshot()

                        # 2. ትሬድ መክፈት (Auto Execution)
                        if side:
                            await ws.send(json.dumps({"authorize": cfg.DERIV_TOKEN}))
                            await ws.recv()
                            trade_p = {"buy": 1, "price": cfg.RISK_AMT, "parameters": {"contract_type": "MULTUP" if side=="buy" else "MULTDOWN", "symbol": cfg.SYMBOL, "amount": cfg.RISK_AMT}}
                            await ws.send(json.dumps(trade_p))
                            self.send_log(f"⚡ <b>AUTO TRADE: {side.upper()}</b>\nEntry: {entry}\nSL: {sl}")

                await asyncio.sleep(900) # በየ 15 ደቂቃው ቼክ ያደርጋል
            except Exception as e:
                print(f"Error: {e}")
                await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(Bot().run())
