"""
╔══════════════════════════════════════════════════════════════════════╗
║        SMC SNIPER EA v5.4 — Fully Customizable Trading Bot          ║
║     Senior Quant SMC | Sniper Brain | News Shield | Broker Connect   ║
║   Duplicate Candle Fix | Chart Fix | Supabase | Custom Settings      ║
║               [Score / Timeframe / Top-Down Analysis]                ║
╚══════════════════════════════════════════════════════════════════════╝
"""

# ==================================================
# 1. STANDARD LIBRARY IMPORTS
# ==================================================
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

# ==================================================
# 2. SETUP LOGGING IMMEDIATELY
# ==================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("SNIPER")

# ==================================================
# 3. REQUIRED THIRD-PARTY IMPORTS
# ==================================================
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mplfinance as mpf
import numpy as np
import pandas as pd
import requests
import websockets
from aiohttp import web
from bs4 import BeautifulSoup

# ==================================================
# 4. OPTIONAL IMPORTS (Supabase & TradingView)
# ==================================================
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
supabase = None

if SUPABASE_URL and SUPABASE_KEY:
    try:
        from supabase import create_client, Client
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        log.info("✅ Supabase client initialized")
    except ImportError:
        log.warning("⚠️ Supabase package not installed. Install with: pip install supabase")
    except Exception as e:
        log.warning(f"⚠️ Supabase init failed: {e}")
else:
    log.info("ℹ️ Supabase not configured (optional)")

try:
    from tradingview_ta import TA_Handler, Interval
    TV_AVAILABLE = True
    log.info("✅ TradingView TA available")
except ImportError:
    TV_AVAILABLE = False
    log.info("ℹ️ TradingView TA not installed (optional)")
except Exception as e:
    TV_AVAILABLE = False
    log.warning(f"⚠️ TradingView init failed: {e}")

# ==================================================
# 5. CONSTANTS & REGISTRIES
# ==================================================
NY_TZ = ZoneInfo("America/New_York")
UTC = timezone.utc

PAIR_REGISTRY: Dict[str, tuple] = {
    "XAUUSD": ("frxXAUUSD", "OTC_XAUUSD", 0.01, 1.0, "XAU/USD 🥇", "METAL", "FX_IDC:XAUUSD"),
    "EURUSD": ("frxEURUSD", "OTC_EURUSD", 0.0001, 1.0, "EUR/USD 🇪🇺", "FOREX", "FX:EURUSD"),
    "GBPUSD": ("frxGBPUSD", "OTC_GBPUSD", 0.0001, 1.0, "GBP/USD 🇬🇧", "FOREX", "FX:GBPUSD"),
    "US100":  ("frxUS100",  "OTC_NDX",    0.1,    1.0, "NASDAQ 💻",   "INDEX", "NASDAQ:US100"),
}

GRAN_FALLBACKS = {
    3600: [3600, 7200],
    900:  [900, 600, 1800],
    300:  [300, 180, 600],
    60:   [60, 120],
}

SESSIONS = {
    "ASIAN":   (0,  8),
    "LONDON":  (8,  13),
    "OVERLAP": (13, 17),
    "NY":      (17, 22),
}

BEST_SESSIONS = {
    "METAL": ["LONDON", "NY", "OVERLAP"],
    "FOREX": ["LONDON", "NY", "OVERLAP"],
    "INDEX": ["NY", "OVERLAP"],
}

MARKET_OPEN_HOUR  = 22
MARKET_CLOSE_HOUR = 21

TOKEN_FILE = Path("/tmp/.deriv_token")

PRE_NEWS_BLOCK = 30 * 60
POST_NEWS_WAIT = 15 * 60
NEWS_INTERVAL  = 12 * 3600
CHART_INTERVAL = int(os.getenv("CHART_INTERVAL", "300"))
PORT           = int(os.getenv("PORT", "8080"))

DERIV_APP_ID     = os.getenv("DERIV_APP_ID", "1089")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DERIV_WS_BASE    = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"

# ==================================================
# 6. DATA CLASSES
# ==================================================
class NewsEvent:
    __slots__ = ("time_et","currency","impact","title","actual","forecast","prev","dt_utc")
    def __init__(self, time_et, currency, impact, title, actual="", forecast="", prev=""):
        self.time_et  = time_et
        self.currency = currency
        self.impact   = impact
        self.title    = title
        self.actual   = actual
        self.forecast = forecast
        self.prev     = prev
        self.dt_utc   = None

    @property
    def is_red(self):    return self.impact == "high"
    @property
    def is_orange(self): return self.impact == "medium"


class TradeReason:
    def __init__(self):
        self.h1_trend    = ""
        self.structure   = ""
        self.pd_zone     = ""
        self.idm_sweep   = ""
        self.trap_sweep  = ""
        self.ob_type     = ""
        self.fvg_present = False
        self.session     = ""
        self.atr_state   = ""
        self.ema_confirm = ""
        self.rsi_level   = 0.0
        self.score       = 0
        self.candle_conf = ""
        self.entry_logic = ""
        self.tv_confirm  = ""

    def build_report(self, direction: str) -> str:
        arrow = "📈 BUY" if direction == "BUY" else "📉 SELL"
        lines = [
            "🧠 *Sniper Brain — Trade Reasoning*",
            f"Direction: *{arrow}*",
            f"Score: `{self.score}/100`",
            "",
            "*Multi-Timeframe Analysis:*",
            f"• Trend: `{self.h1_trend}`",
            f"• Structure: `{self.structure}`",
            f"• Zone: `{self.pd_zone}`",
            "",
            "*Liquidity & Traps:*",
            f"• IDM Sweep: `{self.idm_sweep}`",
            f"• Trap Sweep: `{self.trap_sweep}`",
            f"• OB Type: `{self.ob_type}`",
            f"• FVG Present: `{'✅ Yes' if self.fvg_present else '⚠️ No'}`",
            "",
            "*Market Filters:*",
            f"• Session: `{self.session}`",
            f"• ATR: `{self.atr_state}`",
            f"• EMA50: `{self.ema_confirm}`",
            f"• RSI: `{self.rsi_level:.1f}`",
            f"• Candle: `{self.candle_conf}`",
            "",
            "*Entry Logic:*",
            f"`{self.entry_logic}`",
        ]
        if self.tv_confirm:
            lines.append(f"\n*TradingView Confirmation:*\n`{self.tv_confirm}`")
        return "\n".join(lines)

    def build_amharic(self, direction: str) -> str:
        arrow = "ወደ ላይ (BUY)" if direction == "BUY" else "ወደ ታች (SELL)"
        return (
            f"🤖 *ቦቱ ዝርዝር ምክንያት:*\n"
            f"አቅጣጫ: `{arrow}`\n"
            f"• ዋና ዝንባሌ: `{self.h1_trend}`\n"
            f"• IDM ተወስዷል: `{self.idm_sweep}`\n"
            f"• ወጥመድ ተወስዷል: `{self.trap_sweep}`\n"
            f"• OB ዓይነት: `{self.ob_type}`\n"
            f"• ዞን: `{self.pd_zone}`\n"
            f"• ሰሽን: `{self.session}`\n"
            f"• ውሳኔ ምክንያት: `{self.entry_logic}`"
        )


# ==================================================
# 7. BOT STATE
# ==================================================
class BotState:
    def __init__(self):
        self.deriv_token      = os.getenv("DERIV_API_TOKEN", "")
        self.broker_connected = False
        self.account_type     = "unknown"
        self.account_id       = ""
        self.account_balance  = 0.0
        self.account_currency = "USD"
        self.awaiting_token   = False
        self.awaiting_custom_score = False  # for manual score input

        self.running       = True
        self.paused        = False
        self.autonomous    = True
        self.block_trading = False
        self.block_reason  = ""

        self.trading_mode = "SNIPER"
        self.min_score    = int(os.getenv("MIN_SCORE", "75"))
        self.trend_tf     = "H1"
        self.exec_tf      = "M15"
        self.conf_tf      = "M5"
        self.top_down     = True   # enable top-down confirmation by default

        self.pair_key       = "XAUUSD"
        self.active_symbol  = ""
        self.risk_pct       = 0.01
        self.tp1_r          = 2.0
        self.tp2_r          = 4.0
        self.tp3_r          = 6.0
        self.small_acc_mode = False

        self.h1_candles  = deque(maxlen=1000)
        self.m15_candles = deque(maxlen=1000)
        self.m5_candles  = deque(maxlen=1000)
        self.m1_candles  = deque(maxlen=1000)
        self.gran_actual = {3600: 3600, 900: 900, 300: 300, 60: 60}

        self.current_price    = 0.0
        self.trend_bias       = "NEUTRAL"
        self.last_signal      = None
        self.active_ob        = None
        self.active_fvg       = None
        self.active_trap      = None
        self.active_idm       = None
        self.premium_discount = "NEUTRAL"
        self.ob_score         = 0
        self.session_now      = "ASIAN"
        self.atr_filter_ok    = True
        self.market_open      = True

        self.ws             = None
        self.req_id         = 1
        self.pending_reqs   = {}
        self.subscribed_sym = None
        self.ws_task        = None

        self.open_contracts : Dict[str, dict] = {}
        self.trade_count    = 0
        self.wins           = 0
        self.losses         = 0
        self.total_pnl      = 0.0
        self.trade_history  : List[dict] = []
        self.last_trade_ts  = 0.0
        self.signal_cooldown = 300

        self.news_events     : List[NewsEvent] = []
        self.news_last_fetch = 0.0
        self.next_red_event  : Optional[NewsEvent] = None
        self.news_chart_path : Optional[str] = None

        self.tv_last_check = 0.0
        self.tv_confirmed = False
        self.tv_signal = None

    @property
    def pair_info(self):     return PAIR_REGISTRY[self.pair_key]
    @property
    def pair_display(self):  return self.pair_info[4]
    @property
    def pair_category(self): return self.pair_info[5]
    @property
    def tv_symbol(self):     return self.pair_info[6] if len(self.pair_info) > 6 else None


state = BotState()

def _load_saved_token():
    try:
        if TOKEN_FILE.exists():
            t = TOKEN_FILE.read_text().strip()
            if t:
                state.deriv_token = t
                log.info("Loaded saved Deriv token")
    except Exception as e:
        log.warning(f"Token load: {e}")

# ==================================================
# 8. MARKET HOURS & SESSION
# ==================================================
def is_market_open() -> bool:
    now = datetime.now(UTC)
    wd  = now.weekday()
    h   = now.hour
    if wd == 4 and h >= 21: return False
    if wd == 5:              return False
    if wd == 6 and h < 22:  return False
    return True

def time_to_next_open() -> str:
    now          = datetime.now(UTC)
    wd           = now.weekday()
    days_to_sun  = (6 - wd) % 7
    if days_to_sun == 0 and now.hour >= 22:
        days_to_sun = 7
    next_open = (now + timedelta(days=days_to_sun)).replace(
        hour=22, minute=0, second=0, microsecond=0)
    delta = next_open - now
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m = rem // 60
    return f"{h}h {m}m"

def get_session() -> str:
    h = datetime.now(UTC).hour
    if 8  <= h < 13: return "LONDON"
    if 13 <= h < 17: return "OVERLAP"
    if 17 <= h < 22: return "NY"
    return "ASIAN"

