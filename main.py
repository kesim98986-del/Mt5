"""
╔══════════════════════════════════════════════════════════════════════╗
║        SMC SNIPER EA v5.3 — Multi-Strategy Autonomous Trading Bot    ║
║     TradingView AI Integration | SMC Brain | Zero-Noise | Amharic    ║
║              [COMPLETE REWRITE — 24H GAPLESS SESSIONS]              ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import logging
import os
import time
import traceback
import math
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import numpy as np
import pandas as pd
import requests
import websockets
from aiohttp import web
from bs4 import BeautifulSoup
from tradingview_ta import TA_Handler, Interval, Exchange

# ══════════════════════════════════════════════════════════════════════
# LOGGING & GLOBAL CONFIG
# ══════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("SNIPER")

NY_TZ = ZoneInfo("America/New_York")
UTC = timezone.utc

PAIR_REGISTRY = {
    "XAUUSD": ("frxXAUUSD", "OTC_XAUUSD", 0.01,   1.0, "XAU/USD 🥇", "METAL"),
    "EURUSD": ("frxEURUSD", "OTC_EURUSD", 0.0001, 1.0, "EUR/USD 🇪🇺", "FOREX"),
    "GBPUSD": ("frxGBPUSD", "OTC_GBPUSD", 0.0001, 1.0, "GBP/USD 🇬🇧", "FOREX"),
    "US100":  ("frxUS100",  "OTC_NDX",    0.1,    1.0, "NASDAQ 💻",  "INDEX"),
}

# ══════════════════════════════════════════════════════════════════════
# TRADINGVIEW ENGINE (FREE DATA FETCH)
# ══════════════════════════════════════════════════════════════════════
def get_tradingview_bias(pair_key: str, tf: str) -> str:
    """ያለምንም ክፍያ ከTradingView የቴክኒክ ትንታኔ ውጤትን ያመጣል"""
    try:
        tv_symbol = pair_key
        screener = "forex"
        exchange = "FX_IDC"
        
        if pair_key == "XAUUSD":
            screener = "cfd"
            exchange = "OANDA"
        elif pair_key == "US100":
            tv_symbol = "NDX"
            screener = "america"
            exchange = "NASDAQ"

        interval_map = {
            "M1": Interval.INTERVAL_1_MINUTE,
            "M5": Interval.INTERVAL_5_MINUTES,
            "M15": Interval.INTERVAL_15_MINUTES,
            "H1": Interval.INTERVAL_1_HOUR
        }
        
        tv_interval = interval_map.get(tf, Interval.INTERVAL_15_MINUTES)
        handler = TA_Handler(symbol=tv_symbol, screener=screener, exchange=exchange, interval=tv_interval)
        analysis = handler.get_analysis()
        return analysis.summary['RECOMMENDATION'] # BUY, STRONG_BUY, etc.
    except Exception as e:
        log.warning(f"TradingView API Error: {e}")
        return "NEUTRAL"

# ══════════════════════════════════════════════════════════════════════
# DATA CLASSES & BOT STATE
# ══════════════════════════════════════════════════════════════════════
class BotState:
    def __init__(self):
        self.deriv_token = os.getenv("DERIV_API_TOKEN", "")
        self.broker_connected = False
        self.account_balance = 0.0
        self.account_currency = "USD"
        self.running = True
        self.paused = False
        
        self.pair_key = "XAUUSD"
        self.active_symbol = PAIR_REGISTRY[self.pair_key][0]
        self.current_price = 0.0
        
        # Candle Buffers (maxlen=1000 for stability)
        self.h1_candles = deque(maxlen=1000)
        self.m15_candles = deque(maxlen=1000)
        self.m5_candles = deque(maxlen=1000)
        
        self.tv_bias_current = "NEUTRAL"
        self.session_now = "ASIAN"
        self.ws = None
        self.req_id = 1
        self.open_contracts = {}
        self.risk_pct = 0.01

state = BotState()
PORT = int(os.getenv("PORT", "8080"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ══════════════════════════════════════════════════════════════════════
# SMC ANALYSIS BRAIN (OB, FVG, LIQUIDITY)
# ══════════════════════════════════════════════════════════════════════
def detect_smc_structures(df):
    """Order Block እና FVG ዞኖችን ይለያል"""
    structures = {"bull_ob": [], "bear_ob": [], "fvg": []}
    if len(df) < 10: return structures

    # FVG Detection
    for i in range(2, len(df)):
        # Bullish FVG (Gap between Candle 1 High and Candle 3 Low)
        if df['low'].iloc[i] > df['high'].iloc[i-2]:
            structures["fvg"].append({
                "top": df['low'].iloc[i], 
                "bottom": df['high'].iloc[i-2], 
                "type": "BULL"
            })
        # Bearish FVG
        elif df['high'].iloc[i] < df['low'].iloc[i-2]:
            structures["fvg"].append({
                "top": df['low'].iloc[i-2], 
                "bottom": df['high'].iloc[i], 
                "type": "BEAR"
            })
            
    # Order Block Logic (Simplified for High Probability)
    # የቅርብ ጊዜውን ተቃራኒ ሻማ እንደ OB ይወስዳል
    last_candle = df.iloc[-2]
    if last_candle['close'] > last_candle['open']:
        structures["bull_ob"].append(last_candle['low'])
    else:
        structures["bear_ob"].append(last_candle['high'])
        
    return structures

# ══════════════════════════════════════════════════════════════════════
# FIXED SESSION LOGIC (NO GAPS)
# ══════════════════════════════════════════════════════════════════════
def update_session():
    now_utc = datetime.now(UTC)
    h = now_utc.hour
    if 0 <= h < 8: state.session_now = "ASIAN"
    elif 8 <= h < 13: state.session_now = "LONDON"
    elif 13 <= h < 17: state.session_now = "OVERLAP"
    elif 17 <= h < 22: state.session_now = "NY"
    else: state.session_now = "ASIAN" # 22:00 - 00:00 gap fix

# ══════════════════════════════════════════════════════════════════════
# TELEGRAM NOTIFIER
# ══════════════════════════════════════════════════════════════════════
async def tg_async(text: str):
    if not TELEGRAM_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=5)
    except: pass

async def send_chart(path: str, caption: str):
    if not TELEGRAM_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        with open(path, "rb") as f:
            requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "Markdown"}, files={"photo": f}, timeout=10)
    except: pass

# ══════════════════════════════════════════════════════════════════════
# HEALTH CHECK & WEB SERVER
# ══════════════════════════════════════════════════════════════════════
async def health(request):
    update_session()
    return web.json_response({
        "status": "Running",
        "pair": state.pair_key,
        "price": state.current_price,
        "tv_bias": state.tv_bias_current,
        "session": state.session_now,
        "balance": state.account_balance
    })

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"Web Dashboard Active on Port {PORT}")

# ══════════════════════════════════════════════════════════════════════
# ADVANCED CHART GENERATOR (SMC VISUALS & FIXED SCALING)
# ══════════════════════════════════════════════════════════════════════
def create_dashboard_image(tf="M15"):
    """ቻርቱን በSMC ምልክቶች እና በትክክለኛ ስኬሊንግ ይስላል"""
    if tf == "H1": buf = state.h1_candles
    elif tf == "M5": buf = state.m5_candles
    else: buf = state.m15_candles

    if len(buf) < 60: return None

    df = pd.DataFrame(list(buf), columns=["time", "open", "high", "low", "close"])
    structures = detect_smc_structures(df)
    
    plt.style.use('dark_background')
    fig = plt.figure(figsize=(12, 8), dpi=100)
    gs = fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.05)
    ax = fig.add_subplot(gs[0])

    # 1. Candle Drawing (Customized for Clarity)
    up = df[df.close >= df.open]
    down = df[df.close < df.open]
    
    ax.bar(up.index, up.close - up.open, 0.6, bottom=up.open, color='#26a69a', label='Bullish')
    ax.bar(up.index, up.high - up.close, 0.1, bottom=up.close, color='#26a69a')
    ax.bar(up.index, up.open - up.low, 0.1, bottom=up.low, color='#26a69a')
    
    ax.bar(down.index, down.open - down.close, 0.6, bottom=down.close, color='#ef5350', label='Bearish')
    ax.bar(down.index, down.high - down.open, 0.1, bottom=down.open, color='#ef5350')
    ax.bar(down.index, down.close - down.low, 0.1, bottom=down.low, color='#ef5350')

    # 2. Visualizing FVG Zones (Fair Value Gaps)
    for f in structures["fvg"][-5:]: # የቅርብ ጊዜ 5 FVGs
        alpha = 0.2
        color = '#26a69a' if f["type"] == "BULL" else '#ef5350'
        rect = patches.Rectangle((len(df)-15, f["bottom"]), 15, f["top"]-f["bottom"], 
                                 color=color, alpha=alpha, label=f"FVG {f['type']}")
        ax.add_patch(rect)

    # 3. Visualizing Order Blocks (OB)
    for ob_price in structures["bull_ob"][-2:]:
        ax.axhline(y=ob_price, color='#00ffcc', linestyle='--', alpha=0.6, label="Bull OB")
    for ob_price in structures["bear_ob"][-2:]:
        ax.axhline(y=ob_price, color='#ff0066', linestyle='--', alpha=0.6, label="Bear OB")

    # 4. SURGICAL SCALING FIX (FLAT-LINE PREVENTION)
    mx, mn = df['high'].max(), df['low'].min()
    mid = (mx + mn) / 2
    spread = mx - mn
    
    # ወርቅ ከሆነ ቢያንስ የ 5 ዶላር ልዩነት እንዲታይ ያስገድዳል
    min_range = 5.0 if state.pair_key == "XAUUSD" else 0.005
    if spread < min_range:
        ax.set_ylim(mid - (min_range/2), mid + (min_range/2))
    else:
        ax.set_ylim(mn - (spread * 0.1), mx + (spread * 0.1))

    ax.set_title(f"SMC SNIPER v5.3 | {state.pair_key} | {state.session_now} | TV: {state.tv_bias_current}", fontsize=14, color='gold')
    ax.grid(True, color='#1f1f1f', linestyle=':', alpha=0.5)
    
    # Save Image
    path = "/tmp/dashboard.png"
    plt.savefig(path, bbox_inches='tight', facecolor='#0a0a0a')
    plt.close()
    return path

# ══════════════════════════════════════════════════════════════════════
# TRADE EXECUTION & SIGNAL LOGIC
# ══════════════════════════════════════════════════════════════════════
async def execute_trade(direction, entry, sl, tp):
    """ትሬዱን በዴሪቭ ላይ ይከፍታል"""
    if not state.broker_connected: return
    
    contract_type = "CALL" if direction == "BUY" else "PUT"
    stake = max(1.0, state.account_balance * state.risk_pct)
    
    request = {
        "buy": 1,
        "price": stake,
        "parameters": {
            "amount": stake,
            "basis": "stake",
            "contract_type": contract_type,
            "currency": state.account_currency,
            "symbol": state.active_symbol,
            "duration": 24,
            "duration_unit": "h",
            "stop_loss": abs(entry - sl),
            "take_profit": abs(entry - tp)
        }
    }
    await state.ws.send(json.dumps(request))
    log.info(f"Trade Request Sent: {direction} @ {entry}")

async def trading_engine():
    """ዋናው የቦቱ የመወሰኛ ክፍል"""
    await asyncio.sleep(20) # ዳታ እስኪሰበሰብ መጠበቅ
    while state.running:
        try:
            update_session()
            state.tv_bias_current = get_tradingview_bias(state.pair_key, "M15")
            
            # ቀላል የSMC ሲግናል ቼክ
            df = pd.DataFrame(list(state.m15_candles), columns=["time", "open", "high", "low", "close"])
            if len(df) < 50: continue
            
            last_price = state.current_price
            bias = "BULLISH" if df['close'].iloc[-1] > df['close'].rolling(50).mean().iloc[-1] else "BEARISH"
            
            # TradingView + SMC Confirmation
            if bias == "BULLISH" and state.tv_bias_current in ["BUY", "STRONG_BUY"]:
                chart_path = create_dashboard_image("M15")
                msg = f"🎯 *BUY SIGNAL CONFIRMED*\nTV: `{state.tv_bias_current}`\nPrice: `{last_price}`"
                await send_chart(chart_path, msg)
                # await execute_trade("BUY", last_price, last_price-2.0, last_price+4.0)
                await asyncio.sleep(300) # Cooldown

            elif bias == "BEARISH" and state.tv_bias_current in ["SELL", "STRONG_SELL"]:
                chart_path = create_dashboard_image("M15")
                msg = f"🎯 *SELL SIGNAL CONFIRMED*\nTV: `{state.tv_bias_current}`\nPrice: `{last_price}`"
                await send_chart(chart_path, msg)
                # await execute_trade("SELL", last_price, last_price+2.0, last_price-4.0)
                await asyncio.sleep(300)

        except Exception as e:
            log.error(f"Trading Engine Error: {e}")
        await asyncio.sleep(30)

# ══════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════
async def ws_manager():
    """ከዴሪቭ ጋር ግንኙነት ይፈጥራል"""
    uri = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"
    while state.running:
        try:
            async with websockets.connect(uri) as ws:
                state.ws = ws
                state.broker_connected = True
                log.info("Successfully connected to Deriv WS")
                
                # Subscribe to price and history
                await ws.send(json.dumps({"ticks": state.active_symbol, "subscribe": 1}))
                await ws.send(json.dumps({"ticks_history": state.active_symbol, "end": "latest", "count": 1000, "style": "candles", "granularity": 900}))
                
                async for message in ws:
                    data = json.loads(message)
                    if "tick" in data:
                        state.current_price = data["tick"]["quote"]
                    elif "candles" in data:
                        for c in data["candles"]:
                            state.m15_candles.append((c["epoch"], c["open"], c["high"], c["low"], c["close"]))
        except:
            state.broker_connected = False
            await asyncio.sleep(5)

async def main():
    await asyncio.gather(
        start_web_server(),
        ws_manager(),
        trading_engine()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        state.running = False
