"""
╔══════════════════════════════════════════════════════════════════════╗
║        SMC SNIPER EA v5.9 — COMPLETE PRODUCTION VERSION             ║
║     Senior Quant SMC | Sniper Brain | News Shield | Broker Connect   ║
║   Price from API Ninjas | Trade on Deriv | Full Telegram Support     ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import logging
import os
import time
import traceback
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
import websockets
from aiohttp import web
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------
# API Ninjas configuration
# ----------------------------------------------------------------------
API_NINJAS_KEY = "bK4d0rGjIDSRQiA7hTWiEJvzRcBBhBkkl6aDM00z"
API_NINJAS_BASE = "https://api.api-ninjas.com/v1"

PAIR_API_MAP = {
    "XAUUSD": {"type": "commodity", "name": "gold", "multiplier": 1.0},
    "EURUSD": {"type": "forex", "pair": "EURUSD", "multiplier": 1.0},
    "GBPUSD": {"type": "forex", "pair": "GBPUSD", "multiplier": 1.0},
    "US100":  {"type": "index", "name": "nasdaq100", "multiplier": 1.0},
}

# ----------------------------------------------------------------------
# Optional Supabase
# ----------------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        from supabase import create_client, Client
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        log.info("✅ Supabase client initialized")
    except Exception as e:
        print(f"Supabase init failed: {e}")

# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("SNIPER")

# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------
NY_TZ = ZoneInfo("America/New_York")
UTC = timezone.utc

PAIR_REGISTRY = {
    "XAUUSD": ("frxXAUUSD", "OTC_XAUUSD", 0.01, 1.0, "XAU/USD 🥇", "METAL"),
    "EURUSD": ("frxEURUSD", "OTC_EURUSD", 0.0001, 1.0, "EUR/USD 🇪🇺", "FOREX"),
    "GBPUSD": ("frxGBPUSD", "OTC_GBPUSD", 0.0001, 1.0, "GBP/USD 🇬🇧", "FOREX"),
    "US100":  ("frxUS100",  "OTC_NDX",    0.1,    1.0, "NASDAQ 💻",   "INDEX"),
}

GRAN_FALLBACKS = {3600: [3600,7200], 900: [900,600,1800], 300: [300,180,600], 60: [60,120]}
SESSIONS = {"ASIAN":(0,8), "LONDON":(8,13), "OVERLAP":(13,17), "NY":(17,22)}
BEST_SESSIONS = {"METAL":["LONDON","NY","OVERLAP"], "FOREX":["LONDON","NY","OVERLAP"], "INDEX":["NY","OVERLAP"]}

MARKET_OPEN_HOUR, MARKET_CLOSE_HOUR = 22, 21
TOKEN_FILE = Path("/tmp/.deriv_token")
PRE_NEWS_BLOCK, POST_NEWS_WAIT, NEWS_INTERVAL = 30*60, 15*60, 12*3600
CHART_INTERVAL = int(os.getenv("CHART_INTERVAL","300"))
PORT = int(os.getenv("PORT","8080"))
DERIV_APP_ID = os.getenv("DERIV_APP_ID","1089")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN","")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID","")
DERIV_WS_BASE = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"

# ----------------------------------------------------------------------
# Helper classes
# ----------------------------------------------------------------------
class NewsEvent:
    __slots__ = ("time_et","currency","impact","title","actual","forecast","prev","dt_utc")
    def __init__(self, time_et, currency, impact, title, actual="", forecast="", prev=""):
        self.time_et = time_et
        self.currency = currency
        self.impact = impact
        self.title = title
        self.actual = actual
        self.forecast = forecast
        self.prev = prev
        self.dt_utc = None
    @property
    def is_red(self): return self.impact == "high"
    @property
    def is_orange(self): return self.impact == "medium"

class TradeReason:
    def __init__(self):
        self.h1_trend = self.structure = self.pd_zone = ""
        self.idm_sweep = self.trap_sweep = self.ob_type = ""
        self.fvg_present = False
        self.session = self.atr_state = self.ema_confirm = ""
        self.rsi_level = 0.0
        self.score = 0
        self.candle_conf = self.entry_logic = self.tv_confirm = ""
    def build_report(self, direction):
        arrow = "📈 BUY" if direction=="BUY" else "📉 SELL"
        lines = [f"🧠 *Sniper Brain — Trade Reasoning*\nDirection: *{arrow}*\nScore: `{self.score}/100`",
                 "\n*Multi-Timeframe Analysis:*", f"• Trend: `{self.h1_trend}`", f"• Structure: `{self.structure}`", f"• Zone: `{self.pd_zone}`",
                 "\n*Liquidity & Traps:*", f"• IDM Sweep: `{self.idm_sweep}`", f"• Trap Sweep: `{self.trap_sweep}`", f"• OB Type: `{self.ob_type}`", f"• FVG Present: `{'✅ Yes' if self.fvg_present else '⚠️ No'}`",
                 "\n*Market Filters:*", f"• Session: `{self.session}`", f"• ATR: `{self.atr_state}`", f"• EMA50: `{self.ema_confirm}`", f"• RSI: `{self.rsi_level:.1f}`", f"• Candle: `{self.candle_conf}`",
                 "\n*Entry Logic:*", f"`{self.entry_logic}`"]
        if self.tv_confirm:
            lines.append(f"\n*TradingView Confirmation:*\n`{self.tv_confirm}`")
        return "\n".join(lines)
    def build_amharic(self, direction):
        arrow = "ወደ ላይ (BUY)" if direction=="BUY" else "ወደ ታች (SELL)"
        return (f"🤖 *ቦቱ ዝርዝር ምክንያት:*\nአቅጣጫ: `{arrow}`\n• ዋና ዝንባሌ: `{self.h1_trend}`\n• IDM ተወስዷል: `{self.idm_sweep}`\n• ወጥመድ ተወስዷል: `{self.trap_sweep}`\n• OB ዓይነት: `{self.ob_type}`\n• ዞን: `{self.pd_zone}`\n• ሰሽን: `{self.session}`\n• ውሳኔ ምክንያት: `{self.entry_logic}`")

# ----------------------------------------------------------------------
# Bot State
# ----------------------------------------------------------------------
class BotState:
    def __init__(self):
        self.deriv_token = os.getenv("DERIV_API_TOKEN", "")
        self.broker_connected = False
        self.account_type = "unknown"
        self.account_id = ""
        self.account_balance = 0.0
        self.account_currency = "USD"
        self.awaiting_token = False
        self.awaiting_custom_score = False

        self.running = True
        self.paused = False
        self.autonomous = True
        self.block_trading = False
        self.block_reason = ""

        self.trading_mode = "SNIPER"
        self.min_score = int(os.getenv("MIN_SCORE","75"))
        self.trend_tf = "H1"
        self.exec_tf = "M15"
        self.conf_tf = "M5"
        self.top_down = True

        self.pair_key = "XAUUSD"
        self.active_symbol = ""
        self.risk_pct = 0.01
        self.tp1_r, self.tp2_r, self.tp3_r = 2.0, 4.0, 6.0
        self.small_acc_mode = False

        self.h1_candles = deque(maxlen=1000)
        self.m15_candles = deque(maxlen=1000)
        self.m5_candles = deque(maxlen=1000)
        self.m1_candles = deque(maxlen=1000)
        self.gran_actual = {3600:3600, 900:900, 300:300, 60:60}
        self.last_api_update = 0

        self.current_price = 0.0
        self.trend_bias = "NEUTRAL"
        self.last_signal = None
        self.active_ob = self.active_fvg = self.active_trap = self.active_idm = None
        self.premium_discount = "NEUTRAL"
        self.ob_score = 0
        self.session_now = "ASIAN"
        self.atr_filter_ok = True
        self.market_open = True

        self.ws = None
        self.req_id = 1
        self.pending_reqs = {}
        self.subscribed_sym = None

        self.open_contracts = {}
        self.trade_count = 0
        self.wins = self.losses = 0
        self.total_pnl = 0.0
        self.trade_history = []
        self.last_trade_ts = 0.0
        self.signal_cooldown = 300

        self.news_events = []
        self.news_last_fetch = 0.0
        self.next_red_event = None
        self.news_chart_path = None

    @property
    def pair_info(self): return PAIR_REGISTRY[self.pair_key]
    @property
    def pair_display(self): return self.pair_info[4]
    @property
    def pair_category(self): return self.pair_info[5]

state = BotState()

# ----------------------------------------------------------------------
# API Ninjas functions
# ----------------------------------------------------------------------
def get_current_price() -> float:
    try:
        info = PAIR_API_MAP.get(state.pair_key)
        if not info:
            return 0.0
        if info["type"] == "commodity":
            url = f"{API_NINJAS_BASE}/commodityprice?name={info['name']}"
        elif info["type"] == "forex":
            url = f"{API_NINJAS_BASE}/exchangerate?pair={info['pair']}"
        else:
            return 0.0
        headers = {"X-Api-Key": API_NINJAS_KEY}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if info["type"] == "commodity":
                price = float(data.get("price", 0))
            else:
                price = float(data.get("exchange_rate", 0))
            return price * info.get("multiplier", 1.0)
        else:
            log.warning(f"API Ninjas error {resp.status_code}")
            return 0.0
    except Exception as e:
        log.error(f"API Ninjas price error: {e}")
        return 0.0

# ----------------------------------------------------------------------
# Market & session helpers
# ----------------------------------------------------------------------
def is_market_open():
    now = datetime.now(UTC)
    wd, h = now.weekday(), now.hour
    if wd == 4 and h >= 21: return False
    if wd == 5: return False
    if wd == 6 and h < 22: return False
    return True

def time_to_next_open():
    now = datetime.now(UTC)
    wd = now.weekday()
    days = (6 - wd) % 7
    if days == 0 and now.hour >= 22: days = 7
    next_open = (now + timedelta(days=days)).replace(hour=22, minute=0, second=0, microsecond=0)
    delta = next_open - now
    h, m = divmod(int(delta.total_seconds()), 3600)
    m //= 60
    return f"{h}h {m}m"

def get_session():
    h = datetime.now(UTC).hour
    if 8 <= h < 13: return "LONDON"
    if 13 <= h < 17: return "OVERLAP"
    if 17 <= h < 22: return "NY"
    return "ASIAN"

def market_header():
    if is_market_open():
        return f"🟢 Market is *OPEN* · Session: `{get_session()}`"
    return f"🔴 Market is *CLOSED* · Opens in `{time_to_next_open()}`"

# ----------------------------------------------------------------------
# RSI calculation
# ----------------------------------------------------------------------
def _rsi_calc(p: np.ndarray, n: int = 14) -> np.ndarray:
    if len(p) < n + 1:
        return np.full(len(p), 50.)
    d = np.diff(p)
    g = np.where(d > 0, d, 0.)
    l = np.where(d < 0, -d, 0.)
    ag = np.convolve(g, np.ones(n)/n, "valid")
    al = np.convolve(l, np.ones(n)/n, "valid")
    rs = np.where(al != 0, ag / al, 100.)
    rsi = 100. - 100. / (1. + rs)
    return np.concatenate([np.full(len(p)-len(rsi), 50.), rsi])

# ----------------------------------------------------------------------
# Chart generation (simple matplotlib, no mplfinance issues)
# ----------------------------------------------------------------------
def generate_chart(candles, tf="M15", entry_price=None, exit_price=None, direction=None, pnl=None, chart_type="live", reason=None):
    if len(candles) < 10:
        return None
    df = pd.DataFrame(list(candles))
    df.columns = ["time","open","high","low","close"]
    df = df.astype(float)
    df = df[(df["open"]>0) & (df["high"]>0) & (df["low"]>0) & (df["close"]>0)]
    df["date"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("date", inplace=True)
    df = df.tail(40)
    if len(df) < 5:
        return None

    price_range = df["high"].max() - df["low"].min()
    if state.pair_key == "XAUUSD":
        min_padding = 10.0
    else:
        min_padding = 0.010
    padding = max(price_range * 0.1, min_padding)
    y_min = df["low"].min() - padding
    y_max = df["high"].max() + padding

    fig, ax = plt.subplots(figsize=(12,6))
    for i in range(len(df)):
        o = df["open"].iloc[i]
        c = df["close"].iloc[i]
        h = df["high"].iloc[i]
        l = df["low"].iloc[i]
        color = '#00c853' if c >= o else '#d50000'
        ax.plot([i,i], [l,h], color=color, linewidth=1)
        ax.add_patch(plt.Rectangle((i-0.3, min(o,c)), 0.6, abs(c-o), facecolor=color, edgecolor=color))
    ax.set_ylim(y_min, y_max)
    ax.set_title(f"{state.pair_display} {tf} · {chart_type}")
    path = f"/tmp/sniper_chart_{chart_type}.png"
    plt.savefig(path, dpi=100, bbox_inches="tight")
    plt.close()
    return path

def generate_history_chart():
    if not state.trade_history:
        return None
    fig, (ax1, ax2) = plt.subplots(2,1, figsize=(14,9))
    labels = [f"#{t['num']}" for t in state.trade_history[-20:]]
    pnls = [t["pnl"] for t in state.trade_history[-20:]]
    colors = ['#00e676' if p>0 else '#ff1744' for p in pnls]
    ax1.bar(labels, pnls, color=colors)
    ax1.axhline(0, color='gray')
    cum = np.cumsum(pnls)
    ax2.plot(labels, cum, marker='o')
    ax2.fill_between(labels, cum, alpha=0.2)
    path = "/tmp/sniper_history.png"
    plt.savefig(path)
    plt.close()
    return path

# ----------------------------------------------------------------------
# News functions (Forex Factory)
# ----------------------------------------------------------------------
FF_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}

def _parse_ff_time(ts, base):
    ts = ts.strip().lower()
    if not ts or ts in ("all day","tentative","","—"):
        return base.replace(hour=0, minute=0, second=0)
    try:
        t = datetime.strptime(ts, "%I:%M%p")
        return base.replace(hour=t.hour, minute=t.minute, second=0, tzinfo=NY_TZ).astimezone(UTC)
    except:
        return None

def fetch_news():
    events = []
    today = datetime.now(NY_TZ)
    for offset in (0,1):
        target = today + timedelta(days=offset)
        url = f"https://www.forexfactory.com/calendar?day={target.strftime('%b%d.%Y').lower()}"
        try:
            resp = requests.get(url, headers=FF_HEADERS, timeout=15)
            if resp.status_code != 200: continue
            soup = BeautifulSoup(resp.text, "html.parser")
            cur_time = ""
            for row in soup.select("tr.calendar__row"):
                tc = row.select_one(".calendar__time")
                if tc:
                    t = tc.get_text(strip=True)
                    if t: cur_time = t
                currency = row.select_one(".calendar__currency").get_text(strip=True) if row.select_one(".calendar__currency") else ""
                ic = row.select_one(".calendar__impact span")
                impact = ""
                if ic:
                    cls = " ".join(ic.get("class",[]))
                    if "high" in cls: impact = "high"
                    elif "medium" in cls: impact = "medium"
                title = row.select_one(".calendar__event-title").get_text(strip=True) if row.select_one(".calendar__event-title") else ""
                if not title or not currency: continue
                if currency not in {"USD","XAU"}: continue
                if impact not in {"high","medium"}: continue
                def _gt(sel):
                    el = row.select_one(sel)
                    return el.get_text(strip=True) if el else ""
                ev = NewsEvent(cur_time, currency, impact, title,
                               _gt(".calendar__actual"), _gt(".calendar__forecast"), _gt(".calendar__previous"))
                ev.dt_utc = _parse_ff_time(cur_time, target)
                events.append(ev)
        except Exception as e:
            log.error(f"FF scrape: {e}")
    events.sort(key=lambda e: e.dt_utc or datetime.min.replace(tzinfo=UTC))
    return events

def _next_red():
    now = datetime.now(UTC)
    for ev in state.news_events:
        if ev.is_red and ev.dt_utc and ev.dt_utc > now:
            return ev
    return None

def _news_block():
    now = datetime.now(UTC)
    for ev in state.news_events:
        if not ev.is_red or not ev.dt_utc: continue
        until = (ev.dt_utc - now).total_seconds()
        after = (now - ev.dt_utc).total_seconds()
        if 0 < until <= PRE_NEWS_BLOCK:
            return True, f"🚨 Red news in {int(until//60)}m: *{ev.title}*"
        if 0 < after <= POST_NEWS_WAIT:
            remain = int((POST_NEWS_WAIT - after)//60)
            return True, f"⏳ Post-news cooldown: {remain}m left (*{ev.title}*)"
    return False, ""

def _amharic_summary():
    now = datetime.now(UTC)
    reds = [e for e in state.news_events if e.is_red and e.dt_utc and e.dt_utc > now]
    oranges = [e for e in state.news_events if e.is_orange and e.dt_utc and e.dt_utc > now]
    if not reds and not oranges:
        return ("✅ *ደህንነቱ የተጠበቀ ቀን — Safe Day*\nዛሬ ለወርቅ (XAU/USD) ትልቅ ዜና የለም።\nቦቱ ያለ እገዳ ትሬዶችን ሊከፍት ይችላል።\n_ጥሩ የትሬዲንግ ቀን!_")
    if reds:
        titles = ", ".join(e.title[:28] for e in reds[:3])
        times = ", ".join(e.dt_utc.astimezone(NY_TZ).strftime("%I:%M%p ET") for e in reds[:3] if e.dt_utc)
        return (f"⚠️ *አደገኛ ቀን — Dangerous Day!*\nከፍተኛ ዜና: `{titles}`\nሰዓት: `{times}`\n\n📌 ዜናው ከ30 ደቂቃ በፊት ቦቱ ይቆማል።\nዜናው ካለቀ በኋላ 15 ደቂቃ ይጠብቃል።\n_ዛሬ ወርቅን በጥንቃቄ ይንግዱ!_")
    return ("🟡 *መካከለኛ ጥንቃቄ — Moderate Caution*\nዛሬ መካከለኛ ዜና አለ።\nቦቱ ይሰራል — ነገር ግን ጥንቃቄ ያስፈልጋል።")

def generate_news_chart():
    evs = state.news_events
    if not evs: return None
    now_ny = datetime.now(UTC).astimezone(NY_TZ)
    rows = []
    for ev in evs[:18]:
        t = ev.dt_utc.astimezone(NY_TZ).strftime("%I:%M%p") if ev.dt_utc else ev.time_et
        rows.append([t, ev.currency, "🔴" if ev.is_red else "🟠", ev.title[:40], ev.forecast or "—", ev.actual or "—"])
    cols = ["Time (ET)","Curr","Impact","Event","Forecast","Actual"]
    widths = [0.10,0.06,0.07,0.44,0.14,0.15]
    fig, ax = plt.subplots(figsize=(14, max(4,len(rows)*0.42+1.8)), facecolor="#0d1117")
    ax.axis("off")
    tbl = ax.table(cellText=rows, colLabels=cols, cellLoc="left", loc="center", colWidths=widths)
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(8)
    tbl.scale(1,1.5)
    for j in range(len(cols)):
        tbl[0,j].set_facecolor("#1f2937")
        tbl[0,j].set_text_props(color="#cdd9e5", fontweight="bold", fontfamily="monospace")
    for i, ev in enumerate(evs[:18]):
        rc = "#2d0000" if ev.is_red else "#2d1a00" if ev.is_orange else "#161b22"
        tc = "#ff6b6b" if ev.is_red else "#ffa94d" if ev.is_orange else "#90a4ae"
        for j in range(len(cols)):
            tbl[i+1,j].set_facecolor(rc)
            tbl[i+1,j].set_text_props(color=tc, fontfamily="monospace")
    nxt = state.next_red_event
    nxt_s = f" | Next🔴: {nxt.title[:22]}@{nxt.dt_utc.astimezone(NY_TZ).strftime('%I:%M%p ET')}" if nxt and nxt.dt_utc else ""
    ax.set_title(f"📰 Forex Factory — USD & XAU News · {now_ny.strftime('%A %b %d %Y %I:%M%p ET')}{nxt_s}", color="#cdd9e5", fontsize=9, fontfamily="monospace", pad=12)
    path = "/tmp/sniper_news.png"
    plt.tight_layout()
    plt.savefig(path, dpi=120, bbox_inches="tight", facecolor="#0d1117")
    plt.close(fig)
    return path

async def news_refresh_loop():
    while state.running:
        try:
            loop = asyncio.get_event_loop()
            events = await loop.run_in_executor(None, fetch_news)
            state.news_events = events
            state.news_last_fetch = time.time()
            state.next_red_event = _next_red()
            path = await loop.run_in_executor(None, generate_news_chart)
            state.news_chart_path = path
        except Exception as e:
            log.error(f"news_refresh_loop: {e}")
        await asyncio.sleep(NEWS_INTERVAL)

async def news_block_monitor():
    await asyncio.sleep(30)
    while state.running:
        try:
            block, reason = _news_block()
            if block and not state.block_trading:
                state.block_trading = True
                state.block_reason = reason
                log.info(f"🚫 BLOCKED: {reason}")
            elif not block and state.block_trading:
                state.block_trading = False
                state.block_reason = ""
                log.info("✅ UNBLOCKED")
        except Exception as e:
            log.error(f"news_block_monitor: {e}")
        await asyncio.sleep(60)

# ----------------------------------------------------------------------
# Telegram keyboards
# ----------------------------------------------------------------------
def kb_main():
    block_lbl = ("🚫 Blocked" if state.block_trading else ("🛑 Paused" if state.paused else "🟢 Active"))
    return {"inline_keyboard": [
        [{"text":"📊 Status","callback_data":"cmd_status"},{"text":"📰 News","callback_data":"cmd_news"}],
        [{"text":"📈 Chart","callback_data":"cmd_chart"},{"text":"📋 History","callback_data":"cmd_history"}],
        [{"text":"🔗 Connect Broker","callback_data":"cmd_connect"},{"text":"⚙️ Settings","callback_data":"cmd_settings"}],
        [{"text":"💰 Balance","callback_data":"cmd_balance"},{"text":block_lbl,"callback_data":"cmd_toggle_pause"}],
        [{"text":"🛑 Emergency Stop","callback_data":"cmd_stop"}],
    ]}

def kb_settings():
    r = state.risk_pct*100
    sm = "✅" if state.small_acc_mode else "○"
    md = "🎯 Sniper" if state.trading_mode=="SNIPER" else "⚡ Scalper"
    td = "✅" if state.top_down else "○"
    return {"inline_keyboard": [
        [{"text":f"🎛 Mode: {md}","callback_data":"cmd_mode_menu"}],
        [{"text":"💱 Select Pair","callback_data":"cmd_pair_menu"}],
        [{"text":f"{sm} 💎 Small Acc ($10)","callback_data":"cmd_small_acc"}],
        [{"text":f"🎯 Min Score: {state.min_score}","callback_data":"cmd_score_menu"}],
        [{"text":"⏱️ Timeframes","callback_data":"cmd_tf_menu"}],
        [{"text":f"{td} Top‑Down Analysis","callback_data":"cmd_top_down"}],
        [{"text":f"{'✅' if r==1 else '○'} 1% Risk","callback_data":"cmd_risk_1"},
         {"text":f"{'✅' if r==3 else '○'} 3% Risk","callback_data":"cmd_risk_3"},
         {"text":f"{'✅' if r==5 else '○'} 5% Risk","callback_data":"cmd_risk_5"}],
        [{"text":"⬅️ Back","callback_data":"cmd_back"}],
    ]}

def kb_mode():
    return {"inline_keyboard": [
        [{"text":"🎯 Switch to Sniper (H1/M15/M5)","callback_data":"cmd_mode_sniper"}],
        [{"text":"⚡ Switch to Scalper (M15/M5/M1)","callback_data":"cmd_mode_scalper"}],
        [{"text":"⬅️ Back","callback_data":"cmd_settings"}],
    ]}

def kb_pair_menu():
    rows, row = [], []
    for key, info in PAIR_REGISTRY.items():
        tick = "✅ " if key==state.pair_key else ""
        row.append({"text":tick+info[4], "callback_data":f"cmd_pair_{key}"})
        if len(row)==2:
            rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([{"text":"⬅️ Back","callback_data":"cmd_settings"}])
    return {"inline_keyboard":rows}

def kb_connect():
    return {"inline_keyboard": [
        [{"text":"📋 How to get Token","callback_data":"cmd_token_help"}],
        [{"text":"⬅️ Cancel","callback_data":"cmd_back"}],
    ]}

def kb_score():
    buttons = [{"text":str(i),"callback_data":f"cmd_score_{i}"} for i in range(10,101,10)]
    rows = [buttons[i:i+4] for i in range(0,len(buttons),4)]
    rows.append([{"text":"✏️ Custom","callback_data":"cmd_score_custom"}])
    rows.append([{"text":"⬅️ Back","callback_data":"cmd_settings"}])
    return {"inline_keyboard":rows}

def kb_tf():
    return {"inline_keyboard": [
        [{"text":"Trend TF","callback_data":"cmd_tf_trend"}],
        [{"text":"Execution TF","callback_data":"cmd_tf_exec"}],
        [{"text":"Confirmation TF","callback_data":"cmd_tf_conf"}],
        [{"text":"⬅️ Back","callback_data":"cmd_settings"}],
    ]}

def kb_tf_trend():
    return {"inline_keyboard": [
        [{"text":"H1","callback_data":"cmd_tf_trend_H1"},{"text":"M15","callback_data":"cmd_tf_trend_M15"}],
        [{"text":"M5","callback_data":"cmd_tf_trend_M5"},{"text":"M1","callback_data":"cmd_tf_trend_M1"}],
        [{"text":"⬅️ Back","callback_data":"cmd_tf_menu"}],
    ]}

def kb_tf_exec():
    return {"inline_keyboard": [
        [{"text":"M15","callback_data":"cmd_tf_exec_M15"},{"text":"M5","callback_data":"cmd_tf_exec_M5"},{"text":"M1","callback_data":"cmd_tf_exec_M1"}],
        [{"text":"⬅️ Back","callback_data":"cmd_tf_menu"}],
    ]}

def kb_tf_conf():
    return {"inline_keyboard": [
        [{"text":"M5","callback_data":"cmd_tf_conf_M5"},{"text":"M1","callback_data":"cmd_tf_conf_M1"}],
        [{"text":"⬅️ Back","callback_data":"cmd_tf_menu"}],
    ]}

# ----------------------------------------------------------------------
# Telegram sending helpers
# ----------------------------------------------------------------------
def tg_send(text, photo_path=None, reply_markup=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    markup = reply_markup if reply_markup is not None else kb_main()
    try:
        if photo_path:
            with open(photo_path,"rb") as fh:
                requests.post(f"{base}/sendPhoto", data={"chat_id":TELEGRAM_CHAT_ID,"caption":text[:1024],"reply_markup":json.dumps(markup),"parse_mode":"Markdown"}, files={"photo":fh}, timeout=20)
        else:
            requests.post(f"{base}/sendMessage", json={"chat_id":TELEGRAM_CHAT_ID,"text":text,"reply_markup":markup,"parse_mode":"Markdown"}, timeout=10)
    except Exception as e:
        log.error(f"tg_send: {e}")

def tg_answer(cqid, text=""):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery", json={"callback_query_id":cqid,"text":text}, timeout=5)
    except: pass

async def tg_async(text, photo_path=None, reply_markup=None):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: tg_send(text, photo_path, reply_markup))

# ----------------------------------------------------------------------
# SMC signal functions (fully restored)
# ----------------------------------------------------------------------
def _swing_pts(df, n=5):
    H, L = [], []
    for i in range(n, len(df)-n):
        if df["high"].iloc[i] == df["high"].iloc[i-n:i+n+1].max(): H.append(i)
        if df["low"].iloc[i] == df["low"].iloc[i-n:i+n+1].min(): L.append(i)
    return H, L

def _bos_choch(df):
    H, L = _swing_pts(df)
    if len(H)<2 or len(L)<2: return None
    lsh, psh = H[-1], H[-2]
    lsl, psl = L[-1], L[-2]
    lc = df["close"].iloc[-1]
    if lc > df["high"].iloc[lsh] and lsh > psh: return {"type":"BOS","direction":"BULLISH","level":df["high"].iloc[lsh]}
    if lc < df["low"].iloc[lsl] and lsl > psl: return {"type":"BOS","direction":"BEARISH","level":df["low"].iloc[lsl]}
    if df["high"].iloc[lsh] < df["high"].iloc[psh] and lc > df["high"].iloc[lsh]: return {"type":"CHoCH","direction":"BULLISH","level":df["high"].iloc[lsh]}
    if df["low"].iloc[lsl] > df["low"].iloc[psl] and lc < df["low"].iloc[lsl]: return {"type":"CHoCH","direction":"BEARISH","level":df["low"].iloc[lsl]}
    return None

def _pd_zone(df):
    hi, lo = df["high"].max(), df["low"].min()
    r = hi - lo
    if r==0: return "NEUTRAL", hi, lo
    fib_hi, fib_lo = hi - r*0.382, lo + r*0.382
    c = df["close"].iloc[-1]
    if c <= fib_lo: return "DISCOUNT", fib_hi, fib_lo
    if c >= fib_hi: return "PREMIUM", fib_hi, fib_lo
    return "EQUILIBRIUM", fib_hi, fib_lo

def _idm(df, direction):
    H, L = _swing_pts(df,3)
    if direction=="BULLISH" and len(L)>=2:
        lvl = df["low"].iloc[L[-2]]
        last = df.iloc[-1]
        return {"side":"BUY","level":lvl,"swept": last["low"] < lvl and last["close"] > lvl}
    if direction=="BEARISH" and len(H)>=2:
        lvl = df["high"].iloc[H[-2]]
        last = df.iloc[-1]
        return {"side":"SELL","level":lvl,"swept": last["high"] > lvl and last["close"] < lvl}
    return None

def _equal_hl(df):
    tol = 0.0003 if state.pair_key in ("EURUSD","GBPUSD") else 0.0005
    r = df.iloc[-25:]
    hs, ls = r["high"].values, r["low"].values
    for i in range(len(hs)-1,1,-1):
        for j in range(i-1, max(i-8,0), -1):
            if abs(hs[i]-hs[j])/hs[j] < tol:
                lvl = (hs[i]+hs[j])/2
                last = df.iloc[-1]
                if last["high"] > lvl and last["close"] < lvl: return {"side":"SELL","level":lvl,"swept":True,"type":"EQL_HIGHS"}
            if abs(ls[i]-ls[j])/ls[j] < tol:
                lvl = (ls[i]+ls[j])/2
                last = df.iloc[-1]
                if last["low"] < lvl and last["close"] > lvl: return {"side":"BUY","level":lvl,"swept":True,"type":"EQL_LOWS"}
    return None

def _ob(df, direction):
    lk = min(30, len(df)-3)
    r = df.iloc[-lk:].reset_index(drop=True)
    ab = (r["close"]-r["open"]).abs().mean()
    if direction=="BULLISH":
        for i in range(len(r)-3,1,-1):
            c, nc = r.iloc[i], r.iloc[i+1]
            if c["close"] < c["open"] and nc["close"] > c["high"] and abs(nc["close"]-nc["open"]) > ab*1.5:
                return {"type":"BULL","high":c["high"],"low":c["low"],"body_hi":max(c["open"],c["close"]),"body_lo":min(c["open"],c["close"]),"displacement":round(abs(nc["close"]-nc["open"])/ab,2)}
    elif direction=="BEARISH":
        for i in range(len(r)-3,1,-1):
            c, nc = r.iloc[i], r.iloc[i+1]
            if c["close"] > c["open"] and nc["close"] < c["low"] and abs(nc["close"]-nc["open"]) > ab*1.5:
                return {"type":"BEAR","high":c["high"],"low":c["low"],"body_hi":max(c["open"],c["close"]),"body_lo":min(c["open"],c["close"]),"displacement":round(abs(nc["close"]-nc["open"])/ab,2)}
    return None

def _fvg(df, ob):
    if ob is None: return None
    thr = 0.03 if state.pair_key in ("EURUSD","GBPUSD") else 0.05
    r = df.iloc[-min(25,len(df)-3):].reset_index(drop=True)
    if ob["type"]=="BULL":
        for i in range(len(r)-3,0,-1):
            c1, c3 = r.iloc[i], r.iloc[i+2]
            gp = (c3["low"]-c1["high"])/c1["high"]*100
            if c1["high"] < c3["low"] and gp >= thr: return {"type":"BULL","high":c3["low"],"low":c1["high"],"gap_pct":gp}
    elif ob["type"]=="BEAR":
        for i in range(len(r)-3,0,-1):
            c1, c3 = r.iloc[i], r.iloc[i+2]
            gp = (c1["low"]-c3["high"])/c1["low"]*100
            if c1["low"] > c3["high"] and gp >= thr: return {"type":"BEAR","high":c1["low"],"low":c3["high"],"gap_pct":gp}
    return None

def _atr(df, n=14):
    if len(df) < n+1: return 0.
    tr = np.maximum(df["high"]-df["low"], np.maximum(abs(df["high"]-df["close"].shift(1)), abs(df["low"]-df["close"].shift(1))))
    return float(tr.iloc[-n:].mean())

def _get_trend(tf_name):
    buf = state.h1_candles if tf_name=="H1" else state.m15_candles
    if len(buf) < 30: return "NEUTRAL"
    df = pd.DataFrame(list(buf))
    df.columns = ["time","open","high","low","close"]
    r = _bos_choch(df)
    if r:
        state.trend_bias = r["direction"]
    else:
        c = df["close"].values[-20:]
        state.trend_bias = "BULLISH" if np.polyfit(np.arange(len(c)),c,1)[0] > 0 else "BEARISH"
    return state.trend_bias

def sniper_score(ob, fvg, trap, idm, rsi, session, atr_ok, ema_ok, candle_ok, struct_type, disp):
    s, reasons = 0, []
    if fvg: s+=20; reasons.append("FVG +20")
    if trap and trap.get("swept"): s+=15; reasons.append("LiqTrap✅ +15")
    if idm and idm.get("swept"): s+=15; reasons.append("IDM✅ +15")
    if ob and disp>=2.0: s+=10; reasons.append(f"Disp{disp:.1f}x +10")
    if struct_type=="BOS": s+=10; reasons.append("BOS +10")
    if struct_type=="CHoCH": s+=8; reasons.append("CHoCH +8")
    if session=="OVERLAP": s+=10; reasons.append("Overlap +10")
    if session in ("LONDON","NY"): s+=7; reasons.append(f"{session} +7")
    if atr_ok: s+=5; reasons.append("ATR✅ +5")
    if ema_ok: s+=5; reasons.append("EMA✅ +5")
    if candle_ok: s+=5; reasons.append("Candle✅ +5")
    if ob:
        if ob["type"]=="BULL" and rsi<40: s+=5; reasons.append(f"RSI{rsi:.0f}(OS) +5")
        if ob["type"]=="BEAR" and rsi>60: s+=5; reasons.append(f"RSI{rsi:.0f}(OB) +5")
    return min(s,100), reasons

def compute_signal(tf="M15"):
    if tf=="M15": buf = state.m15_candles
    elif tf=="M5": buf = state.m5_candles
    elif tf=="M1": buf = state.m1_candles
    else: return None
    if len(buf) < 40: return None
    df = pd.DataFrame(list(buf))
    df.columns = ["time","open","high","low","close"]
    bias = _get_trend(state.trend_tf)
    if bias == "NEUTRAL": return None
    struct = _bos_choch(df)
    if struct is None or struct["direction"] != bias: return None
    pd_zone, fib_hi, fib_lo = _pd_zone(df)
    state.premium_discount = pd_zone
    if (bias=="BULLISH" and pd_zone!="DISCOUNT") or (bias=="BEARISH" and pd_zone!="PREMIUM"): return None
    idm = _idm(df, bias)
    state.active_idm = idm
    if idm is None: return None
    trap = _equal_hl(df)
    state.active_trap = trap
    if trap and trap["side"] != ("BUY" if bias=="BULLISH" else "SELL"):
        trap = None; state.active_trap = None
    ob_ = _ob(df, bias)
    if ob_ is None: return None
    state.active_ob = ob_
    fvg_ = _fvg(df, ob_)
    state.active_fvg = fvg_
    rsi_now = float(_rsi_calc(df["close"].values)[-1])
    session = get_session()
    state.session_now = session
    if state.pair_key=="XAUUSD" and session not in ("LONDON","NY","OVERLAP"): return None
    atr_v = _atr(df)
    thresholds = {"XAUUSD":0.5, "EURUSD":0.0005, "GBPUSD":0.0006, "US100":5.}
    atr_ok = atr_v >= thresholds.get(state.pair_key, .0001)
    state.atr_filter_ok = atr_ok
    if not atr_ok: return None
    ema50 = df["close"].ewm(span=50, adjust=False).mean().iloc[-1]
    price = df["close"].iloc[-1]
    ema_ok = (price > ema50 if bias=="BULLISH" else price < ema50)
    last = df.iloc[-1]
    candle_ok = (last["close"] > last["open"] if bias=="BULLISH" else last["close"] < last["open"])
    disp = ob_.get("displacement",1.)
    sc, score_reasons = sniper_score(ob_, fvg_, trap, idm, rsi_now, session, atr_ok, ema_ok, candle_ok, struct["type"], disp)
    state.ob_score = sc
    log.info(f"💡 Score details: {score_reasons} (total {sc})")
    if sc < state.min_score: return None
    reason = TradeReason()
    reason.h1_trend = f"{bias} (BOS/CHoCH confirmed)"
    reason.structure = struct["type"]
    reason.pd_zone = pd_zone
    reason.idm_sweep = f"{'✅ Swept' if idm.get('swept') else '⏳ Pending'} @{idm['level']:.5f}"
    reason.trap_sweep = f"{'✅ Swept' if trap and trap.get('swept') else 'None'}"
    reason.ob_type = f"{ob_['type']} disp:{disp:.1f}x"
    reason.fvg_present = fvg_ is not None
    reason.session = session
    reason.atr_state = f"{'✅ OK' if atr_ok else '⚠️ LOW'} ({atr_v:.5f})"
    reason.ema_confirm = f"Price {'above' if price>ema50 else 'below'} EMA50 ✅"
    reason.rsi_level = rsi_now
    reason.candle_conf = f"{'Bullish' if candle_ok and bias=='BULLISH' else 'Bearish'} close ✅"
    reason.score = sc
    reason.entry_logic = " + ".join(score_reasons)
    if bias=="BULLISH":
        entry = ob_["body_hi"]
        sl = ob_["low"] * 0.9995
    else:
        entry = ob_["body_lo"]
        sl = ob_["high"] * 1.0005
    risk = abs(entry - sl)
    if risk == 0: return None
    mult = 1 if bias=="BULLISH" else -1
    tp1 = entry + risk * state.tp1_r * mult
    tp2 = entry + risk * state.tp2_r * mult
    tp3 = entry + risk * state.tp3_r * mult
    stake = max(1., round(max(PAIR_REGISTRY[state.pair_key][3], state.account_balance * state.risk_pct),2))
    sig = {"direction":"BUY" if bias=="BULLISH" else "SELL", "entry":round(entry,5), "sl":round(sl,5),
           "tp1":round(tp1,5), "tp2":round(tp2,5), "tp3":round(tp3,5), "risk_r":round(risk,5), "stake":stake,
           "struct":struct["type"], "ob":ob_, "fvg":fvg_, "trap":trap, "idm":idm, "ob_score":sc, "rsi":rsi_now,
           "pd_zone":pd_zone, "fib_hi":fib_hi, "fib_lo":fib_lo, "session":session, "tf":tf, "bias":bias,
           "reason":reason, "score_reasons":score_reasons, "tv_confirmed":False, "ts":datetime.now(UTC).isoformat()}
    state.last_signal = sig
    return sig

def check_trade_mgmt():
    for cid, info in list(state.open_contracts.items()):
        sig = info.get("signal")
        if not sig: continue
        p = state.current_price
        d = info["direction"]
        if not info["be_moved"]:
            if (d=="BUY" and p >= sig["tp1"]) or (d=="SELL" and p <= sig["tp1"]):
                info["be_moved"] = True
                sig["sl"] = sig["entry"]
        buf = state.m15_candles if state.exec_tf=="M15" else state.m5_candles
        if len(buf) >= 10:
            df = pd.DataFrame(list(buf)[-30:])
            df.columns = ["time","open","high","low","close"]
            swh, swl = _swing_pts(df,3)
            if d=="BUY" and swl:
                t = df["low"].iloc[swl[-1]] * 0.9998
                if t > sig["sl"]: sig["sl"] = t
            elif d=="SELL" and swh:
                t = df["high"].iloc[swh[-1]] * 1.0002
                if t < sig["sl"]: sig["sl"] = t

# ----------------------------------------------------------------------
# Deriv WebSocket and trade execution (only for trading, not for OHLC)
# ----------------------------------------------------------------------
async def send_req(payload):
    if state.ws is None: raise RuntimeError("WS not connected")
    rid = state.req_id
    state.req_id += 1
    payload["req_id"] = rid
    fut = asyncio.get_event_loop().create_future()
    state.pending_reqs[rid] = fut
    await state.ws.send(json.dumps(payload))
    try:
        return await asyncio.wait_for(asyncio.shield(fut), timeout=20)
    except asyncio.TimeoutError:
        state.pending_reqs.pop(rid, None)
        raise

async def authorize(token=None):
    t = token or state.deriv_token
    if not t: raise RuntimeError("No token")
    resp = await send_req({"authorize": t})
    if "error" in resp: raise RuntimeError(resp["error"]["message"])
    auth = resp["authorize"]
    state.broker_connected = True
    state.account_id = auth.get("loginid","")
    state.account_type = auth.get("account_type","demo")
    if token:
        state.deriv_token = token
        try: TOKEN_FILE.write_text(token)
        except: pass
    log.info(f"Authorized: {state.account_id} ({state.account_type})")
    return auth

async def get_balance():
    r = await send_req({"balance":1,"subscribe":0})
    if "balance" in r:
        state.account_balance = r["balance"]["balance"]
        state.account_currency = r["balance"]["currency"]

async def _fetch_initial_candles(sym, nom):
    lbl = {3600:"H1",900:"M15",300:"M5",60:"M1"}
    for ag in GRAN_FALLBACKS.get(nom, [nom]):
        try:
            r = await send_req({"ticks_history":sym,"end":"latest","count":200,"granularity":ag,"style":"candles"})
            if "error" in r: continue
            raw = r.get("candles",[])
            if not raw: continue
            rows = [(int(c["epoch"]), float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])) for c in raw]
            state.gran_actual[nom] = ag
            if nom == 3600: state.h1_candles.extend(rows)
            elif nom == 900: state.m15_candles.extend(rows)
            elif nom == 300: state.m5_candles.extend(rows)
            elif nom == 60: state.m1_candles.extend(rows)
            log.info(f"✅ Initial {len(rows)} {lbl.get(nom,'?')} candles from Deriv")
            return len(rows)
        except Exception as e:
            log.warning(f"Initial fetch {nom} failed: {e}")
    return 0

async def _resolve_sym(key):
    pri, otc = PAIR_REGISTRY[key][0], PAIR_REGISTRY[key][1]
    return pri

async def subscribe_pair(key):
    state.h1_candles.clear(); state.m15_candles.clear(); state.m5_candles.clear(); state.m1_candles.clear()
    state.last_signal = None
    state.active_ob = state.active_fvg = state.active_trap = state.active_idm = None
    state.current_price = 0.
    state.trend_bias = "NEUTRAL"
    state.ob_score = 0
    state.premium_discount = "NEUTRAL"
    sym = await _resolve_sym(key)
    state.active_symbol = sym
    h = await _fetch_initial_candles(sym,3600)
    m = await _fetch_initial_candles(sym,900)
    f = await _fetch_initial_candles(sym,300)
    o = await _fetch_initial_candles(sym,60)
    log.info(f"Initial candles loaded: H1:{h} M15:{m} M5:{f} M1:{o}")
    return h,m,f,o

async def execute_trade(signal):
    if not state.broker_connected: log.error("Broker not connected"); return None
    if state.paused: log.error("Bot paused"); return None
    if state.block_trading: log.error(f"Trading blocked: {state.block_reason}"); return None
    direction = signal["direction"]
    amount = signal["stake"]
    contract_type = "MULTUP" if direction=="BUY" else "MULTDOWN"
    params = {
        "buy":1, "price":round(amount,2),
        "parameters": {
            "contract_type":contract_type,
            "symbol":state.active_symbol or PAIR_REGISTRY[state.pair_key][0],
            "amount":round(amount,2),
            "currency":state.account_currency,
            "multiplier":10, "basis":"stake", "stop_out":1
        }
    }
    log.info(f"🚀 EXECUTING TRADE: {direction} {amount:.2f} {state.account_currency}")
    log.info(f"📡 Full request: {json.dumps(params)}")
    try:
        resp = await send_req(params)
        log.info(f"📡 Full response: {json.dumps(resp)}")
        if "error" in resp:
            log.error(f"❌ Trade execution FAILED: {resp['error'].get('message')}")
            return None
        if "buy" not in resp: return None
        cid = str(resp["buy"]["contract_id"])
        log.info(f"✅ Trade EXECUTED! Contract ID: {cid}")
        state.open_contracts[cid] = {
            "direction":direction, "entry":state.current_price, "amount":amount,
            "signal":signal, "be_moved":False, "opened_at":time.time()
        }
        state.trade_count += 1
        return cid
    except Exception as e:
        log.error(f"Trade execution EXCEPTION: {e}")
        return None

async def close_contract(cid):
    try:
        r = await send_req({"sell":cid,"price":0})
        if "error" in r: return False
        state.open_contracts.pop(cid, None)
        return True
    except: return False

async def close_all():
    for cid in list(state.open_contracts.keys()):
        await close_contract(cid)
    return len(state.open_contracts)

# ----------------------------------------------------------------------
# Price updater using API Ninjas (updates last candle every 10 seconds)
# ----------------------------------------------------------------------
async def price_updater_loop():
    while state.running:
        try:
            price = get_current_price()
            if price > 0:
                state.current_price = price
                # Update the last candle in each timeframe
                for buf, gran in [(state.m1_candles,60), (state.m5_candles,300), (state.m15_candles,900), (state.h1_candles,3600)]:
                    if buf:
                        last = buf[-1]
                        new_candle = (last[0], last[1], max(last[2], price), min(last[3], price), price)
                        buf[-1] = new_candle
                log.debug(f"Price updated via API Ninjas: {price}")
            else:
                log.warning("API Ninjas returned 0 price")
        except Exception as e:
            log.error(f"Price updater error: {e}")
        await asyncio.sleep(10)

# ----------------------------------------------------------------------
# WebSocket message handler (only for trade responses, balance)
# ----------------------------------------------------------------------
async def handle_msg(msg):
    rid = msg.get("req_id")
    if rid and rid in state.pending_reqs:
        fut = state.pending_reqs.pop(rid)
        if not fut.done(): fut.set_result(msg)
        return
    mt = msg.get("msg_type","")
    if mt == "proposal_open_contract":
        poc = msg.get("proposal_open_contract",{})
        cid = str(poc.get("contract_id",""))
        if cid not in state.open_contracts: return
        profit = float(poc.get("profit",0))
        status = poc.get("status","")
        exit_s = float(poc.get("exit_tick", state.current_price) or state.current_price)
        if status in ("sold","expired"):
            info = state.open_contracts.pop(cid, {})
            if profit > 0: state.wins += 1
            else: state.losses += 1
            state.total_pnl += profit
            tnum = state.trade_count
            sig = info.get("signal", {}) or {}
            state.trade_history.append({"num":tnum,"id":cid,"pair":state.pair_display,"direction":info.get("direction","?"),
                                        "entry":info.get("entry",0.),"exit":exit_s,"pnl":round(profit,2),"win":profit>0,
                                        "score":sig.get("ob_score",0),"session":sig.get("session","?"),
                                        "ts":datetime.now(UTC).strftime("%m/%d %H:%M")})
            if len(state.trade_history) > 50: state.trade_history.pop(0)
            chart = generate_chart(state.m15_candles if state.exec_tf=="M15" else state.m5_candles, state.exec_tf,
                                   entry_price=info.get("entry"), exit_price=exit_s, direction=info.get("direction"), pnl=profit, chart_type="exit")
            sign = "+" if profit>0 else ""
            wr = f"{state.wins/(state.wins+state.losses)*100:.1f}%" if (state.wins+state.losses)>0 else "N/A"
            reason_obj = sig.get("reason")
            post_report = reason_obj.build_report(info.get("direction","?")) if reason_obj else ""
            amharic_r = reason_obj.build_amharic(info.get("direction","?")) if reason_obj else ""
            await tg_async(f"{'✅ WIN' if profit>0 else '❌ LOSS'} `#{tnum}` — *Post-Trade Report*\n\n"
                           f"Pair:`{state.pair_display}` Dir:`{info.get('direction','?')}`\nEntry:`{info.get('entry',0):.5f}` Exit:`{exit_s:.5f}`\n"
                           f"P&L: `{sign}{profit:.2f} {state.account_currency}`\nScore:`{sig.get('ob_score',0)}/100` Session:`{sig.get('session','?')}`\n"
                           f"W/L:{state.wins}/{state.losses} WR:{wr} Total:{state.total_pnl:+.2f} Bal:{state.account_balance:.2f}\n\n{post_report}",
                           photo_path=chart)
            if amharic_r: await tg_async(amharic_r)
    elif mt == "balance":
        state.account_balance = msg["balance"]["balance"]
        state.account_currency = msg["balance"]["currency"]
    elif "error" in msg:
        log.warning(f"API error: {msg['error']}")

# ----------------------------------------------------------------------
# WebSocket reader and loop (only for trade execution)
# ----------------------------------------------------------------------
async def ws_reader(ws):
    async for raw in ws:
        try: await handle_msg(json.loads(raw))
        except Exception as e: log.error(f"Handler: {e}")

async def ws_run(ws):
    state.ws = ws
    async def setup():
        await asyncio.sleep(0.1)
        await authorize()
        await get_balance()
        await subscribe_pair(state.pair_key)
        await tg_async(f"{market_header()}\n🤖 *SMC SNIPER EA v5.9 (API Ninjas)*\nBalance: {state.account_balance:.2f} {state.account_currency}\nPair: {state.pair_display}", reply_markup=kb_main())
    task = asyncio.ensure_future(setup())
    try: await ws_reader(ws)
    finally: task.cancel()

async def ws_loop():
    delay = 5
    while state.running:
        try:
            async with websockets.connect(DERIV_WS_BASE, ping_interval=25, ping_timeout=10) as ws:
                delay = 5
                await ws_run(ws)
        except Exception as e:
            log.error(f"WS: {e}")
            await asyncio.sleep(delay)
            delay = min(delay*2,60)

# ----------------------------------------------------------------------
# Background loops (trading, chart scan)
# ----------------------------------------------------------------------
async def chart_loop():
    await asyncio.sleep(50)
    while state.running:
        try:
            if state.current_price>0 and len(state.m15_candles)>=20:
                sig = compute_signal(state.exec_tf)
                sess = get_session()
                state.session_now = sess
                log.info(f"📡 Scan | {state.pair_display} | {state.trading_mode} | Bias:{state.trend_bias} | Zone:{state.premium_discount} | Score:{state.ob_score}/100 | Sess:{sess} | Price:{state.current_price:.5f} | Signal:{'✅ '+sig['direction'] if sig else '—'}")
        except Exception as e:
            log.error(f"chart_loop: {e}")
        await asyncio.sleep(CHART_INTERVAL)

async def trading_loop():
    await asyncio.sleep(35)
    FORCE_TEST_TRADE = False   # Set to True to test trade execution
    while state.running:
        try:
            if state.paused or state.block_trading or state.current_price==0 or not state.broker_connected or not is_market_open():
                await asyncio.sleep(30); continue
            state.market_open = True
            check_trade_mgmt()
            if time.time() - state.last_trade_ts < state.signal_cooldown:
                await asyncio.sleep(30); continue
            log.info(f"🔍 Scanning {state.pair_display} ...")
            sig = compute_signal(state.exec_tf)
            if FORCE_TEST_TRADE and sig is None and state.current_price>0:
                log.warning("⚠️ NO SIGNAL – forcing test BUY trade")
                sig = {
                    "direction":"BUY", "entry":state.current_price, "sl":state.current_price-5.0,
                    "tp1":state.current_price+10.0, "tp2":state.current_price+20.0, "tp3":state.current_price+30.0,
                    "stake":1.0, "ob_score":100, "score_reasons":["TEST"], "tv_confirmed":False,
                    "session":get_session(), "struct":"TEST", "pd_zone":"TEST"
                }
            if sig and sig['ob_score'] >= state.min_score:
                log.info(f"🎯 SIGNAL DETECTED! Score: {sig['ob_score']}/100 → executing")
                if state.trading_mode=="SNIPER" and state.top_down:
                    sig_conf = compute_signal(state.conf_tf)
                    if not (sig_conf and sig_conf["direction"]==sig["direction"]):
                        log.info(f"Confirmation mismatch – skipping"); continue
                chart = generate_chart(state.m15_candles if state.exec_tf=="M15" else state.m5_candles, state.exec_tf,
                                       entry_price=sig["entry"], direction=sig["direction"], chart_type="entry")
                await tg_async(f"🚀 ENTRY – {state.pair_display}\nScore: {sig['ob_score']}/100\nDir: {sig['direction']}\nEntry: {sig['entry']}\nSL: {sig['sl']}\nStake: {sig['stake']:.2f}", photo_path=chart)
                cid = await execute_trade(sig)
                if cid: state.last_trade_ts = time.time()
        except Exception as e:
            log.error(f"trading_loop: {e}")
        await asyncio.sleep(30)

# ----------------------------------------------------------------------
# Telegram polling and command handlers
# ----------------------------------------------------------------------
async def tg_poll_loop():
    if not TELEGRAM_TOKEN: return
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, lambda: requests.post(f"{base}/deleteWebhook", json={"drop_pending_updates":True}, timeout=10))
        log.info("Webhook cleared.")
    except: pass
    offset, ec = 0, 0
    while state.running:
        try:
            r = await loop.run_in_executor(None, lambda: requests.get(f"{base}/getUpdates", params={"offset":offset,"timeout":20,"allowed_updates":["message","callback_query"]}, timeout=25))
            data = r.json()
            if not data.get("ok"):
                ec += 1
                await asyncio.sleep(10)
                continue
            ec = 0
            for upd in data.get("result",[]):
                offset = upd["update_id"]+1
                await _handle_upd(upd)
        except Exception as e:
            ec += 1
            log.error(f"TG poll: {e}")
            await asyncio.sleep(min(5*ec,60))
        await asyncio.sleep(0.5)

async def _handle_upd(upd):
    if "message" in upd:
        text = upd["message"].get("text","").strip()
        cid = str(upd["message"]["chat"]["id"])
        if TELEGRAM_CHAT_ID and cid != TELEGRAM_CHAT_ID: return
        if state.awaiting_token and text and not text.startswith("/"):
            await _process_token(text); return
        if state.awaiting_custom_score:
            try:
                score = int(text.strip())
                if 10 <= score <= 100:
                    state.min_score = score
                    state.awaiting_custom_score = False
                    await tg_async(f"✅ Minimum score set to {score}", reply_markup=kb_settings())
                else:
                    await tg_async("❌ Please enter a number between 10 and 100.", reply_markup=kb_score())
            except:
                await tg_async("❌ Invalid number.", reply_markup=kb_score())
            return
        await _cmd(text)
    elif "callback_query" in upd:
        cq = upd["callback_query"]
        data = cq.get("data","")
        cqid = cq["id"]
        cid = str(cq["message"]["chat"]["id"])
        if TELEGRAM_CHAT_ID and cid != TELEGRAM_CHAT_ID:
            tg_answer(cqid, "Unauthorized"); return
        tg_answer(cqid)
        await _cmd(data)

async def _process_token(token: str):
    state.awaiting_token = False
    if not state.ws:
        await tg_async("⚠️ Bot not connected yet. Please wait and try again.", reply_markup=kb_main())
        return
    await tg_async("🔄 Authenticating with Deriv...", reply_markup=kb_main())
    try:
        await authorize(token)
        await get_balance()
        acct_icon = "🔴 REAL" if state.account_type=="real" else "🟢 DEMO"
        await tg_async(f"✅ *Broker Connected Successfully!*\n\nAccount: `{state.account_id}`\nType: {acct_icon}\nBalance: `{state.account_balance:.2f} {state.account_currency}`\n\n_Sniper Brain active._", reply_markup=kb_main())
        await subscribe_pair(state.pair_key)
    except Exception as e:
        await tg_async(f"❌ Authentication failed: `{e}`", reply_markup=kb_connect())

async def _cmd(cmd):
    cmd = cmd.lower().strip()
    if cmd in ("/start","/help","cmd_back"):
        mkt = market_header()
        bl = ("🚫 "+state.block_reason if state.block_trading else ("🛑 PAUSED" if state.paused else "🟢 AUTONOMOUS"))
        conn = "✅ Connected" if state.broker_connected else "❌ Not connected — tap 🔗 Connect Broker"
        md_lbl = "🎯 Sniper" if state.trading_mode=="SNIPER" else "⚡ Scalper"
        await tg_async(f"{mkt}\n\n🤖 *SMC SNIPER EA v5.9*\n\nStatus: {bl}\nBroker: {conn}\nStrategy: `{md_lbl}`\nAcct: `{state.account_id}` ({state.account_type.upper()})\nBal: `{state.account_balance:.2f} {state.account_currency}`\nPair: `{state.pair_display}` Risk:`{state.risk_pct*100:.0f}%`\nMin Score: `{state.min_score}/100`\nH1:`{len(state.h1_candles)}` M15:`{len(state.m15_candles)}` M5:`{len(state.m5_candles)}` M1:`{len(state.m1_candles)}`", reply_markup=kb_main())
    elif cmd in ("/status","cmd_status"):
        mkt = market_header()
        bl = ("🚫 "+state.block_reason if state.block_trading else ("🛑 PAUSED" if state.paused else "🟢 SCANNING"))
        nxt = state.next_red_event
        nxt_s = f"\n🔴 Next Red: *{nxt.title}* @ `{nxt.dt_utc.astimezone(NY_TZ).strftime('%I:%M%p ET')}`" if nxt and nxt.dt_utc else ""
        sig_s = ""
        if state.last_signal:
            s = state.last_signal
            sig_s = f"\n\n*Last Setup Detected:*\n`{s['direction']}` @ `{s['entry']}` SL:`{s['sl']}`\nScore:`{s['ob_score']}/100` {s['struct']} {s['pd_zone']}"
        md_lbl = "🎯 SMC Sniper" if state.trading_mode=="SNIPER" else "⚡ Quick Scalper"
        await tg_async(f"{mkt}\n\n⚡ *Sniper Status*\nMode: {bl}\nStrategy: `{md_lbl}`\nPair: `{state.pair_display}` Price:`{state.current_price:.5f}`\nBias:`{state.trend_bias}` Zone:`{state.premium_discount}`\nSession:`{state.session_now}` ATR:`{'OK' if state.atr_filter_ok else 'LOW'}`\nOpen Trades:`{len(state.open_contracts)}`\nAcct:`{state.account_type.upper()}` Bal:`{state.account_balance:.2f}`\nW/L:`{state.wins}/{state.losses}` P&L:`{state.total_pnl:+.2f}`{nxt_s}{sig_s}", reply_markup=kb_main())
    elif cmd in ("/news","cmd_news"):
        if time.time()-state.news_last_fetch > 3600 or not state.news_events:
            await tg_async("⏳ Fetching from Forex Factory...", reply_markup=kb_main())
            loop = asyncio.get_event_loop()
            evs = await loop.run_in_executor(None, fetch_news)
            state.news_events = evs
            state.news_last_fetch = time.time()
            state.next_red_event = _next_red()
            path = await loop.run_in_executor(None, generate_news_chart)
            state.news_chart_path = path
        nxt = state.next_red_event
        ni = f"\n\n🔴 Next Red: *{nxt.title}* @ `{nxt.dt_utc.astimezone(NY_TZ).strftime('%I:%M%p ET')}` (~{int((nxt.dt_utc-datetime.now(UTC)).total_seconds()//60)}m away)" if nxt and nxt.dt_utc else ""
        bl_s, bl_r = _news_block()
        bi = f"\n\n{bl_r}" if bl_s else "\n\n✅ No active news block."
        await tg_async(f"{market_header()}\n\n📰 *Economic Calendar — Today*\n\n{_amharic_summary()}{ni}{bi}", photo_path=state.news_chart_path, reply_markup=kb_main())
    elif cmd in ("/chart","cmd_chart"):
        buf = state.m15_candles if state.exec_tf=="M15" else state.m5_candles
        if len(buf) >= 20:
            path = generate_chart(buf, state.exec_tf, chart_type="live")
            if path:
                pe = ("🟢" if state.premium_discount=="DISCOUNT" else "🔴" if state.premium_discount=="PREMIUM" else "⚪")
                blk = " 🚫NEWS BLOCK" if state.block_trading else ""
                md_lbl = "🎯 Sniper" if state.trading_mode=="SNIPER" else "⚡ Scalper"
                await tg_async(f"{market_header()}\n\n📊 *{state.pair_display} {state.exec_tf}* [{md_lbl}]{blk}\nPrice:`{state.current_price:.5f}` Bias:`{state.trend_bias}`\nZone:{pe}`{state.premium_discount}` Score:`{state.ob_score}/100`\nSession:`{state.session_now}` ATR:`{'OK' if state.atr_filter_ok else 'LOW'}`\nIDM:{'✅' if state.active_idm and state.active_idm.get('swept') else '⏳'} Trap:{'✅' if state.active_trap and state.active_trap.get('swept') else '⏳'}", photo_path=path, reply_markup=kb_main())
                return
        await tg_async(f"{market_header()}\n\n⚠️ *Chart not ready*\n{state.exec_tf}:`{len(buf)}` bars (needs 20+) sym:`{state.active_symbol}`\nWS:`{'connected' if state.ws else 'disconnected'}`", reply_markup=kb_main())
    elif cmd in ("/history","cmd_history"):
        if not state.trade_history:
            await tg_async("📋 No trade history yet.", reply_markup=kb_main()); return
        path = generate_history_chart()
        lines = ["📋 *Trade History — Last 10*\n"]
        for t in state.trade_history[-10:]:
            s = "+" if t["pnl"]>0 else ""
            lines.append(f"{'✅' if t['win'] else '❌'} `#{t['num']}` {t['direction']} `{t['entry']:.5f}`→`{t['exit']:.5f}` `{s}{t['pnl']:.2f}` sc:`{t.get('score',0)}` _{t['ts']}_")
        wr = f"{state.wins/(state.wins+state.losses)*100:.1f}%" if (state.wins+state.losses)>0 else "N/A"
        lines.append(f"\nP&L:`{state.total_pnl:+.2f}` WR:`{wr}`")
        await tg_async("\n".join(lines), photo_path=path, reply_markup=kb_main())
    elif cmd in ("/balance","cmd_balance"):
        if state.ws: await get_balance()
        wr = f"{state.wins/(state.wins+state.losses)*100:.1f}%" if (state.wins+state.losses)>0 else "N/A"
        acct = "🔴 REAL" if state.account_type=="real" else "🟢 DEMO"
        await tg_async(f"💰 *Account Balance*\n`{state.account_balance:.2f} {state.account_currency}`\nType: {acct} ID: `{state.account_id}`\n\nTrades:`{state.trade_count}` W/L:`{state.wins}/{state.losses}` WR:`{wr}`\nTotal P&L:`{state.total_pnl:+.2f}`", reply_markup=kb_main())
    elif cmd in ("/connect","cmd_connect"):
        state.awaiting_token = True
        await tg_async("🔗 *Connect Broker — Deriv*\n\nPlease send your *Deriv API Token* as the next message.\n\nThe token must have:\n• ✅ Read scope\n• ✅ Trade scope\n\nGet it at:\n`app.deriv.com/account/api-token`\n\n_Your token is saved securely on the server._", reply_markup=kb_connect())
    elif cmd == "cmd_token_help":
        await tg_async("📋 *How to get your Deriv API Token:*\n\n1. Open `app.deriv.com`\n2. Login → Account Settings\n3. API Token → Create new token\n4. Enable: *Read + Trade*\n5. Copy and paste the token here\n\n_For demo account: use your demo credentials_", reply_markup=kb_connect())
    elif cmd in ("/settings","cmd_settings"):
        md_lbl = "🎯 Sniper" if state.trading_mode=="SNIPER" else "⚡ Scalper"
        await tg_async(f"⚙️ *Settings*\nStrategy:`{md_lbl}`\nPair:`{state.pair_display}` sym:`{state.active_symbol}`\nRisk:`{state.risk_pct*100:.0f}%` TPs:`{state.tp1_r}R/{state.tp2_r}R/{state.tp3_r}R`\nMin Score:`{state.min_score}/100`\nSmall Acc:`{'ON 💎' if state.small_acc_mode else 'OFF'}`", reply_markup=kb_settings())
    elif cmd in ("/mode","cmd_mode_menu"):
        await tg_async("🎛 *Select Trading Strategy Mode:*", reply_markup=kb_mode())
    elif cmd == "cmd_mode_sniper":
        state.trading_mode = "SNIPER"; state.min_score=75; state.trend_tf="H1"; state.exec_tf="M15"; state.conf_tf="M5"
        await tg_async("✅ Strategy updated to *🎯 SMC Sniper*.\nTIMEFRAMES: [H1, M15, M5] | Min Score: 75", reply_markup=kb_settings())
    elif cmd == "cmd_mode_scalper":
        state.trading_mode = "SCALPER"; state.min_score=60; state.trend_tf="M15"; state.exec_tf="M5"; state.conf_tf="M1"
        await tg_async("✅ Strategy updated to *⚡ Quick Scalper*.\nTIMEFRAMES: [M15, M5, M1] | Min Score: 60", reply_markup=kb_settings())
    elif cmd == "cmd_pair_menu":
        await tg_async("💱 *Select Pair:*", reply_markup=kb_pair_menu())
    elif cmd.startswith("cmd_pair_"):
        key = cmd.replace("cmd_pair_","").upper()
        if key in PAIR_REGISTRY:
            old = state.pair_key
            state.pair_key = key
            state.small_acc_mode = False
            if state.ws:
                try:
                    await tg_async(f"⏳ Switching to `{PAIR_REGISTRY[key][4]}`...", reply_markup=kb_main())
                    await subscribe_pair(key)
                    await tg_async(f"💱 Switched → `{state.pair_display}` ✅", reply_markup=kb_main())
                except Exception as e:
                    state.pair_key = old
                    await tg_async(f"❌ Switch failed: {e}", reply_markup=kb_main())
            else:
                await tg_async(f"💱 Pair → `{state.pair_display}` (next connect)", reply_markup=kb_main())
    elif cmd == "cmd_small_acc":
        if state.small_acc_mode:
            state.small_acc_mode = False; state.risk_pct=0.01; state.tp1_r,state.tp2_r,state.tp3_r = 2.,4.,6.
            await tg_async("💎 Small Acc *OFF* — 1% risk", reply_markup=kb_settings())
        else:
            state.small_acc_mode = True
            if state.pair_key=="XAUUSD":
                state.risk_pct=0.02; state.tp1_r,state.tp2_r,state.tp3_r = 1.5,3.,5.
                note = "XAU/USD 2% risk (tight TPs)"
            else:
                state.pair_key="GBPUSD"; state.risk_pct=0.05; state.tp1_r,state.tp2_r,state.tp3_r = 1.5,3.,4.5
                note = "GBP/USD 5% risk"
            if state.ws: await subscribe_pair(state.pair_key)
            await tg_async(f"💎 *Small Acc ON*\n{note}", reply_markup=kb_settings())
    elif cmd == "cmd_risk_1":
        state.risk_pct=0.01; state.small_acc_mode=False; await tg_async("✅ Risk → 1%", reply_markup=kb_settings())
    elif cmd == "cmd_risk_3":
        state.risk_pct=0.03; state.small_acc_mode=False; await tg_async("✅ Risk → 3%", reply_markup=kb_settings())
    elif cmd == "cmd_risk_5":
        state.risk_pct=0.05; state.small_acc_mode=False; await tg_async("✅ Risk → 5%", reply_markup=kb_settings())
    # New settings handlers
    elif cmd == "cmd_score_menu":
        await tg_async("🎯 Select minimum score for signal execution:", reply_markup=kb_score())
    elif cmd.startswith("cmd_score_"):
        val = cmd.replace("cmd_score_","")
        if val == "custom":
            state.awaiting_custom_score = True
            await tg_async("✏️ Enter a number between 10 and 100:", reply_markup=None)
        else:
            state.min_score = int(val)
            await tg_async(f"✅ Minimum score set to {state.min_score}", reply_markup=kb_settings())
    elif cmd == "cmd_tf_menu":
        await tg_async("⏱️ Select which timeframe to change:", reply_markup=kb_tf())
    elif cmd == "cmd_tf_trend":
        await tg_async("📈 Select trend timeframe:", reply_markup=kb_tf_trend())
    elif cmd == "cmd_tf_exec":
        await tg_async("⚡ Select execution timeframe:", reply_markup=kb_tf_exec())
    elif cmd == "cmd_tf_conf":
        await tg_async("✅ Select confirmation timeframe:", reply_markup=kb_tf_conf())
    elif cmd.startswith("cmd_tf_trend_"):
        state.trend_tf = cmd.replace("cmd_tf_trend_","")
        await tg_async(f"✅ Trend TF set to {state.trend_tf}", reply_markup=kb_tf())
    elif cmd.startswith("cmd_tf_exec_"):
        state.exec_tf = cmd.replace("cmd_tf_exec_","")
        await tg_async(f"✅ Execution TF set to {state.exec_tf}", reply_markup=kb_tf())
    elif cmd.startswith("cmd_tf_conf_"):
        state.conf_tf = cmd.replace("cmd_tf_conf_","")
        await tg_async(f"✅ Confirmation TF set to {state.conf_tf}", reply_markup=kb_tf())
    elif cmd == "cmd_top_down":
        state.top_down = not state.top_down
        await tg_async(f"🔄 Top‑down analysis turned {'ON' if state.top_down else 'OFF'}", reply_markup=kb_settings())
    elif cmd in ("/stop","cmd_stop"):
        state.paused = True
        n = await close_all()
        await tg_async(f"🛑 *Emergency Stop*\nClosed `{n}` contracts. Bot *PAUSED*.", reply_markup=kb_main())
    elif cmd == "cmd_toggle_pause":
        if state.paused or state.block_trading:
            state.paused = False; state.block_trading=False; state.block_reason=""
            await tg_async("▶️ Bot *RESUMED* — Sniper Brain active.", reply_markup=kb_main())
        else:
            state.paused = True
            await tg_async("⏸ Bot *PAUSED* — press Resume to restart.", reply_markup=kb_main())

# ----------------------------------------------------------------------
# Health server
# ----------------------------------------------------------------------
async def health(req):
    return web.json_response({"status":"running","version":"5.9","source":"API Ninjas + Deriv"})

async def start_health():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"Health server on port {PORT}")

# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
async def main():
    log.info("Starting SMC SNIPER EA v5.9 with API Ninjas price source")
    _load_saved_token()
    await asyncio.gather(
        start_health(),
        ws_loop(),
        price_updater_loop(),
        trading_loop(),
        tg_poll_loop(),
        chart_loop(),
        news_refresh_loop(),
        news_block_monitor(),
    )

def _load_saved_token():
    try:
        if TOKEN_FILE.exists():
            t = TOKEN_FILE.read_text().strip()
            if t:
                state.deriv_token = t
                log.info("Loaded saved Deriv token")
    except Exception as e:
        log.warning(f"Token load: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutdown")
        state.running = False