def market_header() -> str:
    if is_market_open():
        return f"🟢 Market is *OPEN* · Session: `{get_session()}`"
    return f"🔴 Market is *CLOSED* · Opens in `{time_to_next_open()}`"

# ==================================================
# 9. TRADINGVIEW CONFIRMATION (optional)
# ==================================================
def check_tradingview(direction: str) -> tuple:
    if not TV_AVAILABLE or not state.tv_symbol:
        return False, "TradingView not available"
    try:
        interval_map = {
            "M1": Interval.INTERVAL_1_MINUTE,
            "M5": Interval.INTERVAL_5_MINUTES,
            "M15": Interval.INTERVAL_15_MINUTES,
            "H1": Interval.INTERVAL_1_HOUR,
        }
        tv_interval = interval_map.get(state.conf_tf, Interval.INTERVAL_15_MINUTES)
        handler = TA_Handler(
            symbol=state.tv_symbol,
            screener="forex" if state.pair_category == "FOREX" else "cfd",
            exchange="FX_IDC" if state.pair_key == "XAUUSD" else "FX",
            interval=tv_interval
        )
        analysis = handler.get_analysis()
        summary = analysis.summary
        tv_direction = "BUY" if summary.get("RECOMMENDATION", "").upper() in ["BUY", "STRONG_BUY"] else "SELL"
        if tv_direction == direction:
            return True, f"TV confirms {direction} (summary: {summary.get('RECOMMENDATION', 'NEUTRAL')})"
        else:
            return False, f"TV mismatch: {tv_direction} vs {direction}"
    except Exception as e:
        log.warning(f"TradingView check failed: {e}")
        return False, f"TV error: {e}"

# ==================================================
# 10. KEYBOARDS (Telegram)
# ==================================================
def kb_main():
    block_lbl = ("🚫 Blocked" if state.block_trading
                 else ("🛑 Paused" if state.paused else "🟢 Active"))
    return {"inline_keyboard": [
        [{"text": "📊 Status", "callback_data": "cmd_status"},
         {"text": "📰 News",   "callback_data": "cmd_news"}],
        [{"text": "📈 Chart",  "callback_data": "cmd_chart"},
         {"text": "📋 History","callback_data": "cmd_history"}],
        [{"text": "🔗 Connect Broker", "callback_data": "cmd_connect"},
         {"text": "⚙️ Settings",       "callback_data": "cmd_settings"}],
        [{"text": "💰 Balance","callback_data": "cmd_balance"},
         {"text": f"{block_lbl}", "callback_data": "cmd_toggle_pause"}],
        [{"text": "🛑 Emergency Stop", "callback_data": "cmd_stop"}],
    ]}

def kb_settings():
    r  = state.risk_pct * 100
    sm = "✅" if state.small_acc_mode else "○"
    md = "🎯 Sniper" if state.trading_mode == "SNIPER" else "⚡ Scalper"
    td = "✅" if state.top_down else "○"
    return {"inline_keyboard": [
        [{"text": f"🎛 Mode: {md}", "callback_data": "cmd_mode_menu"}],
        [{"text": "💱 Select Pair", "callback_data": "cmd_pair_menu"}],
        [{"text": f"{sm} 💎 Small Acc ($10)", "callback_data": "cmd_small_acc"}],
        [{"text": f"🎯 Min Score: {state.min_score}", "callback_data": "cmd_score_menu"}],
        [{"text": f"⏱️ Timeframes", "callback_data": "cmd_tf_menu"}],
        [{"text": f"{td} Top‑Down Analysis", "callback_data": "cmd_top_down"}],
        [{"text": f"{'✅' if r==1 else '○'} 1% Risk", "callback_data": "cmd_risk_1"},
         {"text": f"{'✅' if r==3 else '○'} 3% Risk", "callback_data": "cmd_risk_3"},
         {"text": f"{'✅' if r==5 else '○'} 5% Risk", "callback_data": "cmd_risk_5"}],
        [{"text": "⬅️ Back", "callback_data": "cmd_back"}],
    ]}

def kb_mode():
    return {"inline_keyboard": [
        [{"text": "🎯 Switch to Sniper (H1/M15/M5)",  "callback_data": "cmd_mode_sniper"}],
        [{"text": "⚡ Switch to Scalper (M15/M5/M1)", "callback_data": "cmd_mode_scalper"}],
        [{"text": "⬅️ Back", "callback_data": "cmd_settings"}],
    ]}

def kb_pair_menu():
    rows, row = [], []
    for key, info in PAIR_REGISTRY.items():
        tick = "✅ " if key == state.pair_key else ""
        row.append({"text": tick + info[4], "callback_data": f"cmd_pair_{key}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "⬅️ Back", "callback_data": "cmd_settings"}])
    return {"inline_keyboard": rows}

def kb_connect():
    return {"inline_keyboard": [
        [{"text": "📋 How to get Token", "callback_data": "cmd_token_help"}],
        [{"text": "⬅️ Cancel",           "callback_data": "cmd_back"}],
    ]}

def kb_score():
    # buttons for 10,20,...,100 and a custom option
    buttons = []
    for i in range(10, 101, 10):
        buttons.append({"text": str(i), "callback_data": f"cmd_score_{i}"})
    # group in rows of 4
    rows = [buttons[i:i+4] for i in range(0, len(buttons), 4)]
    rows.append([{"text": "✏️ Custom", "callback_data": "cmd_score_custom"}])
    rows.append([{"text": "⬅️ Back", "callback_data": "cmd_settings"}])
    return {"inline_keyboard": rows}

def kb_tf():
    return {"inline_keyboard": [
        [{"text": "Trend TF", "callback_data": "cmd_tf_trend"}],
        [{"text": "Execution TF", "callback_data": "cmd_tf_exec"}],
        [{"text": "Confirmation TF", "callback_data": "cmd_tf_conf"}],
        [{"text": "⬅️ Back", "callback_data": "cmd_settings"}],
    ]}

def kb_tf_trend():
    return {"inline_keyboard": [
        [{"text": "H1", "callback_data": "cmd_tf_trend_H1"},
         {"text": "M15", "callback_data": "cmd_tf_trend_M15"}],
        [{"text": "M5", "callback_data": "cmd_tf_trend_M5"},
         {"text": "M1", "callback_data": "cmd_tf_trend_M1"}],
        [{"text": "⬅️ Back", "callback_data": "cmd_tf_menu"}],
    ]}

def kb_tf_exec():
    return {"inline_keyboard": [
        [{"text": "M15", "callback_data": "cmd_tf_exec_M15"},
         {"text": "M5", "callback_data": "cmd_tf_exec_M5"},
         {"text": "M1", "callback_data": "cmd_tf_exec_M1"}],
        [{"text": "⬅️ Back", "callback_data": "cmd_tf_menu"}],
    ]}

def kb_tf_conf():
    return {"inline_keyboard": [
        [{"text": "M5", "callback_data": "cmd_tf_conf_M5"},
         {"text": "M1", "callback_data": "cmd_tf_conf_M1"}],
        [{"text": "⬅️ Back", "callback_data": "cmd_tf_menu"}],
    ]}

# ==================================================
# 11. TELEGRAM HELPER FUNCTIONS
# ==================================================
def tg_send(text: str, photo_path: str = None, reply_markup=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    base   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    markup = reply_markup if reply_markup is not None else kb_main()
    try:
        if photo_path:
            with open(photo_path, "rb") as fh:
                r = requests.post(f"{base}/sendPhoto", data={
                    "chat_id":      TELEGRAM_CHAT_ID,
                    "caption":      text[:1024],
                    "reply_markup": json.dumps(markup),
                    "parse_mode":   "Markdown",
                }, files={"photo": fh}, timeout=20)
        else:
            r = requests.post(f"{base}/sendMessage", json={
                "chat_id":      TELEGRAM_CHAT_ID,
                "text":         text,
                "reply_markup": markup,
                "parse_mode":   "Markdown",
            }, timeout=10)
        if r.status_code not in (200, 201):
            log.warning(f"TG {r.status_code}: {r.text[:100]}")
    except Exception as e:
        log.error(f"tg_send: {e}")

def tg_answer(cqid: str, text: str = ""):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": cqid, "text": text},
            timeout=5)
    except Exception:
        pass

async def tg_async(text: str, photo_path: str = None, reply_markup=None):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: tg_send(text, photo_path, reply_markup))

# ==================================================
# 12. NEWS ENGINE (Forex Factory)
# ==================================================
FF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

def _parse_ff_time(ts: str, base: datetime) -> Optional[datetime]:
    ts = ts.strip().lower()
    if not ts or ts in ("all day", "tentative", "", "—"):
        return base.replace(hour=0, minute=0, second=0)
    try:
        t = datetime.strptime(ts, "%I:%M%p")
        return base.replace(
            hour=t.hour, minute=t.minute, second=0,
            tzinfo=NY_TZ).astimezone(UTC)
    except Exception:
        return None

def fetch_news() -> List[NewsEvent]:
    events: List[NewsEvent] = []
    today = datetime.now(NY_TZ)
    for offset in (0, 1):
        target = today + timedelta(days=offset)
        url = f"https://www.forexfactory.com/calendar?day={target.strftime('%b%d.%Y').lower()}"
        try:
            resp = requests.get(url, headers=FF_HEADERS, timeout=15)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            cur_time = ""
            for row in soup.select("tr.calendar__row"):
                tc = row.select_one(".calendar__time")
                if tc:
                    t = tc.get_text(strip=True)
                    if t:
                        cur_time = t
                cur_cell = row.select_one(".calendar__currency")
                currency = cur_cell.get_text(strip=True) if cur_cell else ""
                ic = row.select_one(".calendar__impact span")
                impact = ""
                if ic:
                    cls = " ".join(ic.get("class", []))
                    if "high" in cls:
                        impact = "high"
                    elif "medium" in cls:
                        impact = "medium"
                ec = row.select_one(".calendar__event-title")
                title = ec.get_text(strip=True) if ec else ""
                if not title or not currency:
                    continue
                if currency not in {"USD", "XAU"}:
                    continue
                if impact not in {"high", "medium"}:
                    continue

                def _gt(sel):
                    el = row.select_one(sel)
                    return el.get_text(strip=True) if el else ""

                ev = NewsEvent(
                    cur_time, currency, impact, title,
                    _gt(".calendar__actual"),
                    _gt(".calendar__forecast"),
                    _gt(".calendar__previous"))
                ev.dt_utc = _parse_ff_time(cur_time, target)
                events.append(ev)
        except Exception as e:
            log.error(f"FF scrape: {e}")
    events.sort(key=lambda e: e.dt_utc or datetime.min.replace(tzinfo=UTC))
    log.info(f"📰 Fetched {len(events)} relevant events")
    return events

def _next_red() -> Optional[NewsEvent]:
    now = datetime.now(UTC)
    for ev in state.news_events:
        if ev.is_red and ev.dt_utc and ev.dt_utc > now:
            return ev
    return None

def _news_block() -> tuple:
    now = datetime.now(UTC)
    for ev in state.news_events:
        if not ev.is_red or not ev.dt_utc:
            continue
        until = (ev.dt_utc - now).total_seconds()
        after = (now - ev.dt_utc).total_seconds()
        if 0 < until <= PRE_NEWS_BLOCK:
            return True, f"🚨 Red news in {int(until//60)}m: *{ev.title}*"
        if 0 < after <= POST_NEWS_WAIT:
            remain = int((POST_NEWS_WAIT - after) // 60)
            return True, f"⏳ Post-news cooldown: {remain}m left (*{ev.title}*)"
    return False, ""

def _amharic_summary() -> str:
    now = datetime.now(UTC)
    reds = [e for e in state.news_events if e.is_red and e.dt_utc and e.dt_utc > now]
    oranges = [e for e in state.news_events if e.is_orange and e.dt_utc and e.dt_utc > now]
    if not reds and not oranges:
        return ("✅ *ደህንነቱ የተጠበቀ ቀን — Safe Day*\n"
                "ዛሬ ለወርቅ (XAU/USD) ትልቅ ዜና የለም።\n"
                "ቦቱ ያለ እገዳ ትሬዶችን ሊከፍት ይችላል።\n"
                "_ጥሩ የትሬዲንግ ቀን!_")
    if reds:
        titles = ", ".join(e.title[:28] for e in reds[:3])
        times = ", ".join(e.dt_utc.astimezone(NY_TZ).strftime("%I:%M%p ET") for e in reds[:3] if e.dt_utc)
        return (f"⚠️ *አደገኛ ቀን — Dangerous Day!*\n"
                f"ከፍተኛ ዜና: `{titles}`\n"
                f"ሰዓት: `{times}`\n\n"
                f"📌 ዜናው ከ30 ደቂቃ በፊት ቦቱ ይቆማል።\n"
                f"ዜናው ካለቀ በኋላ 15 ደቂቃ ይጠብቃል።\n"
                "_ዛሬ ወርቅን በጥንቃቄ ይንግዱ!_")
    return ("🟡 *መካከለኛ ጥንቃቄ — Moderate Caution*\n"
            "ዛሬ መካከለኛ ዜና አለ።\n"
            "ቦቱ ይሰራል — ነገር ግን ጥንቃቄ ያስፈልጋል።")

def generate_news_chart() -> Optional[str]:
    evs = state.news_events
    if not evs:
        return None
    now_ny = datetime.now(UTC).astimezone(NY_TZ)
    rows = []
    for ev in evs[:18]:
        t = ev.dt_utc.astimezone(NY_TZ).strftime("%I:%M%p") if ev.dt_utc else ev.time_et
        rows.append([t, ev.currency, "🔴" if ev.is_red else "🟠", ev.title[:40], ev.forecast or "—", ev.actual or "—"])
    cols = ["Time (ET)", "Curr", "Impact", "Event", "Forecast", "Actual"]
    widths = [0.10, 0.06, 0.07, 0.44, 0.14, 0.15]
    fig, ax = plt.subplots(figsize=(14, max(4, len(rows)*0.42+1.8)), facecolor="#0d1117")
    ax.axis("off")
    tbl = ax.table(cellText=rows, colLabels=cols, cellLoc="left", loc="center", colWidths=widths)
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(8)
    tbl.scale(1, 1.5)
    for j in range(len(cols)):
        tbl[0, j].set_facecolor("#1f2937")
        tbl[0, j].set_text_props(color="#cdd9e5", fontweight="bold", fontfamily="monospace")
    for i, ev in enumerate(evs[:18]):
        rc = "#2d0000" if ev.is_red else "#2d1a00" if ev.is_orange else "#161b22"
        tc = "#ff6b6b" if ev.is_red else "#ffa94d" if ev.is_orange else "#90a4ae"
        for j in range(len(cols)):
            tbl[i+1, j].set_facecolor(rc)
            tbl[i+1, j].set_text_props(color=tc, fontfamily="monospace")
    nxt = state.next_red_event
    nxt_s = f" | Next🔴: {nxt.title[:22]}@{nxt.dt_utc.astimezone(NY_TZ).strftime('%I:%M%p ET')}" if nxt and nxt.dt_utc else ""
    ax.set_title(f"📰 Forex Factory — USD & XAU News · {now_ny.strftime('%A %b %d %Y %I:%M%p ET')}{nxt_s}",
                 color="#cdd9e5", fontsize=9, fontfamily="monospace", pad=12)
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

# ==================================================
# 13. CHART ENGINE (FIXED: panel indices and ratios)
# ==================================================
BG = "#0d1117"; PB = "#161b22"; GR = "#1e2a38"
BC = "#00e676"; RC = "#ff1744"
OBB = "#00bcd4"; OBR = "#ff9800"
FC = "#ce93d8"; TC = "#ffeb3b"; IC = "#80cbc4"
EC = "#2979ff"; SC = "#f44336"
TPC = ["#69f0ae", "#40c4ff", "#b388ff"]

def _ax_s(ax):
    ax.set_facecolor(PB)
    ax.tick_params(colors="#90a4ae", labelsize=7)
    for s in ax.spines.values():
        s.set_edgecolor(GR)
    ax.grid(axis="y", color=GR, linewidth=0.4, alpha=0.6)

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

def _swing_pts(df: pd.DataFrame, n: int = 5):
    H, L = [], []
    for i in range(n, len(df)-n):
        if df["high"].iloc[i] == df["high"].iloc[i-n:i+n+1].max():
            H.append(i)
        if df["low"].iloc[i] == df["low"].iloc[i-n:i+n+1].min():
            L.append(i)
    return H, L

def _dedupe_candles(df: pd.DataFrame) -> pd.DataFrame:
    mask = ~(
        (df["open"] == df["open"].shift(1)) &
        (df["high"] == df["high"].shift(1)) &
        (df["low"] == df["low"].shift(1)) &
        (df["close"] == df["close"].shift(1))
    )
    df = df[mask].reset_index(drop=True)
    if "time" in df.columns:
        df = df.drop_duplicates(subset=["time"], keep="last").reset_index(drop=True)
    return df

def generate_chart(candles: deque, tf: str = "M15", entry_price: float = None,
                   exit_price: float = None, direction: str = None, pnl: float = None,
                   chart_type: str = "live", reason: "TradeReason" = None) -> Optional[str]:
    if len(candles) < 20:
        log.info(f"Chart skipped — only {len(candles)} candles")
        return None

    df = pd.DataFrame(list(candles))
    df.columns = ["time", "open", "high", "low", "close"]
    df = df.astype({"open": float, "high": float, "low": float, "close": float})

    df = df[(df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0) & (df["close"] > 0)]
    med = df["close"].median()
    df = df[(df["close"] > med * 0.5) & (df["close"] < med * 2.0)]
    df = _dedupe_candles(df)

    SHOW = 40
    df = df.tail(SHOW).reset_index(drop=True)
    if len(df) < 10:
        return None

    df["date"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("date", inplace=True)

    raw_min, raw_max = df["low"].min(), df["high"].max()
    if state.pair_key == "XAUUSD":
        y_min, y_max = raw_min - 0.5, raw_max + 0.5
    else:
        pr = raw_max - raw_min
        y_min, y_max = raw_min - pr * 0.05, raw_max + pr * 0.05

    ema21 = df["close"].ewm(span=21, adjust=False).mean()
    add_plots = [mpf.make_addplot(ema21, color='#ffeb3b', width=1.5, panel=0)]
    if len(df) >= 50:
        ema50 = df["close"].ewm(span=50, adjust=False).mean()
        add_plots.append(mpf.make_addplot(ema50, color='#78909c', width=1.2, linestyle='--', panel=0))

    # RSI - now placed on panel=1 (since volume=False, panels: 0 = price, 1 = RSI)
    rsi = _rsi_calc(df["close"].values)
    rsi_series = pd.Series(rsi, index=df.index)
    add_plots.append(mpf.make_addplot(rsi_series, color='#90a4ae', width=1.2, panel=1, ylabel='RSI'))
    rsi_70 = pd.Series([70] * len(df), index=df.index)
    rsi_30 = pd.Series([30] * len(df), index=df.index)
    add_plots.append(mpf.make_addplot(rsi_70, color='#d50000', width=0.6, linestyle='--', panel=1))
    add_plots.append(mpf.make_addplot(rsi_30, color='#00c853', width=0.6, linestyle='--', panel=1))

    hlines_dict = {'hlines': [], 'colors': [], 'linestyle': [], 'linewidths': []}
    if state.last_signal:
        sig = state.last_signal
        hlines_dict['hlines'].extend([sig["entry"], sig["sl"]])
        hlines_dict['colors'].extend(['#00e5ff', '#ff5252'])
        hlines_dict['linestyle'].extend(['-', '--'])
        hlines_dict['linewidths'].extend([2, 1.5])
        for tk, tc in zip(["tp1","tp2","tp3"], ['#69f0ae','#b2ff59','#eeff41']):
            if tk in sig:
                hlines_dict['hlines'].append(sig[tk])
                hlines_dict['colors'].append(tc)
                hlines_dict['linestyle'].append('-.')
                hlines_dict['linewidths'].append(1)
    if entry_price:
        hlines_dict['hlines'].append(entry_price)
        hlines_dict['colors'].append('#00e5ff')
        hlines_dict['linestyle'].append('-')
        hlines_dict['linewidths'].append(2.5)
    if exit_price:
        xc = '#00c853' if (pnl and pnl > 0) else '#d50000'
        hlines_dict['hlines'].append(exit_price)
        hlines_dict['colors'].append(xc)
        hlines_dict['linestyle'].append('--')
        hlines_dict['linewidths'].append(2)

    mc_style = mpf.make_marketcolors(
        up='#00c853', down='#d50000',
        edge={'up':'#00e676','down':'#ff1744'},
        wick={'up':'#00c853','down':'#d50000'},
        volume={'up':'#00c853','down':'#d50000'}
    )
    s = mpf.make_mpf_style(
        marketcolors=mc_style, figcolor='#0d1117', facecolor='#0d1117',
        edgecolor='#30363d', gridcolor='#21262d', gridstyle='--',
        gridaxis='both', y_on_right=False,
        rc={'font.size':8, 'axes.labelcolor':'#8b949e', 'axes.titlesize':10,
            'xtick.color':'#8b949e', 'ytick.color':'#8b949e'}
    )

    title = (f"{'📡 LIVE' if chart_type=='live' else '🎯 ENTRY' if chart_type=='entry' else '🏁 CLOSED'} "
             f"{state.pair_display} · {tf} · {'🟢' if is_market_open() else '🔴'} "
             f"Bias:{state.trend_bias}  Zone:{state.premium_discount} · {state.current_price:.2f} "
             f"Sess:{state.session_now}{' 🚫NEWS' if state.block_trading else ''}")

    try:
        fig, axes = mpf.plot(
            df, type='candle', style=s, title=title, ylabel='Price', volume=False,
            addplot=add_plots, figsize=(12,8), panel_ratios=(5, 1.5), returnfig=True,
            tight_layout=True, scale_width_adjustment=dict(candle=0.8, volume=0.8),
            update_width_config=dict(candle_linewidth=1.2, candle_width=0.8),
            hlines=hlines_dict,
            warn_too_much_data=1000, ylim=(y_min, y_max)
        )
    except Exception as e:
        log.error(f"mplfinance.plot error: {e}")
        return None

    ax_main = axes[0]

    def _in_range(p): return y_min <= p <= y_max
    if state.active_ob:
        ob = state.active_ob
        if _in_range(ob["low"]) and _in_range(ob["high"]):
            oc = '#2962ff' if ob["type"] == "BULL" else '#d50000'
            ax_main.axhspan(ob["low"], ob["high"], alpha=0.15, color=oc, zorder=1)
            ax_main.axhline(ob["high"], color=oc, lw=1.2, alpha=0.8)
            ax_main.axhline(ob["low"], color=oc, lw=1.2, alpha=0.8)
    if state.active_fvg:
        fvg = state.active_fvg
        if _in_range(fvg["low"]) and _in_range(fvg["high"]):
            ax_main.axhspan(fvg["low"], fvg["high"], alpha=0.1, color='#7c4dff', hatch='///', zorder=1)
    if state.active_idm:
        idm = state.active_idm
        if _in_range(idm["level"]):
            ax_main.axhline(idm["level"], color='#ffab00', ls=':', lw=1.5, alpha=0.9)
    if state.active_trap:
        trap = state.active_trap
        if _in_range(trap["level"]):
            ax_main.axhline(trap["level"], color='#e040fb', ls=':', lw=1.5, alpha=0.9)

    ts_now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    info_text = (f"SMC SNIPER v5.4 [{state.trading_mode}] · {state.pair_display} · "
                 f"Bal:{state.account_balance:.2f}{state.account_currency} · "
                 f"Risk:{state.risk_pct*100:.0f}% · Last {len(df)} candles · {ts_now}")
    fig.text(0.5, 0.01, info_text, ha='center', va='bottom', fontsize=6, color='#8b949e', fontfamily='monospace')

    path = f"/tmp/sniper_chart_{chart_type}.png"
    fig.savefig(path, dpi=130, bbox_inches="tight", facecolor='#0d1117', edgecolor='none')
    plt.close(fig)
    return path

def generate_history_chart() -> Optional[str]:
    h = state.trade_history[-20:]
    if not h:
        return None
    fig, (ax1, ax2) = plt.subplots(2,1, figsize=(14,9), facecolor=BG, gridspec_kw={"height_ratios":[2,1]})
    for a in (ax1, ax2):
        _ax_s(a)
    labels = [f"#{t['num']}" for t in h]
    pnls = [t["pnl"] for t in h]
    colors = [BC if p>0 else RC for p in pnls]
    bars = ax1.bar(labels, pnls, color=colors, alpha=.85, ec=GR)
    ax1.axhline(0, color=GR, lw=.8)
    for bar, val in zip(bars, pnls):
        s = "+" if val >= 0 else ""
        ax1.text(bar.get_x()+bar.get_width()/2, bar.get_height()+(0.05 if val>=0 else -.15),
                 f"{s}{val:.2f}", ha="center", va="bottom", color="#cdd9e5", fontsize=7, fontfamily="monospace")
    wr = f"{state.wins/(state.wins+state.losses)*100:.1f}%" if (state.wins+state.losses)>0 else "N/A"
    ax1.set_title(f"📋 Trade History {state.wins}W/{state.losses}L WR:{wr} P&L:{state.total_pnl:+.2f} {state.account_currency} Bal:{state.account_balance:.2f}",
                  color="#cdd9e5", fontsize=10, fontfamily="monospace")
    ax1.set_ylabel("P&L", color="#90a4ae", fontsize=9)
    cum = np.cumsum(pnls)
    cc = BC if cum[-1] >= 0 else RC
    ax2.plot(labels, cum, color=cc, lw=1.8, marker="o", ms=4)
    ax2.fill_between(labels, cum, alpha=.12, color=cc)
    ax2.axhline(0, color=GR, lw=.8)
    ax2.set_ylabel("Cumulative", color="#90a4ae", fontsize=8)
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    fig.text(.99, .01, f"SMC SNIPER v5.4 · {ts}", color="#444d56", fontsize=7, ha="right")
    path = "/tmp/sniper_history.png"
    plt.tight_layout()
    plt.savefig(path, dpi=120, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return path

# ==================================================
# 14. SMC SNIPER BRAIN (All signal functions restored)
# ==================================================
def _bos_choch(df: pd.DataFrame) -> Optional[dict]:
    H, L = _swing_pts(df)
    if len(H) < 2 or len(L) < 2:
        return None
    lsh, psh = H[-1], H[-2]
    lsl, psl = L[-1], L[-2]
    lc = df["close"].iloc[-1]
    if lc > df["high"].iloc[lsh] and lsh > psh:
        return {"type": "BOS", "direction": "BULLISH", "level": df["high"].iloc[lsh]}
    if lc < df["low"].iloc[lsl] and lsl > psl:
        return {"type": "BOS", "direction": "BEARISH", "level": df["low"].iloc[lsl]}
    if df["high"].iloc[lsh] < df["high"].iloc[psh] and lc > df["high"].iloc[lsh]:
        return {"type": "CHoCH", "direction": "BULLISH", "level": df["high"].iloc[lsh]}
    if df["low"].iloc[lsl] > df["low"].iloc[psl] and lc < df["low"].iloc[lsl]:
        return {"type": "CHoCH", "direction": "BEARISH", "level": df["low"].iloc[lsl]}
    return None

def _pd_zone(df: pd.DataFrame) -> tuple:
    hi, lo = df["high"].max(), df["low"].min()
    r = hi - lo
    if r == 0:
        return "NEUTRAL", hi, lo
    fib_hi, fib_lo = hi - r * 0.382, lo + r * 0.382
    c = df["close"].iloc[-1]
    if c <= fib_lo: return "DISCOUNT", fib_hi, fib_lo
    if c >= fib_hi: return "PREMIUM", fib_hi, fib_lo
    return "EQUILIBRIUM", fib_hi, fib_lo

def _idm(df: pd.DataFrame, direction: str) -> Optional[dict]:
    H, L = _swing_pts(df, n=3)
    if direction == "BULLISH" and len(L) >= 2:
        lvl = df["low"].iloc[L[-2]]
        last = df.iloc[-1]
        return {"side": "BUY", "level": lvl, "swept": last["low"] < lvl and last["close"] > lvl}
    if direction == "BEARISH" and len(H) >= 2:
        lvl = df["high"].iloc[H[-2]]
        last = df.iloc[-1]
        return {"side": "SELL", "level": lvl, "swept": last["high"] > lvl and last["close"] < lvl}
    return None

def _equal_hl(df: pd.DataFrame) -> Optional[dict]:
    tol = 0.0003 if state.pair_key in ("EURUSD", "GBPUSD") else 0.0005
    r = df.iloc[-25:]
    hs, ls = r["high"].values, r["low"].values
    for i in range(len(hs)-1, 1, -1):
        for j in range(i-1, max(i-8,0), -1):
            if abs(hs[i]-hs[j])/hs[j] < tol:
                lvl = (hs[i]+hs[j])/2
                last = df.iloc[-1]
                if last["high"] > lvl and last["close"] < lvl:
                    return {"side": "SELL", "level": lvl, "swept": True, "type": "EQL_HIGHS"}
            if abs(ls[i]-ls[j])/ls[j] < tol:
                lvl = (ls[i]+ls[j])/2
                last = df.iloc[-1]
                if last["low"] < lvl and last["close"] > lvl:
                    return {"side": "BUY", "level": lvl, "swept": True, "type": "EQL_LOWS"}
    return None

def _ob(df: pd.DataFrame, direction: str) -> Optional[dict]:
    lk = min(30, len(df)-3)
    r = df.iloc[-lk:].reset_index(drop=True)
    ab = (r["close"]-r["open"]).abs().mean()
    if direction == "BULLISH":
        for i in range(len(r)-3,1,-1):
            c, nc = r.iloc[i], r.iloc[i+1]
            if c["close"] < c["open"] and nc["close"] > c["high"] and abs(nc["close"]-nc["open"]) > ab*1.5:
                return {"type": "BULL", "high": c["high"], "low": c["low"],
                        "body_hi": max(c["open"],c["close"]),
                        "body_lo": min(c["open"],c["close"]),
                        "displacement": round(abs(nc["close"]-nc["open"])/ab,2)}
    elif direction == "BEARISH":
        for i in range(len(r)-3,1,-1):
            c, nc = r.iloc[i], r.iloc[i+1]
            if c["close"] > c["open"] and nc["close"] < c["low"] and abs(nc["close"]-nc["open"]) > ab*1.5:
                return {"type": "BEAR", "high": c["high"], "low": c["low"],
                        "body_hi": max(c["open"],c["close"]),
                        "body_lo": min(c["open"],c["close"]),
                        "displacement": round(abs(nc["close"]-nc["open"])/ab,2)}
    return None

def _fvg(df: pd.DataFrame, ob: dict) -> Optional[dict]:
    if ob is None:
        return None
    thr = 0.03 if state.pair_key in ("EURUSD","GBPUSD") else 0.05
    r = df.iloc[-min(25,len(df)-3):].reset_index(drop=True)
    if ob["type"] == "BULL":
        for i in range(len(r)-3,0,-1):
            c1, c3 = r.iloc[i], r.iloc[i+2]
            gp = (c3["low"]-c1["high"])/c1["high"]*100
            if c1["high"] < c3["low"] and gp >= thr:
                return {"type":"BULL","high":c3["low"],"low":c1["high"],"gap_pct":gp}
    elif ob["type"] == "BEAR":
        for i in range(len(r)-3,0,-1):
            c1, c3 = r.iloc[i], r.iloc[i+2]
            gp = (c1["low"]-c3["high"])/c1["low"]*100
            if c1["low"] > c3["high"] and gp >= thr:
                return {"type":"BEAR","high":c1["low"],"low":c3["high"],"gap_pct":gp}
    return None

def _atr(df: pd.DataFrame, n: int = 14) -> float:
    if len(df) < n+1:
        return 0.
    tr = np.maximum(df["high"]-df["low"],
                    np.maximum(abs(df["high"]-df["close"].shift(1)),
                               abs(df["low"]-df["close"].shift(1))))
    return float(tr.iloc[-n:].mean())

def _get_trend(tf_name: str) -> str:
    buf = state.h1_candles if tf_name == "H1" else state.m15_candles
    if len(buf) < 30:
        return "NEUTRAL"
    df = pd.DataFrame(list(buf))
    df.columns = ["time","open","high","low","close"]
    r = _bos_choch(df)
    if r:
        state.trend_bias = r["direction"]
    else:
        c = df["close"].values[-20:]
        state.trend_bias = "BULLISH" if np.polyfit(np.arange(len(c)),c,1)[0] > 0 else "BEARISH"
    return state.trend_bias

def sniper_score(ob, fvg, trap, idm, rsi, session, atr_ok, ema_ok, candle_ok, struct_type, disp) -> tuple:
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

def compute_signal(tf: str = "M15") -> Optional[dict]:
    if tf == "M15": buf = state.m15_candles
    elif tf == "M5": buf = state.m5_candles
    elif tf == "M1": buf = state.m1_candles
    else: return None
    if len(buf) < 40:
        return None

    df = pd.DataFrame(list(buf))
    df.columns = ["time","open","high","low","close"]

    bias = _get_trend(state.trend_tf)
    if bias == "NEUTRAL":
        return None

    struct = _bos_choch(df)
    if struct is None or struct["direction"] != bias:
        return None

    pd_zone, fib_hi, fib_lo = _pd_zone(df)
    state.premium_discount = pd_zone
    if (bias == "BULLISH" and pd_zone != "DISCOUNT") or (bias == "BEARISH" and pd_zone != "PREMIUM"):
        return None

    idm = _idm(df, bias)
    state.active_idm = idm
    if idm is None:
        return None

    trap = _equal_hl(df)
    state.active_trap = trap
    if trap is not None and trap["side"] != ("BUY" if bias == "BULLISH" else "SELL"):
        trap = None
        state.active_trap = None

    ob_ = _ob(df, bias)
    if ob_ is None:
        return None
    state.active_ob = ob_

    fvg_ = _fvg(df, ob_)
    state.active_fvg = fvg_
    rsi_now = float(_rsi_calc(df["close"].values)[-1])

    session = get_session()
    state.session_now = session
    if state.pair_key == "XAUUSD" and session not in ("LONDON","NY","OVERLAP"):
        return None

    atr_v = _atr(df)
    thresholds = {"XAUUSD":0.5, "EURUSD":0.0005, "GBPUSD":0.0006, "US100":5.}
    atr_ok = atr_v >= thresholds.get(state.pair_key, .0001)
    state.atr_filter_ok = atr_ok
    if not atr_ok:
        return None

    ema50 = df["close"].ewm(span=50, adjust=False).mean().iloc[-1]
    price = df["close"].iloc[-1]
    ema_ok = (price > ema50 if bias == "BULLISH" else price < ema50)

    last = df.iloc[-1]
    candle_ok = (last["close"] > last["open"] if bias == "BULLISH" else last["close"] < last["open"])

    disp = ob_.get("displacement", 1.)
    sc, score_reasons = sniper_score(ob_, fvg_, trap, idm, rsi_now, session,
                                     atr_ok, ema_ok, candle_ok, struct["type"], disp)
    state.ob_score = sc
    # Log score breakdown for debugging
    log.info(f"💡 Score details: {score_reasons} (total {sc})")
    if sc < state.min_score:
        return None

    # TradingView confirmation (optional)
    tv_confirmed = False
    tv_message = ""
    if TV_AVAILABLE and state.tv_symbol:
        tv_confirmed, tv_message = check_tradingview("BUY" if bias == "BULLISH" else "SELL")
        if not tv_confirmed:
            log.info(f"TradingView rejection: {tv_message}")
            return None

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
    if tv_confirmed:
        reason.tv_confirm = tv_message

    if bias == "BULLISH":
        entry = ob_["body_hi"]
        sl = ob_["low"] * 0.9995
    else:
        entry = ob_["body_lo"]
        sl = ob_["high"] * 1.0005

    risk = abs(entry - sl)
    if risk == 0:
        return None
    mult = 1 if bias == "BULLISH" else -1
    tp1 = entry + risk * state.tp1_r * mult
    tp2 = entry + risk * state.tp2_r * mult
    tp3 = entry + risk * state.tp3_r * mult
    stake = max(1., round(max(PAIR_REGISTRY[state.pair_key][3],
                              state.account_balance * state.risk_pct), 2))

    sig = {
        "direction": "BUY" if bias == "BULLISH" else "SELL",
        "entry": round(entry,5), "sl": round(sl,5),
        "tp1": round(tp1,5), "tp2": round(tp2,5), "tp3": round(tp3,5),
        "risk_r": round(risk,5), "stake": stake,
        "struct": struct["type"], "ob": ob_, "fvg": fvg_,
        "trap": trap, "idm": idm, "ob_score": sc, "rsi": rsi_now,
        "pd_zone": pd_zone, "fib_hi": fib_hi, "fib_lo": fib_lo,
        "session": session, "tf": tf, "bias": bias,
        "reason": reason, "score_reasons": score_reasons,
        "tv_confirmed": tv_confirmed,
        "ts": datetime.now(UTC).isoformat(),
    }
    state.last_signal = sig
    return sig

def check_trade_mgmt():
    for cid, info in list(state.open_contracts.items()):
        sig = info.get("signal")
        if not sig:
            continue
        p = state.current_price
        d = info["direction"]
        if not info["be_moved"]:
            if (d == "BUY" and p >= sig["tp1"]) or (d == "SELL" and p <= sig["tp1"]):
                info["be_moved"] = True
                sig["sl"] = sig["entry"]
        buf = state.m15_candles if state.exec_tf == "M15" else state.m5_candles
        if len(buf) >= 10:
            df = pd.DataFrame(list(buf)[-30:])
            df.columns = ["time","open","high","low","close"]
            swh, swl = _swing_pts(df, n=3)
            if d == "BUY" and swl:
                t = df["low"].iloc[swl[-1]] * 0.9998
                if t > sig["sl"]:
                    sig["sl"] = t
            elif d == "SELL" and swh:
                t = df["high"].iloc[swh[-1]] * 1.0002
                if t < sig["sl"]:
                    sig["sl"] = t

# ==================================================
# 15. SUPABASE LOGGING (optional)
# ==================================================
def supabase_log_signal(signal: dict):
    if not supabase:
        return
    try:
        data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "pair": state.pair_key,
            "direction": signal["direction"],
            "score": signal["ob_score"],
            "entry": signal["entry"],
            "sl": signal["sl"],
            "tp1": signal["tp1"],
            "tp2": signal["tp2"],
            "tp3": signal["tp3"],
            "session": signal["session"],
            "pd_zone": signal["pd_zone"],
            "structure": signal["struct"],
            "tv_confirmed": signal.get("tv_confirmed", False),
            "executed": False
        }
        supabase.table("sniper_signals").insert(data).execute()
        log.info("📊 Signal logged to Supabase")
    except Exception as e:
        log.warning(f"Supabase log failed: {e}")

def supabase_log_trade(contract_id: str, signal: dict, result: dict):
    if not supabase:
        return
    try:
        data = {
            "contract_id": contract_id,
            "pair": state.pair_key,
            "direction": signal["direction"],
            "entry": signal["entry"],
            "exit": result.get("exit", 0),
            "pnl": result.get("pnl", 0),
            "score": signal["ob_score"],
            "timestamp": datetime.now(UTC).isoformat(),
            "is_win": result.get("pnl", 0) > 0
        }
        supabase.table("sniper_trades").insert(data).execute()
        log.info("💰 Trade result logged to Supabase")
    except Exception as e:
        log.warning(f"Supabase log failed: {e}")

# ==================================================
# 16. DERIV WEBSOCKET & TRADE EXECUTION
# ==================================================
async def send_req(payload: dict) -> dict:
    if state.ws is None:
        raise RuntimeError("WS not connected")
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
        raise asyncio.TimeoutError(f"Timeout {list(payload.keys())}")

async def authorize(token: str = None) -> dict:
    t = token or state.deriv_token
    if not t:
        raise RuntimeError("No token")
    resp = await send_req({"authorize": t})
    if "error" in resp:
        raise RuntimeError(resp["error"]["message"])
    auth = resp["authorize"]
    state.broker_connected = True
    state.account_id = auth.get("loginid", "")
    state.account_type = auth.get("account_type", "demo")
    if token:
        state.deriv_token = token
        try:
            TOKEN_FILE.write_text(token)
        except Exception:
            pass
    log.info(f"Authorized: {state.account_id} ({state.account_type})")
    return auth

async def get_balance():
    r = await send_req({"balance": 1, "subscribe": 0})
    if "balance" in r:
        state.account_balance = r["balance"]["balance"]
        state.account_currency = r["balance"]["currency"]

def _update_or_append_candle(deque_obj: deque, new_candle: tuple, nom: int):
    epoch = new_candle[0]
    if deque_obj and deque_obj[-1][0] == epoch:
        deque_obj[-1] = new_candle
    else:
        deque_obj.append(new_candle)

def _store(nom: int, rows: list):
    if nom == 3600:
        for row in rows:
            _update_or_append_candle(state.h1_candles, row, nom)
    elif nom == 900:
        for row in rows:
            _update_or_append_candle(state.m15_candles, row, nom)
        if rows: state.current_price = float(rows[-1][4])
    elif nom == 300:
        for row in rows:
            _update_or_append_candle(state.m5_candles, row, nom)
        if rows: state.current_price = float(rows[-1][4])
    elif nom == 60:
        for row in rows:
            _update_or_append_candle(state.m1_candles, row, nom)
        if rows: state.current_price = float(rows[-1][4])

async def _fetch(sym: str, nom: int) -> int:
    lbl = {3600:"H1",900:"M15",300:"M5",60:"M1"}
    for ag in GRAN_FALLBACKS.get(nom, [nom]):
        try:
            r = await send_req({
                "ticks_history": sym, "end": "latest",
                "count": 200, "granularity": ag, "style": "candles"})
            if "error" in r:
                log.warning(f"Fetch {sym} g={ag}: {r['error'].get('message','')}")
                continue
            raw = r.get("candles", [])
            if not raw:
                continue
            rows = [(int(c["epoch"]), float(c["open"]), float(c["high"]),
                     float(c["low"]), float(c["close"])) for c in raw]
            state.gran_actual[nom] = ag
            _store(nom, rows)
            log.info(f"✅ {len(rows)} {lbl.get(nom,'?')} (g={ag}) {sym}")
            asyncio.ensure_future(send_req({
                "ticks_history": sym, "end": "latest",
                "count": 1, "granularity": ag,
                "style": "candles", "subscribe": 1}))
            return len(rows)
        except asyncio.TimeoutError:
            log.warning(f"Timeout g={ag}")
        except Exception as e:
            log.error(f"_fetch {sym} g={ag}: {e}")
    return 0

async def _resolve_sym(key: str) -> str:
    pri, otc = PAIR_REGISTRY[key][0], PAIR_REGISTRY[key][1]
    for sym in (pri, otc):
        try:
            r = await send_req({
                "ticks_history": sym, "end": "latest",
                "count": 1, "granularity": 3600, "style": "candles"})
            if "candles" in r:
                log.info(f"Symbol OK: {sym}")
                return sym
            log.warning(f"Symbol {sym}: {r.get('error',{}).get('message','?')}")
        except Exception as e:
            log.warning(f"Symbol test {sym}: {e}")
    return pri

async def subscribe_pair(key: str):
    state.h1_candles.clear()
    state.m15_candles.clear()
    state.m5_candles.clear()
    state.m1_candles.clear()
    state.last_signal = None
    state.active_ob = None
    state.active_fvg = None
    state.active_trap = None
    state.active_idm = None
    state.current_price = 0.
    state.trend_bias = "NEUTRAL"
    state.ob_score = 0
    state.premium_discount = "NEUTRAL"

    sym = await _resolve_sym(key)
    state.active_symbol = sym
    state.subscribed_sym = sym
    h = await _fetch(sym, 3600)
    m = await _fetch(sym, 900)
    f = await _fetch(sym, 300)
    o = await _fetch(sym, 60)
    log.info(f"subscribe_pair done: {sym} H1:{h} M15:{m} M5:{f} M1:{o}")
    return h, m, f, o

async def execute_trade(signal: dict) -> Optional[str]:
    if not state.broker_connected:
        log.error("❌ Cannot execute trade: Broker not connected")
        return None
    if state.paused:
        log.error("❌ Cannot execute trade: Bot is paused")
        return None
    if state.block_trading:
        log.error(f"❌ Cannot execute trade: Trading blocked - {state.block_reason}")
        return None

    direction = signal["direction"]
    amount = signal["stake"]
    contract_type = "MULTUP" if direction == "BUY" else "MULTDOWN"

    contract_params = {
        "buy": 1,
        "price": round(amount, 2),
        "parameters": {
            "contract_type": contract_type,
            "symbol": state.active_symbol or PAIR_REGISTRY[state.pair_key][0],
            "amount": round(amount, 2),
            "currency": state.account_currency,
            "multiplier": 10,
            "basis": "stake",
            "stop_out": 1
        }
    }

    log.info(f"🚀 EXECUTING TRADE: {direction} {amount:.2f} {state.account_currency}")
    try:
        response = await send_req(contract_params)
        if "error" in response:
            log.error(f"❌ Trade execution FAILED: {response['error'].get('message','Unknown')}")
            return None
        if "buy" not in response:
            log.error(f"❌ Unexpected response: {response}")
            return None
        contract_id = str(response["buy"]["contract_id"])
        log.info(f"✅ Trade EXECUTED! Contract ID: {contract_id}")
        state.open_contracts[contract_id] = {
            "direction": direction,
            "entry": state.current_price,
            "amount": amount,
            "signal": signal,
            "be_moved": False,
            "opened_at": time.time()
        }
        state.trade_count += 1
        return contract_id
    except Exception as e:
        log.error(f"❌ Trade execution EXCEPTION: {e}")
        return None

async def close_contract(cid: str) -> bool:
    try:
        r = await send_req({"sell": cid, "price": 0})
        if "error" in r:
            log.error(f"Close: {r['error']['message']}")
            return False
        state.open_contracts.pop(cid, None)
        log.info(f"🔴 Closed {cid}")
        return True
    except Exception as e:
        log.error(f"close_contract: {e}")
        return False

async def close_all() -> int:
    ids = list(state.open_contracts.keys())
    for cid in ids:
        await close_contract(cid)
    return len(ids)

# ==================================================
# 17. WEBSOCKET MESSAGE HANDLER (with duplicate candle fix)
# ==================================================
def _update_buf(actual_gran: int, rows: list):
    rev = {v:k for k,v in state.gran_actual.items()}
    nom = rev.get(actual_gran, actual_gran)
    _store(nom, rows)

async def handle_msg(msg: dict):
    rid = msg.get("req_id")
    if rid and rid in state.pending_reqs:
        fut = state.pending_reqs.pop(rid)
        if not fut.done():
            fut.set_result(msg)
        return

    mt = msg.get("msg_type", "")
    if mt == "ohlc":
        c = msg["ohlc"]
        epoch = int(c["epoch"])
        open_ = float(c["open"])
        high = float(c["high"])
        low = float(c["low"])
        close = float(c["close"])
        gran = int(c.get("granularity", 0))

        # Update or append based on epoch
        if gran == 3600:
            _update_or_append_candle(state.h1_candles, (epoch, open_, high, low, close), 3600)
        elif gran == 900:
            _update_or_append_candle(state.m15_candles, (epoch, open_, high, low, close), 900)
            state.current_price = close
        elif gran == 300:
            _update_or_append_candle(state.m5_candles, (epoch, open_, high, low, close), 300)
            state.current_price = close
        elif gran == 60:
            _update_or_append_candle(state.m1_candles, (epoch, open_, high, low, close), 60)
            state.current_price = close
    elif mt == "candles":
        gran = int(msg.get("echo_req",{}).get("granularity",0))
        rows = [(int(c["epoch"]), float(c["open"]), float(c["high"]),
                 float(c["low"]), float(c["close"])) for c in msg.get("candles",[])]
        if rows:
            _update_buf(gran, rows)
    elif mt == "tick":
        state.current_price = float(msg["tick"]["quote"])
    elif mt == "proposal_open_contract":
        poc = msg.get("proposal_open_contract",{})
        cid = str(poc.get("contract_id",""))
        if cid not in state.open_contracts:
            return
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
            state.trade_history.append({
                "num": tnum,
                "id": cid,
                "pair": state.pair_display,
                "direction": info.get("direction","?"),
                "entry": info.get("entry",0.),
                "exit": exit_s,
                "pnl": round(profit,2),
                "win": profit>0,
                "score": sig.get("ob_score",0),
                "session": sig.get("session","?"),
                "ts": datetime.now(UTC).strftime("%m/%d %H:%M")
            })
            if len(state.trade_history) > 50:
                state.trade_history.pop(0)

            # Log to Supabase if enabled
            supabase_log_trade(cid, sig, {"exit": exit_s, "pnl": profit})

            buf_for_chart = state.m15_candles if state.exec_tf=="M15" else state.m5_candles
            chart = generate_chart(buf_for_chart, state.exec_tf,
                                   entry_price=info.get("entry"), exit_price=exit_s,
                                   direction=info.get("direction"), pnl=profit,
                                   chart_type="exit")
            sign = "+" if profit>0 else ""
            wr = f"{state.wins/(state.wins+state.losses)*100:.1f}%" if (state.wins+state.losses)>0 else "N/A"
            reason_obj = sig.get("reason")
            post_report = reason_obj.build_report(info.get("direction","?")) if reason_obj else ""
            amharic_r = reason_obj.build_amharic(info.get("direction","?")) if reason_obj else ""
            await tg_async(
                f"{'✅ WIN' if profit>0 else '❌ LOSS'} `#{tnum}` — *Post-Trade Report*\n\n"
                f"Pair:`{state.pair_display}` Dir:`{info.get('direction','?')}`\n"
                f"Entry:`{info.get('entry',0):.5f}` Exit:`{exit_s:.5f}`\n"
                f"P&L: `{sign}{profit:.2f} {state.account_currency}`\n"
                f"Score:`{sig.get('ob_score',0)}/100` Session:`{sig.get('session','?')}`\n"
                f"W/L:{state.wins}/{state.losses} WR:{wr} "
                f"Total:{state.total_pnl:+.2f} Bal:{state.account_balance:.2f}\n\n"
                f"{post_report}",
                photo_path=chart)
            if amharic_r:
                await tg_async(amharic_r)
    elif mt == "balance":
        state.account_balance = msg["balance"]["balance"]
        state.account_currency = msg["balance"]["currency"]
    elif "error" in msg:
        log.warning(f"API: {msg['error'].get('message','?')}")

# ==================================================
# 18. BACKGROUND LOOPS
# ==================================================
async def chart_loop():
    await asyncio.sleep(50)
    while state.running:
        try:
            if state.current_price>0 and len(state.m15_candles)>=20:
                sig = compute_signal(state.exec_tf)
                sess = get_session()
                state.session_now = sess
                log.info(
                    f"📡 Scan | {state.pair_display} | {state.trading_mode} | "
                    f"Bias:{state.trend_bias} | Zone:{state.premium_discount} | "
                    f"Score:{state.ob_score}/100 | Sess:{sess} | "
                    f"Price:{state.current_price:.5f} | "
                    f"Signal:{'✅ '+sig['direction'] if sig else '—'}"
                )
        except Exception as e:
            log.error(f"chart_loop: {e}")
        await asyncio.sleep(CHART_INTERVAL)

async def trading_loop():
    await asyncio.sleep(35)
    while state.running:
        try:
            if state.paused:
                await asyncio.sleep(30); continue
            if state.block_trading:
                log.info(f"⏸ Trading blocked: {state.block_reason}")
                await asyncio.sleep(30); continue
            if state.current_price==0:
                log.info("⏳ Waiting for price data...")
                await asyncio.sleep(30); continue
            if not state.broker_connected:
                log.info("⏳ Broker not connected")
                await asyncio.sleep(30); continue
            if not is_market_open():
                state.market_open = False
                log.info("🔴 Market closed — sleeping")
                await asyncio.sleep(60); continue
            state.market_open = True

            check_trade_mgmt()

            cooldown_left = state.signal_cooldown - (time.time() - state.last_trade_ts)
            if cooldown_left > 0:
                log.info(f"⏳ Cooldown: {int(cooldown_left)}s remaining")
                await asyncio.sleep(30); continue

            log.info(
                f"🔍 Scanning {state.pair_display} [{state.trading_mode}] "
                f"H1:{len(state.h1_candles)} M15:{len(state.m15_candles)} "
                f"M5:{len(state.m5_candles)} M1:{len(state.m1_candles)} "
                f"Bias:{state.trend_bias} Zone:{state.premium_discount} "
                f"Score:{state.ob_score}/100")

            sig = compute_signal(state.exec_tf)
            if sig is None:
                log.info(
                    f"🔎 No signal — Bias:{state.trend_bias} "
                    f"Zone:{state.premium_discount} Score:{state.ob_score}/100 "
                    f"Sess:{state.session_now} ATR_ok:{state.atr_filter_ok}")
            else:
                log.info(f"🎯 SIGNAL DETECTED! Score: {sig['ob_score']}/100, Direction: {sig['direction']}")
                if sig['ob_score'] >= state.min_score:
                    log.info(f"⭐ SCORE ({sig['ob_score']} >= {state.min_score}) — PROCEEDING")
                    if state.trading_mode == "SNIPER" and state.top_down:
                        sig_conf = compute_signal(state.conf_tf)
                        if sig_conf and sig_conf["direction"] == sig["direction"]:
                            log.info(f"✅ Confirmation TF ({state.conf_tf}) confirms {sig['direction']}")
                        else:
                            log.info(f"⚠️ Confirmation TF ({state.conf_tf}) mismatch — skipping")
                            continue
                    buf_for_chart = state.m15_candles if state.exec_tf=="M15" else state.m5_candles
                    chart = generate_chart(buf_for_chart, state.exec_tf,
                                           entry_price=sig["entry"],
                                           direction=sig["direction"],
                                           chart_type="entry",
                                           reason=sig.get("reason"))
                    score_txt = " + ".join(sig.get("score_reasons", [])[:5])
                    md_lbl = "🎯 Sniper" if state.trading_mode=="SNIPER" else "⚡ Scalper"
                    tv_note = " 📊 TV Confirmed" if sig.get("tv_confirmed") else ""
                    await tg_async(
                        f"🚀 *{md_lbl} ENTRY — {state.pair_display}{tv_note}*\n"
                        f"Score: `{sig['ob_score']}/100` ✅ Autonomous\n\n"
                        f"Dir: `{sig['direction']}`\n"
                        f"Entry: `{sig['entry']}`\n"
                        f"SL: `{sig['sl']}`\n"
                        f"TP1({state.tp1_r}R): `{sig['tp1']}`\n"
                        f"TP2({state.tp2_r}R): `{sig['tp2']}`\n"
                        f"TP3({state.tp3_r}R): `{sig['tp3']}`\n\n"
                        f"*Score Breakdown:*\n`{score_txt}`\n\n"
                        f"Stake: `{sig['stake']:.2f} {state.account_currency}`"
                        f"{' 💎' if state.small_acc_mode else ''}",
                        photo_path=chart)
                    # Log to Supabase if enabled
                    supabase_log_signal(sig)
                    contract_id = await execute_trade(sig)
                    if contract_id:
                        state.last_trade_ts = time.time()
                        log.info(f"✅ TRADE EXECUTED! Contract ID: {contract_id}")
                    else:
                        log.error(f"❌ TRADE EXECUTION FAILED for signal with score {sig['ob_score']}")
                else:
                    log.info(f"📊 Score {sig['ob_score']} < {state.min_score} — not executing")
        except Exception as e:
            log.error(f"trading_loop: {e}\n{traceback.format_exc()}")
        await asyncio.sleep(30)

# ==================================================
# 19. TELEGRAM POLLING
# ==================================================
async def tg_poll_loop():
    if not TELEGRAM_TOKEN:
        log.warning("No TELEGRAM_TOKEN")
        return
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, lambda: requests.post(f"{base}/deleteWebhook", json={"drop_pending_updates":True}, timeout=10))
        log.info("Webhook cleared.")
    except Exception as e:
        log.warning(f"Webhook: {e}")
    await asyncio.sleep(2)
    offset, ec = 0, 0
    while state.running:
        try:
            r = await loop.run_in_executor(None, lambda: requests.get(f"{base}/getUpdates",
                params={"offset": offset, "timeout":20, "allowed_updates":["message","callback_query"]}, timeout=25))
            data = r.json()
            if not data.get("ok"):
                desc = data.get("description","")
                log.error(f"TG: {desc}")
                if "Conflict" in desc:
                    await asyncio.sleep(30)
                    await loop.run_in_executor(None, lambda: requests.post(f"{base}/deleteWebhook", json={"drop_pending_updates":True}, timeout=10))
                else:
                    await asyncio.sleep(10)
                ec += 1
                continue
            ec = 0
            for upd in data.get("result",[]):
                offset = upd["update_id"]+1
                await _handle_upd(upd)
        except Exception as e:
            ec += 1
            log.error(f"TG poll: {type(e).__name__}: {e}")
            await asyncio.sleep(min(5*ec,60))
        await asyncio.sleep(.5)

async def _handle_upd(upd: dict):
    if "message" in upd:
        text = upd["message"].get("text","").strip()
        cid = str(upd["message"]["chat"]["id"])
        if TELEGRAM_CHAT_ID and cid != TELEGRAM_CHAT_ID:
            return
        if state.awaiting_token and text and not text.startswith("/"):
            await _process_token(text)
            return
        if state.awaiting_custom_score:
            try:
                score = int(text.strip())
                if 10 <= score <= 100:
                    state.min_score = score
                    state.awaiting_custom_score = False
                    await tg_async(f"✅ Minimum score set to {score}", reply_markup=kb_settings())
                else:
                    await tg_async("❌ Please enter a number between 10 and 100.", reply_markup=kb_score())
            except ValueError:
                await tg_async("❌ Invalid number. Please enter a number between 10 and 100.", reply_markup=kb_score())
            return
        await _cmd(text)
    elif "callback_query" in upd:
        cq = upd["callback_query"]
        data = cq.get("data","")
        cqid = cq["id"]
        cid = str(cq["message"]["chat"]["id"])
        if TELEGRAM_CHAT_ID and cid != TELEGRAM_CHAT_ID:
            tg_answer(cqid, "Unauthorized")
            return
        tg_answer(cqid)
        await _cmd(data)

async def _process_token(token: str):
    state.awaiting_token = False
    if not state.ws:
        await tg_async("⚠️ Bot not connected yet. Please wait and try again.", reply_markup=kb_main())
        return
    await tg_async("🔄 Authenticating with Deriv...", reply_markup=kb_main())
    try:
        auth = await authorize(token)
        await get_balance()
        acct_icon = "🔴 REAL" if state.account_type=="real" else "🟢 DEMO"
        await tg_async(
            f"✅ *Broker Connected Successfully!*\n\n"
            f"Account: `{state.account_id}`\n"
            f"Type: {acct_icon}\n"
            f"Balance: `{state.account_balance:.2f} {state.account_currency}`\n\n"
            f"_Sniper Brain is now active. Scanning for high-quality setups..._",
            reply_markup=kb_main())
        h,m,f,o = await subscribe_pair(state.pair_key)
        log.info(f"Re-subscribed after token: H1:{h} M15:{m} M5:{f} M1:{o}")
    except Exception as e:
        await tg_async(f"❌ Authentication failed: `{e}`\n\nPlease check your token and try again.", reply_markup=kb_connect())

async def _cmd(cmd: str):
    cmd = cmd.lower().strip()
    if cmd in ("/start","/help","cmd_back"):
        mkt = market_header()
        bl = ("🚫 "+state.block_reason if state.block_trading else ("🛑 PAUSED" if state.paused else "🟢 AUTONOMOUS"))
        conn = "✅ Connected" if state.broker_connected else "❌ Not connected — tap 🔗 Connect Broker"
        md_lbl = "🎯 Sniper" if state.trading_mode=="SNIPER" else "⚡ Scalper"
        await tg_async(
            f"{mkt}\n\n🤖 *SMC SNIPER EA v5.4*\n\n"
            f"Status: {bl}\nBroker: {conn}\nStrategy: `{md_lbl}`\n"
            f"Acct: `{state.account_id}` ({state.account_type.upper()})\n"
            f"Bal: `{state.account_balance:.2f} {state.account_currency}`\n"
            f"Pair: `{state.pair_display}` Risk:`{state.risk_pct*100:.0f}%`\n"
            f"Min Score: `{state.min_score}/100`\n"
            f"H1:`{len(state.h1_candles)}` M15:`{len(state.m15_candles)}` "
            f"M5:`{len(state.m5_candles)}` M1:`{len(state.m1_candles)}`",
            reply_markup=kb_main())
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
        await tg_async(
            f"{mkt}\n\n⚡ *Sniper Status*\nMode: {bl}\nStrategy: `{md_lbl}`\n"
            f"Pair: `{state.pair_display}` Price:`{state.current_price:.5f}`\n"
            f"Bias:`{state.trend_bias}` Zone:`{state.premium_discount}`\n"
            f"Session:`{state.session_now}` ATR:`{'OK' if state.atr_filter_ok else 'LOW'}`\n"
            f"Open Trades:`{len(state.open_contracts)}`\n"
            f"Acct:`{state.account_type.upper()}` Bal:`{state.account_balance:.2f}`\n"
            f"W/L:`{state.wins}/{state.losses}` P&L:`{state.total_pnl:+.2f}`{nxt_s}{sig_s}",
            reply_markup=kb_main())
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
        ni = ""
        if nxt and nxt.dt_utc:
            ny_t = nxt.dt_utc.astimezone(NY_TZ).strftime("%I:%M%p ET")
            mins = int((nxt.dt_utc-datetime.now(UTC)).total_seconds()//60)
            ni = f"\n\n🔴 Next Red: *{nxt.title}* @ `{ny_t}` (~{mins}m away)"
        bl_s, bl_r = _news_block()
        bi = f"\n\n{bl_r}" if bl_s else "\n\n✅ No active news block."
        await tg_async(
            f"{market_header()}\n\n📰 *Economic Calendar — Today*\n\n{_amharic_summary()}{ni}{bi}",
            photo_path=state.news_chart_path, reply_markup=kb_main())
    elif cmd in ("/chart","cmd_chart"):
        buf_for_chart = state.m15_candles if state.exec_tf=="M15" else state.m5_candles
        chart_tf = state.exec_tf
        if len(buf_for_chart) >= 20:
            path = generate_chart(buf_for_chart, chart_tf, chart_type="live")
            if path:
                pe = ("🟢" if state.premium_discount=="DISCOUNT" else "🔴" if state.premium_discount=="PREMIUM" else "⚪")
                blk = " 🚫NEWS BLOCK" if state.block_trading else ""
                md_lbl = "🎯 Sniper" if state.trading_mode=="SNIPER" else "⚡ Scalper"
                await tg_async(
                    f"{market_header()}\n\n📊 *{state.pair_display} {chart_tf}* [{md_lbl}]{blk}\n"
                    f"Price:`{state.current_price:.5f}` Bias:`{state.trend_bias}`\n"
                    f"Zone:{pe}`{state.premium_discount}` Score:`{state.ob_score}/100`\n"
                    f"Session:`{state.session_now}` ATR:`{'OK' if state.atr_filter_ok else 'LOW'}`\n"
                    f"IDM:{'✅' if state.active_idm and state.active_idm.get('swept') else '⏳'} "
                    f"Trap:{'✅' if state.active_trap and state.active_trap.get('swept') else '⏳'}",
                    photo_path=path, reply_markup=kb_main())
                return
        await tg_async(
            f"{market_header()}\n\n⚠️ *Chart not ready*\n{chart_tf}:`{len(buf_for_chart)}` bars (needs 20+) sym:`{state.active_symbol}`\n"
            f"WS:`{'connected' if state.ws else 'disconnected'}`",
            reply_markup=kb_main())
    elif cmd in ("/history","cmd_history"):
        if not state.trade_history:
            await tg_async("📋 No trade history yet.", reply_markup=kb_main())
            return
        path = generate_history_chart()
        lines = ["📋 *Trade History — Last 10*\n"]
        for t in state.trade_history[-10:]:
            s = "+" if t["pnl"]>0 else ""
            lines.append(f"{'✅' if t['win'] else '❌'} `#{t['num']}` {t['direction']} `{t['entry']:.5f}`→`{t['exit']:.5f}` `{s}{t['pnl']:.2f}` sc:`{t.get('score',0)}` _{t['ts']}_")
        wr = f"{state.wins/(state.wins+state.losses)*100:.1f}%" if (state.wins+state.losses)>0 else "N/A"
        lines.append(f"\nP&L:`{state.total_pnl:+.2f}` WR:`{wr}`")
        await tg_async("\n".join(lines), photo_path=path, reply_markup=kb_main())
    elif cmd in ("/balance","cmd_balance"):
        if state.ws:
            try:
                await get_balance()
            except Exception:
                pass
        wr = f"{state.wins/(state.wins+state.losses)*100:.1f}%" if (state.wins+state.losses)>0 else "N/A"
        acct = "🔴 REAL" if state.account_type=="real" else "🟢 DEMO"
        await tg_async(
            f"💰 *Account Balance*\n`{state.account_balance:.2f} {state.account_currency}`\n"
            f"Type: {acct} ID: `{state.account_id}`\n\n"
            f"Trades:`{state.trade_count}` W/L:`{state.wins}/{state.losses}` WR:`{wr}`\n"
            f"Total P&L:`{state.total_pnl:+.2f}`",
            reply_markup=kb_main())
    elif cmd in ("/connect","cmd_connect"):
        state.awaiting_token = True
        await tg_async(
            "🔗 *Connect Broker — Deriv*\n\n"
            "Please send your *Deriv API Token* as the next message.\n\n"
            "The token must have:\n• ✅ Read scope\n• ✅ Trade scope\n\n"
            "Get it at:\n`app.deriv.com/account/api-token`\n\n"
            "_Your token is saved securely on the server._",
            reply_markup=kb_connect())
    elif cmd == "cmd_token_help":
        await tg_async(
            "📋 *How to get your Deriv API Token:*\n\n"
            "1. Open `app.deriv.com`\n2. Login → Account Settings\n3. API Token → Create new token\n"
            "4. Enable: *Read + Trade*\n5. Copy and paste the token here\n\n"
            "_For demo account: use your demo credentials_",
            reply_markup=kb_connect())
    elif cmd in ("/settings","cmd_settings"):
        md_lbl = "🎯 Sniper" if state.trading_mode=="SNIPER" else "⚡ Scalper"
        await tg_async(
            f"⚙️ *Settings*\nStrategy:`{md_lbl}`\nPair:`{state.pair_display}` sym:`{state.active_symbol}`\n"
            f"Risk:`{state.risk_pct*100:.0f}%` TPs:`{state.tp1_r}R/{state.tp2_r}R/{state.tp3_r}R`\n"
            f"Min Score:`{state.min_score}/100`\nSmall Acc:`{'ON 💎' if state.small_acc_mode else 'OFF'}`",
            reply_markup=kb_settings())
    elif cmd in ("/mode","cmd_mode_menu"):
        await tg_async("🎛 *Select Trading Strategy Mode:*", reply_markup=kb_mode())
    elif cmd == "cmd_mode_sniper":
        state.trading_mode = "SNIPER"
        state.min_score = 75
        state.trend_tf = "H1"
        state.exec_tf = "M15"
        state.conf_tf = "M5"
        await tg_async("✅ Strategy updated to *🎯 SMC Sniper*.\nTIMEFRAMES: [H1, M15, M5] | Min Score: 75", reply_markup=kb_settings())
    elif cmd == "cmd_mode_scalper":
        state.trading_mode = "SCALPER"
        state.min_score = 60
        state.trend_tf = "M15"
        state.exec_tf = "M5"
        state.conf_tf = "M1"
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
                    h,m,f,o = await subscribe_pair(key)
                    await tg_async(f"💱 Switched → `{state.pair_display}`\nH1:`{h}` M15:`{m}` M5:`{f}` M1:`{o}` ✅", reply_markup=kb_main())
                except Exception as e:
                    state.pair_key = old
                    await tg_async(f"❌ Switch failed: {e}", reply_markup=kb_main())
            else:
                await tg_async(f"💱 Pair → `{state.pair_display}` (next connect)", reply_markup=kb_main())
    elif cmd == "cmd_small_acc":
        if state.small_acc_mode:
            state.small_acc_mode = False
            state.risk_pct = 0.01
            state.tp1_r, state.tp2_r, state.tp3_r = 2.,4.,6.
            await tg_async("💎 Small Acc *OFF* — 1% risk", reply_markup=kb_settings())
        else:
            state.small_acc_mode = True
            if state.pair_key == "XAUUSD":
                state.risk_pct = 0.02
                state.tp1_r, state.tp2_r, state.tp3_r = 1.5,3.,5.
                note = "XAU/USD 2% risk (tight TPs)"
            else:
                state.pair_key = "GBPUSD"
                state.risk_pct = 0.05
                state.tp1_r, state.tp2_r, state.tp3_r = 1.5,3.,4.5
                note = "GBP/USD 5% risk"
            if state.ws:
                try:
                    await subscribe_pair(state.pair_key)
                except Exception:
                    pass
            await tg_async(f"💎 *Small Acc ON*\n{note}", reply_markup=kb_settings())
    elif cmd == "cmd_risk_1":
        state.risk_pct = 0.01; state.small_acc_mode = False
        await tg_async("✅ Risk → 1%", reply_markup=kb_settings())
    elif cmd == "cmd_risk_3":
        state.risk_pct = 0.03; state.small_acc_mode = False
        await tg_async("✅ Risk → 3%", reply_markup=kb_settings())
    elif cmd == "cmd_risk_5":
        state.risk_pct = 0.05; state.small_acc_mode = False
        await tg_async("✅ Risk → 5%", reply_markup=kb_settings())
    # ========== NEW SETTINGS HANDLERS ==========
    elif cmd == "cmd_score_menu":
        await tg_async("🎯 Select minimum score for signal execution:", reply_markup=kb_score())
    elif cmd.startswith("cmd_score_"):
        val = cmd.replace("cmd_score_", "")
        if val == "custom":
            state.awaiting_custom_score = True
            await tg_async("✏️ Enter a number between 10 and 100:", reply_markup=None)
        else:
            score = int(val)
            state.min_score = score
            await tg_async(f"✅ Minimum score set to {score}", reply_markup=kb_settings())
    elif cmd == "cmd_tf_menu":
        await tg_async("⏱️ Select which timeframe to change:", reply_markup=kb_tf())
    elif cmd == "cmd_tf_trend":
        await tg_async("📈 Select trend timeframe:", reply_markup=kb_tf_trend())
    elif cmd == "cmd_tf_exec":
        await tg_async("⚡ Select execution timeframe:", reply_markup=kb_tf_exec())
    elif cmd == "cmd_tf_conf":
        await tg_async("✅ Select confirmation timeframe:", reply_markup=kb_tf_conf())
    elif cmd.startswith("cmd_tf_trend_"):
        tf = cmd.replace("cmd_tf_trend_", "")
        state.trend_tf = tf
        await tg_async(f"✅ Trend TF set to {tf}", reply_markup=kb_tf())
    elif cmd.startswith("cmd_tf_exec_"):
        tf = cmd.replace("cmd_tf_exec_", "")
        state.exec_tf = tf
        await tg_async(f"✅ Execution TF set to {tf}", reply_markup=kb_tf())
    elif cmd.startswith("cmd_tf_conf_"):
        tf = cmd.replace("cmd_tf_conf_", "")
        state.conf_tf = tf
        await tg_async(f"✅ Confirmation TF set to {tf}", reply_markup=kb_tf())
    elif cmd == "cmd_top_down":
        state.top_down = not state.top_down
        status = "ON" if state.top_down else "OFF"
        await tg_async(f"🔄 Top‑down analysis turned {status}", reply_markup=kb_settings())
    # ==========================================
    elif cmd in ("/stop","cmd_stop"):
        state.paused = True
        n = await close_all()
        await tg_async(f"🛑 *Emergency Stop*\nClosed `{n}` contracts. Bot *PAUSED*.", reply_markup=kb_main())
    elif cmd == "cmd_toggle_pause":
        if state.paused or state.block_trading:
            state.paused = False
            state.block_trading = False
            state.block_reason = ""
            await tg_async("▶️ Bot *RESUMED* — Sniper Brain active.", reply_markup=kb_main())
        else:
            state.paused = True
            await tg_async("⏸ Bot *PAUSED* — press Resume to restart.", reply_markup=kb_main())

# ==================================================
# 20. WEBSOCKET ENGINE & HEALTH SERVER
# ==================================================
async def ws_reader(ws):
    async for raw in ws:
        try:
            await handle_msg(json.loads(raw))
        except Exception as e:
            log.error(f"Handler: {type(e).__name__}: {e}")

async def ws_run(ws):
    state.ws = ws
    async def setup():
        await asyncio.sleep(0.1)
        log.info("Authorizing...")
        await authorize()
        await get_balance()
        log.info(f"Bal:{state.account_balance} {state.account_currency} ({state.account_type})")
        h,m,f,o = await subscribe_pair(state.pair_key)
        acct_icon = "🔴 REAL" if state.account_type=="real" else "🟢 DEMO"
        md_lbl = "🎯 Sniper" if state.trading_mode=="SNIPER" else "⚡ Scalper"
        await tg_async(
            f"{market_header()}\n\n🤖 *SMC SNIPER EA v5.4 Online*\n"
            f"Broker: {acct_icon} `{state.account_id}`\nStrategy: `{md_lbl}`\n"
            f"Bal:`{state.account_balance:.2f} {state.account_currency}`\n"
            f"Pair:`{state.pair_display}` sym:`{state.active_symbol}`\n"
            f"Risk:`{state.risk_pct*100:.0f}%` MinScore:`{state.min_score}/100`\n"
            f"Bars H1:`{h}` M15:`{m}` M5:`{f}` M1:`{o}` ✅\n\n"
            f"_Fetching news... Sniper Brain armed._ 🎯",
            reply_markup=kb_main())
    task = asyncio.ensure_future(setup())
    try:
        await ws_reader(ws)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

async def ws_loop():
    delay = 5
    while state.running:
        try:
            log.info(f"Connecting: {DERIV_WS_BASE}")
            async with websockets.connect(DERIV_WS_BASE, ping_interval=25, ping_timeout=10, close_timeout=10, open_timeout=15) as ws:
                delay = 5
                await ws_run(ws)
        except websockets.ConnectionClosed as e:
            log.warning(f"WS closed: {e}")
        except asyncio.TimeoutError as e:
            log.error(f"WS timeout: {e}")
        except Exception as e:
            log.error(f"WS: {type(e).__name__}: {e}")
            log.error(traceback.format_exc())
        finally:
            state.ws = None
            state.broker_connected = False
            for fut in state.pending_reqs.values():
                if not fut.done():
                    fut.cancel()
            state.pending_reqs.clear()
        await asyncio.sleep(delay)
        delay = min(delay*2,60)

async def health(req):
    wr = f"{state.wins/(state.wins+state.losses)*100:.1f}" if (state.wins+state.losses)>0 else "0"
    return web.json_response({
        "version": "5.4",
        "status": "running" if state.running else "stopped",
        "paused": state.paused,
        "block_trading": state.block_trading,
        "block_reason": state.block_reason,
        "autonomous": state.autonomous,
        "mode": state.trading_mode,
        "min_score": state.min_score,
        "trend_tf": state.trend_tf,
        "exec_tf": state.exec_tf,
        "conf_tf": state.conf_tf,
        "top_down": state.top_down,
        "market_open": is_market_open(),
        "session": get_session(),
        "broker_connected": state.broker_connected,
        "account_id": state.account_id,
        "account_type": state.account_type,
        "pair": state.pair_key,
        "symbol": state.active_symbol,
        "price": state.current_price,
        "trend": state.trend_bias,
        "zone": state.premium_discount,
        "session_now": state.session_now,
        "ob_score": state.ob_score,
        "atr_ok": state.atr_filter_ok,
        "balance": state.account_balance,
        "risk_pct": state.risk_pct,
        "small_acc": state.small_acc_mode,
        "trades": state.trade_count,
        "wins": state.wins,
        "losses": state.losses,
        "winrate": wr,
        "total_pnl": state.total_pnl,
        "open_contracts": len(state.open_contracts),
        "history": len(state.trade_history),
        "h1": len(state.h1_candles),
        "m15": len(state.m15_candles),
        "m5": len(state.m5_candles),
        "m1": len(state.m1_candles),
        "news_events": len(state.news_events),
        "next_red": state.next_red_event.title if state.next_red_event else None,
        "gran_actual": state.gran_actual,
        "tv_available": TV_AVAILABLE,
        "supabase_enabled": supabase is not None,
    })

async def start_health():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"Health :{PORT}")

# ==================================================
# 21. ENTRY POINT
# ==================================================
async def main():
    log.info("╔══════════════════════════════════════════════╗")
    log.info("║  SMC SNIPER EA v5.4 · Fully Customizable     ║")
    log.info("║ News Shield | Broker Connect | Post-Reports  ║")
    log.info("║  [DUPLICATE FIX | CHART FIX | CUSTOM SETTINGS]║")
    log.info("╚══════════════════════════════════════════════╝")
    _load_saved_token()
    if not state.deriv_token:
        log.warning("No DERIV_API_TOKEN — bot will prompt user via /connect")
    await asyncio.gather(
        start_health(),
        ws_loop(),
        trading_loop(),
        tg_poll_loop(),
        chart_loop(),
        news_refresh_loop(),
        news_block_monitor(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down…")
        state.running = False
