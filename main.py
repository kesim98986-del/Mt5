"""
╔══════════════════════════════════════════════════════════════════╗
║  SMC ELITE EA  v4.0  —  News-Aware Multi-Pair Forex Bot         ║
║  Senior Quant SMC | News Shield | Forex Factory Parser          ║
║  Amharic News Summary | Zero-Noise Alerts | Railway-Ready       ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import logging
import os
import re
import time
import traceback
from collections  import deque
from datetime     import datetime, timedelta, timezone
from typing       import List, Optional
from zoneinfo     import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches   as mpatches
import matplotlib.pyplot    as plt
import matplotlib.gridspec  as gridspec
import numpy  as np
import pandas as pd
import requests
import websockets
from aiohttp import web
from bs4     import BeautifulSoup

# ══════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(message)s",
    datefmt= "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("SMC-v4")

# ══════════════════════════════════════════════════════
# PAIR REGISTRY
# key → (primary_sym, otc_sym, pip_val, min_stake, display)
# ══════════════════════════════════════════════════════
PAIR_REGISTRY = {
    "XAUUSD": ("frxXAUUSD","OTC_XAUUSD", 0.01,   1.0, "XAU/USD 🥇"),
    "EURUSD": ("frxEURUSD","OTC_EURUSD", 0.0001, 1.0, "EUR/USD 🇪🇺"),
    "GBPUSD": ("frxGBPUSD","OTC_GBPUSD", 0.0001, 1.0, "GBP/USD 🇬🇧"),
    "US100":  ("frxUS100", "OTC_NDX",    0.1,    1.0, "NASDAQ 💻"),
}

GRAN_FALLBACKS = {
    900:  [900, 600, 1800],
    3600: [3600, 7200],
    300:  [300, 180, 600],
}

# ══════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════
DERIV_APP_ID     = os.getenv("DERIV_APP_ID",  "1089")
DERIV_API_TOKEN  = os.getenv("DERIV_API_TOKEN","")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID","")
PORT             = int(os.getenv("PORT","8080"))
CHART_INTERVAL   = int(os.getenv("CHART_INTERVAL","300"))
NEWS_INTERVAL    = 12 * 3600          # 12 hours
PRE_NEWS_BLOCK   = 30 * 60           # 30 min before red news → block trading
POST_NEWS_WAIT   = 15 * 60           # 15 min after red news  → stay blocked
DERIV_WS_BASE    = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"
NY_TZ            = ZoneInfo("America/New_York")   # Forex Factory uses ET

# ══════════════════════════════════════════════════════
# NEWS DATA STRUCTURES
# ══════════════════════════════════════════════════════
class NewsEvent:
    """One Forex Factory calendar row."""
    __slots__ = ("time_et","currency","impact","title","actual","forecast","prev","dt_utc")
    def __init__(self, time_et, currency, impact, title, actual="", forecast="", prev=""):
        self.time_et  = time_et      # "8:30am"
        self.currency = currency     # "USD" / "XAU"
        self.impact   = impact       # "high" / "medium" / "low"
        self.title    = title
        self.actual   = actual
        self.forecast = forecast
        self.prev     = prev
        self.dt_utc   = None         # filled after parsing

    @property
    def is_red(self):    return self.impact == "high"
    @property
    def is_orange(self): return self.impact == "medium"

# ══════════════════════════════════════════════════════
# GLOBAL STATE
# ══════════════════════════════════════════════════════
class BotState:
    def __init__(self):
        self.running           = True
        self.paused            = False
        self.block_trading     = False   # True during news window
        self.block_reason      = ""      # human-readable reason
        self.block_until       = 0.0     # epoch when block lifts

        self.account_balance   = 0.0
        self.account_currency  = "USD"

        self.pair_key          = "XAUUSD"
        self.active_symbol     = ""
        self.risk_pct          = 0.01
        self.tp1_r             = 2.0
        self.tp2_r             = 4.0
        self.tp3_r             = 6.0
        self.small_acc_mode    = False

        self.h1_candles        = deque(maxlen=300)
        self.m15_candles       = deque(maxlen=300)
        self.m5_candles        = deque(maxlen=300)
        self.gran_actual       = {3600:3600, 900:900, 300:300}

        self.current_price     = 0.0
        self.trend_bias        = "NEUTRAL"
        self.last_signal       = None
        self.active_ob         = None
        self.active_fvg        = None
        self.active_trap       = None
        self.active_idm        = None
        self.premium_discount  = "NEUTRAL"
        self.ob_score          = 0

        # ── Win-rate boosters ──
        self.session_bias      = "NEUTRAL"  # LONDON/NY/ASIA
        self.atr_filter_ok     = True       # False when ATR too low (dead market)
        self.confluence_score  = 0          # 0-10 extra confluence points

        self.ws                = None
        self.req_id            = 1
        self.pending_requests  = {}
        self.subscribed_symbol = None

        self.open_contracts    = {}
        self.trade_count       = 0
        self.wins              = 0
        self.losses            = 0
        self.total_pnl         = 0.0
        self.trade_history     = []

        # ── News ──
        self.news_events       : List[NewsEvent] = []
        self.news_last_fetch   = 0.0
        self.next_red_event    : Optional[NewsEvent] = None
        self.news_chart_path   = None        # last generated news screenshot

    @property
    def pair_info(self):   return PAIR_REGISTRY[self.pair_key]
    @property
    def pair_display(self): return self.pair_info[4]

state = BotState()

# ══════════════════════════════════════════════════════
# KEYBOARDS  (updated with 📰 News button)
# ══════════════════════════════════════════════════════
def kb_main():
    block_icon = "🚫" if state.block_trading else "▶️"
    block_txt  = "Unblock" if state.block_trading else "Resume"
    return {"inline_keyboard":[
        [{"text":"💰 Balance",   "callback_data":"cmd_balance"},
         {"text":"📊 Chart",     "callback_data":"cmd_chart"}],
        [{"text":"📰 News",      "callback_data":"cmd_news"},
         {"text":"📋 History",   "callback_data":"cmd_history"}],
        [{"text":"⚙️ Settings",  "callback_data":"cmd_settings"},
         {"text":"⚡ Status",    "callback_data":"cmd_status"}],
        [{"text":"🛑 Stop",      "callback_data":"cmd_stop"},
         {"text":f"{block_icon} {block_txt}", "callback_data":"cmd_resume"}],
    ]}

def kb_settings():
    r  = state.risk_pct*100
    sm = "✅ ON" if state.small_acc_mode else "OFF"
    return {"inline_keyboard":[
        [{"text":"💱 Select Pair",          "callback_data":"cmd_pair_menu"}],
        [{"text":f"💎 Small Acc ($10) [{sm}]","callback_data":"cmd_small_acc"}],
        [{"text":f"{'✅' if r==1 else ''}1% Risk","callback_data":"cmd_risk_1"},
         {"text":f"{'✅' if r==3 else ''}3% Risk","callback_data":"cmd_risk_3"},
         {"text":f"{'✅' if r==5 else ''}5% Risk","callback_data":"cmd_risk_5"}],
        [{"text":"⬅️ Back","callback_data":"cmd_back"}],
    ]}

def kb_pair_menu():
    rows,row=[],[]
    for key,info in PAIR_REGISTRY.items():
        tick="✅ " if key==state.pair_key else ""
        row.append({"text":tick+info[4],"callback_data":f"cmd_pair_{key}"})
        if len(row)==2: rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([{"text":"⬅️ Back","callback_data":"cmd_settings"}])
    return {"inline_keyboard":rows}

# ══════════════════════════════════════════════════════
# TELEGRAM HELPERS
# ══════════════════════════════════════════════════════
def tg_send(text:str, photo_path:str=None, reply_markup=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    base   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    markup = reply_markup if reply_markup is not None else kb_main()
    try:
        if photo_path:
            with open(photo_path,"rb") as fh:
                r=requests.post(f"{base}/sendPhoto",data={
                    "chat_id":TELEGRAM_CHAT_ID,"caption":text[:1024],
                    "reply_markup":json.dumps(markup),"parse_mode":"Markdown",
                },files={"photo":fh},timeout=20)
        else:
            r=requests.post(f"{base}/sendMessage",json={
                "chat_id":TELEGRAM_CHAT_ID,"text":text,
                "reply_markup":markup,"parse_mode":"Markdown",
            },timeout=10)
        if r.status_code not in (200,201):
            log.warning(f"TG {r.status_code}: {r.text[:120]}")
    except Exception as e:
        log.error(f"tg_send: {e}")

def tg_answer(cqid:str, text:str=""):
    if not TELEGRAM_TOKEN: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                      json={"callback_query_id":cqid,"text":text},timeout=5)
    except Exception: pass

async def tg_async(text:str, photo_path:str=None, reply_markup=None):
    loop=asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: tg_send(text,photo_path,reply_markup))

# ══════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────
#  NEWS ENGINE  —  Forex Factory Scraper
# ─────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════
FF_URL     = "https://www.forexfactory.com/calendar"
FF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
TARGET_CURRENCIES = {"USD","XAU"}
TARGET_IMPACTS    = {"high","medium"}


def _parse_ff_time(time_str: str, base_date: datetime) -> Optional[datetime]:
    """Parse '8:30am', '12:00pm', 'All Day' etc → UTC datetime."""
    time_str = time_str.strip().lower()
    if not time_str or time_str in ("all day","tentative","","—"):
        return base_date.replace(hour=0, minute=0, second=0)
    try:
        t = datetime.strptime(time_str, "%I:%M%p")
        dt_et = base_date.replace(hour=t.hour, minute=t.minute, second=0,
                                  tzinfo=NY_TZ)
        return dt_et.astimezone(timezone.utc)
    except Exception:
        return None


def fetch_news() -> List[NewsEvent]:
    """
    Scrape Forex Factory calendar for today + tomorrow.
    Returns list of NewsEvent filtered to USD/XAU, high/medium only.
    """
    events: List[NewsEvent] = []
    today = datetime.now(NY_TZ)

    for offset in (0, 1):
        target_date = today + timedelta(days=offset)
        date_str    = target_date.strftime("%b%d.%Y").lower()  # e.g. jan01.2025
        url         = f"{FF_URL}?day={date_str}"
        try:
            resp = requests.get(url, headers=FF_HEADERS, timeout=15)
            if resp.status_code != 200:
                log.warning(f"FF HTTP {resp.status_code} for {url}")
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            rows = soup.select("tr.calendar__row")

            current_time = ""
            for row in rows:
                # Time cell (may be empty if same time as prev)
                time_cell = row.select_one(".calendar__time")
                if time_cell:
                    t = time_cell.get_text(strip=True)
                    if t: current_time = t

                # Currency
                cur_cell = row.select_one(".calendar__currency")
                currency = cur_cell.get_text(strip=True) if cur_cell else ""

                # Impact
                imp_cell = row.select_one(".calendar__impact span")
                impact   = ""
                if imp_cell:
                    cls = " ".join(imp_cell.get("class",[]))
                    if "high"   in cls: impact = "high"
                    elif "medium" in cls: impact = "medium"
                    elif "low"  in cls: impact = "low"
                    elif "holiday" in cls: impact = "holiday"

                # Title
                ev_cell = row.select_one(".calendar__event-title")
                title   = ev_cell.get_text(strip=True) if ev_cell else ""

                # Actual / Forecast / Prev
                actual   = (row.select_one(".calendar__actual")   or type("",(),{"get_text":lambda *a,**k:""})()).get_text(strip=True)
                forecast = (row.select_one(".calendar__forecast")  or type("",(),{"get_text":lambda *a,**k:""})()).get_text(strip=True)
                prev     = (row.select_one(".calendar__previous")  or type("",(),{"get_text":lambda *a,**k:""})()).get_text(strip=True)

                if not title or not currency: continue
                if currency not in TARGET_CURRENCIES: continue
                if impact not in TARGET_IMPACTS:      continue

                ev         = NewsEvent(current_time, currency, impact, title, actual, forecast, prev)
                ev.dt_utc  = _parse_ff_time(current_time, target_date)
                events.append(ev)

        except Exception as e:
            log.error(f"FF scrape error offset={offset}: {e}")

    events.sort(key=lambda e: e.dt_utc or datetime.min.replace(tzinfo=timezone.utc))
    log.info(f"📰 Fetched {len(events)} relevant news events")
    return events


def _next_red_event() -> Optional[NewsEvent]:
    """Return the next upcoming HIGH impact event from the fetched list."""
    now = datetime.now(timezone.utc)
    for ev in state.news_events:
        if ev.is_red and ev.dt_utc and ev.dt_utc > now:
            return ev
    return None


def _news_block_status() -> tuple:
    """
    Returns (should_block: bool, reason: str).
    Logic:
      - 30 min BEFORE a red event  → block
      - 15 min AFTER  a red event  → block (post-volatility)
    """
    now = datetime.now(timezone.utc)
    for ev in state.news_events:
        if not ev.is_red or not ev.dt_utc:
            continue
        secs_until = (ev.dt_utc - now).total_seconds()
        secs_after = (now - ev.dt_utc).total_seconds()
        if 0 < secs_until <= PRE_NEWS_BLOCK:
            mins = int(secs_until // 60)
            return True, f"🚨 Red news in {mins}m: {ev.title}"
        if 0 < secs_after <= POST_NEWS_WAIT:
            mins = int((POST_NEWS_WAIT - secs_after) // 60)
            return True, f"⏳ Post-news cooldown: {mins}m left ({ev.title})"
    return False, ""


def _amharic_summary() -> str:
    """
    Write a brief Amharic safety assessment for today's news.
    Uses rule-based logic (no external AI needed).
    """
    now      = datetime.now(timezone.utc)
    red_evs  = [e for e in state.news_events if e.is_red and e.dt_utc and e.dt_utc > now]
    orng_evs = [e for e in state.news_events if e.is_orange and e.dt_utc and e.dt_utc > now]

    lines = []
    if not red_evs and not orng_evs:
        lines.append("✅ *ደህንነቱ የተጠበቀ ቀን*")
        lines.append("ዛሬ ለወርቅ (XAU/USD) ትልቅ ዜና የለም።")
        lines.append("የSMC ቦት ያለ እገዳ መስራት ይችላል።")
        lines.append("_ትሬዶችን ለመክፈት ጥሩ ቀን ነው።_")
    elif red_evs:
        titles = ", ".join(e.title[:30] for e in red_evs[:3])
        times  = ", ".join(
            e.dt_utc.astimezone(NY_TZ).strftime("%I:%M%p ET")
            for e in red_evs[:3] if e.dt_utc)
        lines.append("⚠️ *አደገኛ ቀን — ጥንቃቄ ይደረግ!*")
        lines.append(f"ከፍተኛ ተጽዕኖ ያለው ዜና: `{titles}`")
        lines.append(f"ሰዓት: `{times}`")
        lines.append("")
        lines.append("📌 *ምክር:* ዜናው ከ30 ደቂቃ በፊት ቦቱ ይቆማል።")
        lines.append("ዜናው ከጨረሰ በኋላ 15 ደቂቃ ይጠብቃል።")
        lines.append("_ወርቅን ዛሬ በጥንቃቄ ይንግዱ!_")
    else:
        lines.append("🟡 *መካከለኛ ጥንቃቄ*")
        lines.append("ዛሬ መካከለኛ ተጽዕኖ ያለው ዜና አለ።")
        lines.append("ቦቱ ይሰራል፣ ነገር ግን ወርቅን ይጠንቀቁ።")

    return "\n".join(lines)


def generate_news_chart() -> Optional[str]:
    """
    Render a news calendar table as a matplotlib image.
    Red = high impact, Orange = medium.
    """
    events = state.news_events
    if not events:
        return None

    now    = datetime.now(timezone.utc)
    ny_now = now.astimezone(NY_TZ)

    rows_data = []
    for ev in events[:18]:   # max 18 rows
        t_str = (ev.dt_utc.astimezone(NY_TZ).strftime("%I:%M%p")
                 if ev.dt_utc else ev.time_et)
        rows_data.append([
            t_str,
            ev.currency,
            "🔴" if ev.is_red else "🟠",
            ev.title[:42],
            ev.forecast or "—",
            ev.actual   or "—",
        ])

    fig, ax = plt.subplots(figsize=(14, max(4, len(rows_data)*0.42+1.5)),
                            facecolor="#0d1117")
    ax.set_facecolor("#0d1117")
    ax.axis("off")

    col_labels = ["Time (ET)", "Curr", "Impact", "Event", "Forecast", "Actual"]
    col_widths = [0.10, 0.06, 0.07, 0.45, 0.14, 0.14]

    table = ax.table(
        cellText   = rows_data,
        colLabels  = col_labels,
        cellLoc    = "left",
        loc        = "center",
        colWidths  = col_widths,
    )
    table.auto_set_font_size(False)
    table.set_fontsize(8)
    table.scale(1, 1.5)

    # Style header
    for j in range(len(col_labels)):
        table[0, j].set_facecolor("#1f2937")
        table[0, j].set_text_props(color="#cdd9e5", fontweight="bold",
                                   fontfamily="monospace")

    # Style rows
    for i, ev in enumerate(events[:18]):
        row_color = "#2d0000" if ev.is_red else "#2d1a00" if ev.is_orange else "#161b22"
        txt_color = "#ff6b6b" if ev.is_red else "#ffa94d" if ev.is_orange else "#90a4ae"
        for j in range(len(col_labels)):
            table[i+1, j].set_facecolor(row_color)
            table[i+1, j].set_text_props(color=txt_color, fontfamily="monospace")

    ts    = ny_now.strftime("%A %b %d, %Y  %I:%M%p ET")
    nxt   = state.next_red_event
    nxt_t = (f" | Next🔴: {nxt.title[:25]} @ "
             f"{nxt.dt_utc.astimezone(NY_TZ).strftime('%I:%M%p ET')}"
             if nxt and nxt.dt_utc else "")
    ax.set_title(f"📰 Forex Factory — USD & XAU/USD News  ·  {ts}{nxt_t}",
                 color="#cdd9e5", fontsize=9, fontfamily="monospace", pad=12)

    path = "/tmp/smc_news.png"
    plt.tight_layout()
    plt.savefig(path, dpi=130, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


async def news_refresh_loop():
    """Fetch news every 12 h, enforce trading blocks in real-time."""
    while state.running:
        try:
            loop = asyncio.get_event_loop()
            events = await loop.run_in_executor(None, fetch_news)
            state.news_events     = events
            state.news_last_fetch = time.time()
            state.next_red_event  = _next_red_event()

            # Generate and cache the news chart
            path = await loop.run_in_executor(None, generate_news_chart)
            state.news_chart_path = path

            # Proactive Telegram alert if dangerous day
            red_count = sum(1 for e in events if e.is_red)
            if red_count:
                nxt = state.next_red_event
                nxt_info = ""
                if nxt and nxt.dt_utc:
                    ny_t = nxt.dt_utc.astimezone(NY_TZ).strftime("%I:%M%p ET")
                    nxt_info = f"\nNext🔴: *{nxt.title}* @ `{ny_t}`"
                summary = _amharic_summary()
                msg = (
                    f"📰 *ዜና ዝማኔ — News Refresh*\n"
                    f"Found `{red_count}` 🔴 high-impact events today.\n"
                    f"{nxt_info}\n\n{summary}"
                )
                await tg_async(msg, photo_path=path)
            else:
                log.info("📰 No high-impact news today — free trading day")

        except Exception as e:
            log.error(f"news_refresh_loop: {e}")

        await asyncio.sleep(NEWS_INTERVAL)


async def news_block_monitor():
    """Check every 60s if we need to block/unblock trading due to news."""
    await asyncio.sleep(30)
    while state.running:
        try:
            should_block, reason = _news_block_status()
            if should_block and not state.block_trading:
                state.block_trading = True
                state.block_reason  = reason
                log.info(f"🚫 Trading BLOCKED: {reason}")
                await tg_async(
                    f"🚫 *Trading Blocked*\n{reason}\n\n"
                    f"_Bot will auto-resume after the news window._",
                    reply_markup=kb_main()
                )
            elif not should_block and state.block_trading:
                state.block_trading = False
                state.block_reason  = ""
                log.info("✅ Trading UNBLOCKED — news window passed")
                await tg_async(
                    "✅ *Trading Resumed*\n"
                    "News window has passed. SMC scanning restarted.",
                    reply_markup=kb_main()
                )
        except Exception as e:
            log.error(f"news_block_monitor: {e}")
        await asyncio.sleep(60)


# ══════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────
#  CHART ENGINE
# ─────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════
CHART_BG = "#0d1117"; PANEL_BG="#161b22"
BULL_C="#00e676"; BEAR_C="#ff1744"
OB_B="#00bcd4";   OB_R="#ff9800"
FVG_C="#ce93d8";  TRAP_C="#ffeb3b"; IDM_C="#80cbc4"
ENTRY_C="#2979ff"; SL_C="#f44336"
TP_CS=["#69f0ae","#40c4ff","#b388ff"]
TEXT_C="#cdd9e5"; GRID_C="#1e2a38"

def _ax(ax):
    ax.set_facecolor(PANEL_BG)
    ax.tick_params(colors="#90a4ae",labelsize=7)
    for s in ax.spines.values(): s.set_edgecolor(GRID_C)
    ax.grid(axis="y",color=GRID_C,linewidth=0.4,alpha=0.6)

def _rsi(p:np.ndarray,n:int=14)->np.ndarray:
    if len(p)<n+1: return np.full(len(p),50.)
    d=np.diff(p); g=np.where(d>0,d,0.); l=np.where(d<0,-d,0.)
    ag=np.convolve(g,np.ones(n)/n,mode="valid")
    al=np.convolve(l,np.ones(n)/n,mode="valid")
    rs=np.where(al!=0,ag/al,100.)
    rsi=100.-100./(1.+rs)
    return np.concatenate([np.full(len(p)-len(rsi),50.),rsi])

def generate_chart(candles:deque, tf:str="M15",
                   entry_price:float=None, exit_price:float=None,
                   direction:str=None, pnl:float=None,
                   chart_type:str="live") -> Optional[str]:
    if len(candles)<20: return None
    df=pd.DataFrame(list(candles)[-80:])
    df.columns=["time","open","high","low","close"]
    df.reset_index(drop=True,inplace=True)

    fig=plt.figure(figsize=(16,9),facecolor=CHART_BG)
    gs=gridspec.GridSpec(4,1,figure=fig,hspace=0.05,height_ratios=[4,.6,.6,.5])
    am=fig.add_subplot(gs[0]); av=fig.add_subplot(gs[1],sharex=am)
    ar=fig.add_subplot(gs[2],sharex=am); al=fig.add_subplot(gs[3])
    for a in(am,av,ar,al): _ax(a)

    for i,row in df.iterrows():
        c=BULL_C if row["close"]>=row["open"] else BEAR_C
        bl=min(row["open"],row["close"]); bh=max(row["open"],row["close"])
        am.plot([i,i],[row["low"],row["high"]],color=c,lw=0.8,alpha=0.9)
        am.add_patch(mpatches.FancyBboxPatch(
            (i-.35,bl),.7,max(bh-bl,df["close"].mean()*.00005),
            boxstyle="square,pad=0",fc=c,ec=c,alpha=0.85))

    for i,row in df.iterrows():
        av.bar(i,1,color=BULL_C if row["close"]>=row["open"] else BEAR_C,alpha=0.4,width=.7)
    av.set_ylabel("Vol",color="#555d68",fontsize=6)

    rs=_rsi(df["close"].values)
    ar.plot(range(len(rs)),rs,color="#90a4ae",lw=0.9)
    ar.axhline(70,color=BEAR_C,lw=.5,ls="--",alpha=.5)
    ar.axhline(30,color=BULL_C,lw=.5,ls="--",alpha=.5)
    ar.set_ylim(0,100); ar.set_ylabel("RSI",color="#555d68",fontsize=6)

    if state.active_ob:
        ob=state.active_ob; oc=OB_B if ob["type"]=="BULL" else OB_R
        xs=max(0,len(df)-35)/len(df)
        am.axhspan(ob["low"],ob["high"],xmin=xs,alpha=.15,color=oc)
        am.axhline(ob["high"],color=oc,ls="--",lw=.8,alpha=.7)
        am.axhline(ob["low"], color=oc,ls="--",lw=.8,alpha=.7)
        am.text(2,ob["high"],f" {ob['type']} OB sc:{state.ob_score}",
                color=oc,fontsize=7,va="bottom",fontfamily="monospace")

    if state.active_fvg:
        fvg=state.active_fvg
        am.axhspan(fvg["low"],fvg["high"],alpha=.13,color=FVG_C)
        am.text(2,fvg["high"],"  FVG",color=FVG_C,fontsize=7,va="bottom",fontfamily="monospace")

    if state.active_idm:
        idm=state.active_idm
        am.axhline(idm["level"],color=IDM_C,ls=":",lw=1.2,alpha=.9)
        am.text(2,idm["level"],f"  IDM{'✅' if idm.get('swept') else '⏳'}",
                color=IDM_C,fontsize=7,va="bottom",fontfamily="monospace")

    if state.active_trap:
        trap=state.active_trap
        am.axhline(trap["level"],color=TRAP_C,ls=":",lw=1.4,alpha=.9)
        am.text(2,trap["level"],f"  TRAP {trap['side']}",
                color=TRAP_C,fontsize=7,va="bottom",fontfamily="monospace")

    if state.last_signal and "fib_hi" in state.last_signal:
        sig=state.last_signal; mid=(sig["fib_hi"]+sig["fib_lo"])/2
        am.axhline(mid,color="#78909c",ls="-.",lw=.6,alpha=.5)
        am.axhspan(sig["fib_lo"],mid,alpha=.04,color=BULL_C)
        am.axhspan(mid,sig["fib_hi"],alpha=.04,color=BEAR_C)

    if state.last_signal:
        sig=state.last_signal
        am.axhline(sig["entry"],color=ENTRY_C,lw=1.6,ls="-")
        am.axhline(sig["sl"],   color=SL_C,   lw=1., ls="--")
        for tk,tc in zip(["tp1","tp2","tp3"],TP_CS):
            if tk in sig: am.axhline(sig[tk],color=tc,lw=.8,ls="-.")

    if entry_price:
        am.axhline(entry_price,color=ENTRY_C,lw=2.2,alpha=.9)
        am.annotate(f"▶ ENTRY {entry_price:.5f}",
                    xy=(len(df)-1,entry_price),color=ENTRY_C,fontsize=8,ha="right",fontfamily="monospace")
    if exit_price:
        ec=BULL_C if(pnl and pnl>0) else BEAR_C
        am.axhline(exit_price,color=ec,lw=2.,ls="--",alpha=.9)
        ps=f"+{pnl:.2f}" if pnl and pnl>0 else f"{pnl:.2f}"
        am.annotate(f"◀ EXIT {exit_price:.5f}  P&L:{ps}",
                    xy=(len(df)-1,exit_price),color=ec,fontsize=8,ha="right",fontfamily="monospace")

    # News event markers on chart
    if state.news_events:
        price_range = df["high"].max() - df["low"].min()
        for ev in state.news_events:
            if not ev.dt_utc or not ev.is_red: continue
            # find approximate candle index
            ev_epoch = ev.dt_utc.timestamp()
            for ci,crow in df.iterrows():
                if crow["time"] >= ev_epoch:
                    am.axvline(ci,color="#ff1744",lw=1.,ls="--",alpha=.5)
                    am.text(ci,df["high"].max(),"🔴",fontsize=8,ha="center")
                    break

    swh,swl=_swing(df)
    for i in swh: am.plot(i,df.iloc[i]["high"]*1.00015,"^",color=BULL_C,ms=4,alpha=.5)
    for i in swl: am.plot(i,df.iloc[i]["low"]*0.99985, "v",color=BEAR_C,ms=4,alpha=.5)

    tc=BULL_C if state.trend_bias=="BULLISH" else(BEAR_C if state.trend_bias=="BEARISH" else "#90a4ae")
    tl={"live":"📡 LIVE","entry":"🎯 ENTRY","exit":"🏁 CLOSED"}.get(chart_type,"")
    block_warn = "  🚫NEWS BLOCK" if state.block_trading else ""
    am.set_title(
        f"{tl}  {state.pair_display}  ·  {tf}  "
        f"·  Bias:{state.trend_bias}  Zone:{state.premium_discount}  "
        f"·  {state.current_price:.5f}{block_warn}",
        color=tc,fontsize=10,fontfamily="monospace",pad=8)
    am.set_ylabel("Price",color="#90a4ae",fontsize=8)

    al.set_xlim(0,1); al.set_ylim(0,1); al.axis("off")
    ts=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    nxt=state.next_red_event
    nxt_s=(f" | 🔴{nxt.title[:20]}@{nxt.dt_utc.astimezone(NY_TZ).strftime('%H:%Mh')}"
           if nxt and nxt.dt_utc else "")
    al.text(.01,.5,
        f"SMC ELITE v4  ·  {state.pair_display}  sym:{state.active_symbol}  "
        f"Bal:{state.account_balance:.2f} {state.account_currency}  "
        f"Risk:{state.risk_pct*100:.0f}%  OB:{state.ob_score}/100  "
        f"Session:{state.session_bias}  ATR:{'OK' if state.atr_filter_ok else 'LOW'}  "
        f"H1:{len(state.h1_candles)} M15:{len(state.m15_candles)} M5:{len(state.m5_candles)}"
        f"{nxt_s}  {ts}",
        color="#555d68",fontsize=6,va="center",fontfamily="monospace")

    plt.setp(am.get_xticklabels(),visible=False)
    plt.setp(av.get_xticklabels(),visible=False)
    plt.setp(ar.get_xticklabels(),visible=False)
    path=f"/tmp/smc_chart_{chart_type}.png"
    plt.tight_layout()
    plt.savefig(path,dpi=130,bbox_inches="tight",facecolor=fig.get_facecolor())
    plt.close(fig)
    return path

def generate_history_chart() -> Optional[str]:
    h=state.trade_history[-20:]
    if not h: return None
    fig,(ax1,ax2)=plt.subplots(2,1,figsize=(14,9),facecolor=CHART_BG,
                                gridspec_kw={"height_ratios":[2,1]})
    for a in(ax1,ax2): _ax(a)
    labels=[f"#{t['num']}" for t in h]; pnls=[t["pnl"] for t in h]
    colors=[BULL_C if p>0 else BEAR_C for p in pnls]
    bars=ax1.bar(labels,pnls,color=colors,alpha=.85,ec=GRID_C)
    ax1.axhline(0,color=GRID_C,lw=.8)
    for bar,val in zip(bars,pnls):
        s="+" if val>=0 else ""
        ax1.text(bar.get_x()+bar.get_width()/2,
                 bar.get_height()+(0.05 if val>=0 else -.15),
                 f"{s}{val:.2f}",ha="center",va="bottom",
                 color=TEXT_C,fontsize=7,fontfamily="monospace")
    wr=f"{state.wins/(state.wins+state.losses)*100:.1f}%" if(state.wins+state.losses)>0 else "N/A"
    ax1.set_title(
        f"📋 History  {state.wins}W/{state.losses}L  WR:{wr}  "
        f"P&L:{state.total_pnl:+.2f} {state.account_currency}  Bal:{state.account_balance:.2f}",
        color=TEXT_C,fontsize=10,fontfamily="monospace")
    ax1.set_ylabel("P&L",color="#90a4ae",fontsize=9)
    cum=np.cumsum(pnls); cc=BULL_C if cum[-1]>=0 else BEAR_C
    ax2.plot(labels,cum,color=cc,lw=1.8,marker="o",ms=4)
    ax2.fill_between(labels,cum,alpha=.12,color=cc)
    ax2.axhline(0,color=GRID_C,lw=.8); ax2.set_ylabel("Cumulative",color="#90a4ae",fontsize=8)
    ts=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fig.text(.99,.01,f"SMC ELITE v4 · {ts}",color="#444d56",fontsize=7,ha="right")
    path="/tmp/smc_history.png"
    plt.tight_layout()
    plt.savefig(path,dpi=130,bbox_inches="tight",facecolor=fig.get_facecolor())
    plt.close(fig); return path

# ══════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────
#  SMC ENGINE  (strategy 100% intact + win-rate boosters)
# ─────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════
def _swing(df:pd.DataFrame,n:int=5):
    highs,lows=[],[]
    for i in range(n,len(df)-n):
        if df["high"].iloc[i]==df["high"].iloc[i-n:i+n+1].max(): highs.append(i)
        if df["low"].iloc[i] ==df["low"].iloc[i-n:i+n+1].min():  lows.append(i)
    return highs,lows

def detect_bos_choch(df:pd.DataFrame):
    highs,lows=_swing(df)
    if len(highs)<2 or len(lows)<2: return None
    lsh,psh=highs[-1],highs[-2]; lsl,psl=lows[-1],lows[-2]
    lc=df["close"].iloc[-1]
    if lc>df["high"].iloc[lsh] and lsh>psh:
        return {"type":"BOS","direction":"BULLISH","level":df["high"].iloc[lsh]}
    if lc<df["low"].iloc[lsl] and lsl>psl:
        return {"type":"BOS","direction":"BEARISH","level":df["low"].iloc[lsl]}
    if df["high"].iloc[lsh]<df["high"].iloc[psh] and lc>df["high"].iloc[lsh]:
        return {"type":"CHoCH","direction":"BULLISH","level":df["high"].iloc[lsh]}
    if df["low"].iloc[lsl]>df["low"].iloc[psl] and lc<df["low"].iloc[lsl]:
        return {"type":"CHoCH","direction":"BEARISH","level":df["low"].iloc[lsl]}
    return None

def detect_idm(df:pd.DataFrame,direction:str):
    highs,lows=_swing(df,n=3)
    if direction=="BULLISH" and len(lows)>=2:
        lvl=df["low"].iloc[lows[-2]]; last=df.iloc[-1]
        return {"side":"BUY","level":lvl,"swept":last["low"]<lvl and last["close"]>lvl}
    if direction=="BEARISH" and len(highs)>=2:
        lvl=df["high"].iloc[highs[-2]]; last=df.iloc[-1]
        return {"side":"SELL","level":lvl,"swept":last["high"]>lvl and last["close"]<lvl}
    return None

def detect_trap(df:pd.DataFrame):
    tol=0.0003 if state.pair_key in("EURUSD","GBPUSD") else 0.0005
    r=df.iloc[-25:]; hs=r["high"].values; ls=r["low"].values
    for i in range(len(hs)-1,1,-1):
        for j in range(i-1,max(i-8,0),-1):
            if abs(hs[i]-hs[j])/hs[j]<tol:
                lvl=(hs[i]+hs[j])/2; last=df.iloc[-1]
                if last["high"]>lvl and last["close"]<lvl:
                    return {"side":"SELL","level":lvl,"swept":True,"type":"EQL_HIGHS"}
    for i in range(len(ls)-1,1,-1):
        for j in range(i-1,max(i-8,0),-1):
            if abs(ls[i]-ls[j])/ls[j]<tol:
                lvl=(ls[i]+ls[j])/2; last=df.iloc[-1]
                if last["low"]<lvl and last["close"]>lvl:
                    return {"side":"BUY","level":lvl,"swept":True,"type":"EQL_LOWS"}
    return None

def identify_ob(df:pd.DataFrame,direction:str):
    lk=min(30,len(df)-3); r=df.iloc[-lk:].reset_index(drop=True)
    ab=(r["close"]-r["open"]).abs().mean()
    if direction=="BULLISH":
        for i in range(len(r)-3,1,-1):
            c,nc=r.iloc[i],r.iloc[i+1]
            if(c["close"]<c["open"] and nc["close"]>c["high"] and
               abs(nc["close"]-nc["open"])>ab*1.5):
                return {"type":"BULL","high":c["high"],"low":c["low"],
                        "body_hi":max(c["open"],c["close"]),
                        "body_lo":min(c["open"],c["close"]),
                        "displacement":round(abs(nc["close"]-nc["open"])/ab,2)}
    elif direction=="BEARISH":
        for i in range(len(r)-3,1,-1):
            c,nc=r.iloc[i],r.iloc[i+1]
            if(c["close"]>c["open"] and nc["close"]<c["low"] and
               abs(nc["close"]-nc["open"])>ab*1.5):
                return {"type":"BEAR","high":c["high"],"low":c["low"],
                        "body_hi":max(c["open"],c["close"]),
                        "body_lo":min(c["open"],c["close"]),
                        "displacement":round(abs(nc["close"]-nc["open"])/ab,2)}
    return None

def identify_fvg(df:pd.DataFrame,ob:dict):
    if ob is None: return None
    thr=0.03 if state.pair_key in("EURUSD","GBPUSD") else 0.05
    r=df.iloc[-min(25,len(df)-3):].reset_index(drop=True)
    if ob["type"]=="BULL":
        for i in range(len(r)-3,0,-1):
            c1,c3=r.iloc[i],r.iloc[i+2]; gp=(c3["low"]-c1["high"])/c1["high"]*100
            if c1["high"]<c3["low"] and gp>=thr:
                return {"type":"BULL","high":c3["low"],"low":c1["high"],"gap_pct":gp}
    elif ob["type"]=="BEAR":
        for i in range(len(r)-3,0,-1):
            c1,c3=r.iloc[i],r.iloc[i+2]; gp=(c1["low"]-c3["high"])/c1["low"]*100
            if c1["low"]>c3["high"] and gp>=thr:
                return {"type":"BEAR","high":c1["low"],"low":c3["high"],"gap_pct":gp}
    return None

# ── WIN-RATE BOOSTER 1: Session Filter ──────────────────────────
def get_session() -> str:
    """
    Only trade during high-liquidity sessions.
    Best sessions for XAU: London (07:00–12:00 UTC) and NY (12:00–17:00 UTC).
    Asian session has low liquidity → skip.
    """
    h = datetime.now(timezone.utc).hour
    if  7 <= h < 12: return "LONDON"
    if 12 <= h < 17: return "NEW_YORK"
    if  0 <= h <  7: return "ASIA"
    return "CLOSE"

# ── WIN-RATE BOOSTER 2: ATR Volatility Filter ───────────────────
def calc_atr(df:pd.DataFrame, period:int=14) -> float:
    if len(df) < period+1: return 0.0
    tr = pd.concat([
        df["high"]-df["low"],
        (df["high"]-df["close"].shift()).abs(),
        (df["low"] -df["close"].shift()).abs(),
    ],axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])

def atr_filter_ok(df:pd.DataFrame) -> bool:
    """
    Skip trades when ATR is too low (dead/choppy market).
    Minimum ATR threshold per pair.
    """
    atr = calc_atr(df)
    thresholds = {"XAUUSD":0.5,"EURUSD":0.0005,"GBPUSD":0.0006,"US100":5.0}
    mn = thresholds.get(state.pair_key,0.0001)
    ok = atr >= mn
    state.atr_filter_ok = ok
    return ok

# ── WIN-RATE BOOSTER 3: Multi-timeframe EMA Trend Confirm ───────
def ema_trend_confirm(df:pd.DataFrame, bias:str) -> bool:
    """
    Price must be above EMA50 for buys, below for sells.
    Eliminates counter-trend trades.
    """
    if len(df)<52: return True   # not enough data, skip filter
    ema50 = df["close"].ewm(span=50,adjust=False).mean().iloc[-1]
    price = df["close"].iloc[-1]
    if bias=="BULLISH": return price > ema50
    if bias=="BEARISH": return price < ema50
    return False

# ── WIN-RATE BOOSTER 4: Candle confirmation ─────────────────────
def entry_candle_confirm(df:pd.DataFrame, direction:str) -> bool:
    """
    Wait for a confirmation candle at the OB:
    - BUY:  last candle must close bullish (body close above open)
    - SELL: last candle must close bearish
    Prevents entering on still-moving candles.
    """
    last = df.iloc[-1]
    if direction=="BUY":  return last["close"] > last["open"]
    if direction=="SELL": return last["close"] < last["open"]
    return False

def score_ob(ob,fvg,trap,idm,rsi,session,atr_ok,ema_ok,candle_ok)->int:
    s=10
    if fvg:  s+=20
    if trap  and trap.get("swept"):  s+=20
    if idm   and idm.get("swept"):   s+=15
    if ob    and ob.get("displacement",0)>=2.: s+=10
    if ob:
        if ob["type"]=="BULL" and rsi<45: s+=5
        if ob["type"]=="BEAR" and rsi>55: s+=5
    if session in("LONDON","NEW_YORK"): s+=10   # session bonus
    if atr_ok:   s+=5
    if ema_ok:   s+=5
    if candle_ok:s+=5
    state.confluence_score = s
    return min(s,100)

def calc_pd(df:pd.DataFrame):
    highs,lows=_swing(df,n=8)
    if not highs or not lows: return "NEUTRAL",0,0
    fh=df["high"].iloc[highs[-1]]; fl=df["low"].iloc[lows[-1]]
    if fh==fl: return "NEUTRAL",fh,fl
    pct=(state.current_price-fl)/(fh-fl)
    return ("DISCOUNT" if pct<.5 else "PREMIUM"),fh,fl

def analyze_h1():
    if len(state.h1_candles)<30: return "NEUTRAL"
    df=pd.DataFrame(list(state.h1_candles)); df.columns=["time","open","high","low","close"]
    r=detect_bos_choch(df)
    if r: state.trend_bias=r["direction"]
    else:
        c=df["close"].values[-20:]
        state.trend_bias="BULLISH" if np.polyfit(np.arange(len(c)),c,1)[0]>0 else "BEARISH"
    return state.trend_bias

def compute_signal(tf="M15"):
    buf=state.m15_candles if tf=="M15" else state.m5_candles
    if len(buf)<40: return None
    df=pd.DataFrame(list(buf)); df.columns=["time","open","high","low","close"]

    bias=analyze_h1()
    if bias=="NEUTRAL": return None

    struct=detect_bos_choch(df)
    if struct is None or struct["direction"]!=bias: return None

    pd_zone,fib_hi,fib_lo=calc_pd(df)
    state.premium_discount=pd_zone
    if bias=="BULLISH" and pd_zone!="DISCOUNT": return None
    if bias=="BEARISH" and pd_zone!="PREMIUM":  return None

    idm=detect_idm(df,bias); state.active_idm=idm
    if idm is None or not idm.get("swept"): return None

    trap=detect_trap(df); state.active_trap=trap
    if trap is None or not trap.get("swept"): return None
    if trap["side"]!=("BUY" if bias=="BULLISH" else "SELL"): return None

    ob=identify_ob(df,bias)
    if ob is None: return None
    state.active_ob=ob

    fvg=identify_fvg(df,ob); state.active_fvg=fvg

    rsi_now=float(_rsi(df["close"].values)[-1])

    # ── Apply win-rate boosters ──
    session  = get_session(); state.session_bias=session
    atr_ok   = atr_filter_ok(df)
    ema_ok   = ema_trend_confirm(df,bias)
    candle_ok= entry_candle_confirm(df,"BUY" if bias=="BULLISH" else "SELL")

    # Block bad sessions for XAU (Asia = low liquidity)
    if state.pair_key=="XAUUSD" and session=="ASIA":
        log.info("Skipping: Asian session — low XAU liquidity")
        return None

    # Block market close
    if session=="CLOSE":
        log.info("Skipping: Market close hours")
        return None

    # ATR check
    if not atr_ok:
        log.info("Skipping: ATR too low — dead market")
        return None

    # EMA trend filter
    if not ema_ok:
        log.info("Skipping: EMA50 not confirming trend direction")
        return None

    sc=score_ob(ob,fvg,trap,idm,rsi_now,session,atr_ok,ema_ok,candle_ok)
    state.ob_score=sc
    min_sc=50 if state.small_acc_mode else 60
    if sc<min_sc:
        log.info(f"Score {sc}<{min_sc} — skipping")
        return None

    if bias=="BULLISH":
        entry=ob["body_hi"]; sl=ob["low"]*.9995
    else:
        entry=ob["body_lo"]; sl=ob["high"]*1.0005

    risk=abs(entry-sl)
    if risk==0: return None
    mult=1 if bias=="BULLISH" else -1
    tp1=entry+risk*state.tp1_r*mult
    tp2=entry+risk*state.tp2_r*mult
    tp3=entry+risk*state.tp3_r*mult
    stake=max(1.,round(max(state.pair_info[3],
                           state.account_balance*state.risk_pct),2))

    logic=(
        f"{tf} {struct['type']} {bias} | IDM✅ | {trap['type']}✅ | "
        f"{ob['type']}OB(sc:{sc}) | {pd_zone} | RSI:{rsi_now:.1f} | "
        f"Sess:{session} | EMA✅ | Candle✅"
    )
    sig={
        "direction":"BUY" if bias=="BULLISH" else "SELL",
        "entry":round(entry,5),"sl":round(sl,5),
        "tp1":round(tp1,5),"tp2":round(tp2,5),"tp3":round(tp3,5),
        "risk_r":round(risk,5),"stake":stake,
        "struct":struct["type"],"ob":ob,"fvg":fvg,
        "trap":trap,"idm":idm,"ob_score":sc,"rsi":rsi_now,
        "pd_zone":pd_zone,"fib_hi":fib_hi,"fib_lo":fib_lo,
        "session":session,"tf":tf,"bias":bias,"logic":logic,
        "ts":datetime.now(timezone.utc).isoformat(),
    }
    state.last_signal=sig
    return sig

def check_trade_mgmt():
    for cid,info in list(state.open_contracts.items()):
        sig=info.get("signal")
        if not sig: continue
        p,d=state.current_price,info["direction"]
        if not info["be_moved"]:
            if(d=="BUY" and p>=sig["tp1"]) or(d=="SELL" and p<=sig["tp1"]):
                info["be_moved"]=True; sig["sl"]=sig["entry"]
                asyncio.ensure_future(tg_async(
                    f"✅ *BreakEven* `{cid}`\nSL → entry `{sig['entry']}`"))
        if len(state.m15_candles)>=10:
            df=pd.DataFrame(list(state.m15_candles)[-30:])
            df.columns=["time","open","high","low","close"]
            swh,swl=_swing(df,n=3)
            if d=="BUY"  and swl:
                t=df["low"].iloc[swl[-1]]*.9998
                if t>sig["sl"]: sig["sl"]=t
            elif d=="SELL" and swh:
                t=df["high"].iloc[swh[-1]]*1.0002
                if t<sig["sl"]: sig["sl"]=t

# ══════════════════════════════════════════════════════
# DERIV WEBSOCKET
# ══════════════════════════════════════════════════════
async def send_req(payload:dict)->dict:
    if state.ws is None: raise RuntimeError("WS not connected")
    rid=state.req_id; state.req_id+=1
    payload["req_id"]=rid
    fut=asyncio.get_event_loop().create_future()
    state.pending_requests[rid]=fut
    await state.ws.send(json.dumps(payload))
    try:
        return await asyncio.wait_for(asyncio.shield(fut),timeout=20)
    except asyncio.TimeoutError:
        state.pending_requests.pop(rid,None)
        raise asyncio.TimeoutError(f"Timeout {list(payload.keys())}")

async def authorize():
    r=await send_req({"authorize":DERIV_API_TOKEN})
    if "error" in r: raise RuntimeError(f"Auth: {r['error']['message']}")
    log.info(f"Authorized: {r['authorize']['loginid']}")

async def get_balance():
    r=await send_req({"balance":1,"subscribe":0})
    if "balance" in r:
        state.account_balance =r["balance"]["balance"]
        state.account_currency=r["balance"]["currency"]

def _store(nom_gran:int,rows:list):
    if nom_gran==3600: state.h1_candles.extend(rows)
    elif nom_gran==900:
        state.m15_candles.extend(rows)
        if rows: state.current_price=float(rows[-1][4])
    elif nom_gran==300:
        state.m5_candles.extend(rows)
        if rows: state.current_price=float(rows[-1][4])

async def _fetch(sym:str,nom_gran:int)->int:
    lbl={3600:"H1",900:"M15",300:"M5"}
    for ag in GRAN_FALLBACKS.get(nom_gran,[nom_gran]):
        try:
            r=await send_req({"ticks_history":sym,"end":"latest",
                              "count":200,"granularity":ag,"style":"candles"})
            if "error" in r:
                log.warning(f"Fetch {sym} gran={ag}: {r['error'].get('message','?')}")
                continue
            raw=r.get("candles",[])
            if not raw: continue
            rows=[(int(c["epoch"]),float(c["open"]),float(c["high"]),
                   float(c["low"]),float(c["close"])) for c in raw]
            state.gran_actual[nom_gran]=ag
            _store(nom_gran,rows)
            log.info(f"✅ {len(rows)} {lbl.get(nom_gran,'?')} (gran={ag}) {sym}")
            asyncio.ensure_future(send_req({"ticks_history":sym,"end":"latest",
                "count":1,"granularity":ag,"style":"candles","subscribe":1}))
            return len(rows)
        except asyncio.TimeoutError:
            log.warning(f"Timeout gran={ag} {sym}")
        except Exception as e:
            log.error(f"_fetch {sym} gran={ag}: {e}")
    return 0

async def _resolve_sym(key:str)->str:
    pri,otc=PAIR_REGISTRY[key][0],PAIR_REGISTRY[key][1]
    for sym in(pri,otc):
        try:
            r=await send_req({"ticks_history":sym,"end":"latest",
                               "count":1,"granularity":3600,"style":"candles"})
            if "candles" in r:
                log.info(f"Symbol OK: {sym}"); return sym
            log.warning(f"Symbol {sym}: {r.get('error',{}).get('message','?')}")
        except Exception as e:
            log.warning(f"Symbol test {sym}: {e}")
    return pri

async def subscribe_pair(key:str):
    state.h1_candles.clear(); state.m15_candles.clear(); state.m5_candles.clear()
    state.last_signal=None; state.active_ob=None; state.active_fvg=None
    state.active_trap=None; state.active_idm=None
    state.current_price=0.; state.trend_bias="NEUTRAL"
    state.ob_score=0; state.premium_discount="NEUTRAL"
    sym=await _resolve_sym(key)
    state.active_symbol=sym; state.subscribed_symbol=sym
    h=await _fetch(sym,3600)
    m=await _fetch(sym,900)
    f=await _fetch(sym,300)
    log.info(f"subscribe_pair: {sym} H1:{h} M15:{m} M5:{f}")
    return h,m,f

async def open_contract(direction:str,amount:float):
    if state.paused or state.block_trading: return None
    ct="MULTUP" if direction=="BUY" else "MULTDOWN"
    try:
        r=await send_req({"buy":1,"price":round(amount,2),"parameters":{
            "contract_type":ct,"symbol":state.active_symbol or PAIR_REGISTRY[state.pair_key][0],
            "amount":round(amount,2),"currency":state.account_currency,
            "multiplier":10,"basis":"stake","stop_out":1}})
        if "error" in r: log.error(f"Buy: {r['error']['message']}"); return None
        cid=r["buy"]["contract_id"]
        state.open_contracts[cid]={
            "direction":direction,"entry":state.current_price,
            "amount":amount,"signal":state.last_signal,
            "be_moved":False,"opened_at":time.time()}
        state.trade_count+=1
        log.info(f"✅ Opened {cid} [{direction}] ${amount:.2f}")
        asyncio.ensure_future(send_req({"proposal_open_contract":1,
                                        "contract_id":cid,"subscribe":1}))
        return cid
    except Exception as e:
        log.error(f"open_contract: {e}"); return None

async def close_contract(cid:str):
    try:
        r=await send_req({"sell":cid,"price":0})
        if "error" in r: log.error(f"Close: {r['error']['message']}"); return False
        state.open_contracts.pop(cid,None); log.info(f"🔴 Closed {cid}"); return True
    except Exception as e:
        log.error(f"close_contract: {e}"); return False

async def close_all():
    ids=list(state.open_contracts.keys())
    for cid in ids: await close_contract(cid)
    return len(ids)

# ══════════════════════════════════════════════════════
# MESSAGE HANDLER
# ══════════════════════════════════════════════════════
def _update(actual_gran:int,rows:list):
    rev={v:k for k,v in state.gran_actual.items()}
    nom=rev.get(actual_gran,actual_gran)
    _store(nom,rows)

async def handle_msg(msg:dict):
    rid=msg.get("req_id")
    if rid and rid in state.pending_requests:
        fut=state.pending_requests.pop(rid)
        if not fut.done(): fut.set_result(msg)
        return
    mt=msg.get("msg_type","")
    if mt=="ohlc":
        c=msg["ohlc"]; gran=int(c.get("granularity",0))
        _update(gran,[(int(c["epoch"]),float(c["open"]),float(c["high"]),
                       float(c["low"]),float(c["close"]))])
    elif mt=="candles":
        gran=int(msg.get("echo_req",{}).get("granularity",0))
        rows=[(int(c["epoch"]),float(c["open"]),float(c["high"]),
               float(c["low"]),float(c["close"])) for c in msg.get("candles",[])]
        if rows: _update(gran,rows)
    elif mt=="tick":
        state.current_price=float(msg["tick"]["quote"])
    elif mt=="proposal_open_contract":
        poc=msg.get("proposal_open_contract",{})
        cid=str(poc.get("contract_id",""))
        if cid not in state.open_contracts: return
        profit=float(poc.get("profit",0)); status=poc.get("status","")
        exit_s=float(poc.get("exit_tick",state.current_price) or state.current_price)
        if status in("sold","expired"):
            info=state.open_contracts.pop(cid,{})
            if profit>0: state.wins+=1
            else:        state.losses+=1
            state.total_pnl+=profit; tnum=state.trade_count
            state.trade_history.append({
                "num":tnum,"id":cid,"pair":state.pair_display,
                "direction":info.get("direction","?"),
                "entry":info.get("entry",0),"exit":exit_s,
                "pnl":round(profit,2),"win":profit>0,
                "ts":datetime.now(timezone.utc).strftime("%m/%d %H:%M")})
            if len(state.trade_history)>50: state.trade_history.pop(0)
            chart=generate_chart(state.m15_candles,"M15",
                entry_price=info.get("entry"),exit_price=exit_s,
                direction=info.get("direction"),pnl=profit,chart_type="exit")
            s="+" if profit>0 else ""
            # ── Execution-only alert (no noise) ──
            asyncio.ensure_future(tg_async(
                f"{'✅ WIN' if profit>0 else '❌ LOSS'}  `#{tnum}`\n\n"
                f"Pair:`{state.pair_display}`  Dir:`{info.get('direction','?')}`\n"
                f"Entry:`{info.get('entry',0):.5f}`  Exit:`{exit_s:.5f}`\n"
                f"P&L:`{s}{profit:.2f} {state.account_currency}`\n"
                f"W/L:{state.wins}/{state.losses}  "
                f"Total:{state.total_pnl:+.2f}  Bal:{state.account_balance:.2f}",
                photo_path=chart))
    elif mt=="balance":
        state.account_balance =msg["balance"]["balance"]
        state.account_currency=msg["balance"]["currency"]
    elif "error" in msg:
        log.warning(f"API: {msg['error'].get('message','?')}")

# ══════════════════════════════════════════════════════
# AUTO CHART BROADCAST
# ══════════════════════════════════════════════════════
async def chart_broadcast_loop():
    await asyncio.sleep(50)
    while state.running:
        try:
            if state.current_price>0 and len(state.m15_candles)>=20:
                path=generate_chart(state.m15_candles,"M15",chart_type="live")
                if path:
                    be="📈" if state.trend_bias=="BULLISH" else"📉" if state.trend_bias=="BEARISH" else"➡️"
                    pe="🟢" if state.premium_discount=="DISCOUNT" else"🔴" if state.premium_discount=="PREMIUM" else"⚪"
                    bl="  🚫 *NEWS BLOCK*" if state.block_trading else""
                    await tg_async(
                        f"{be} *{state.pair_display}  M15*{bl}\n"
                        f"Price:`{state.current_price:.5f}`  Bias:`{state.trend_bias}`\n"
                        f"Zone:{pe}`{state.premium_discount}`  OB:`{state.ob_score}/100`\n"
                        f"Session:`{state.session_bias}`  ATR:`{'OK' if state.atr_filter_ok else 'LOW'}`\n"
                        f"H1:`{len(state.h1_candles)}` M15:`{len(state.m15_candles)}` M5:`{len(state.m5_candles)}`",
                        photo_path=path)
        except Exception as e:
            log.error(f"chart_broadcast: {e}")
        await asyncio.sleep(CHART_INTERVAL)

# ══════════════════════════════════════════════════════
# TRADING LOOP  (execution-only alerts — no noise)
# ══════════════════════════════════════════════════════
async def trading_loop():
    await asyncio.sleep(35)
    last_sig=0; COOLDOWN=300

    while state.running:
        try:
            # Silence: skip without any message
            if state.paused or state.block_trading or state.current_price==0:
                await asyncio.sleep(30); continue

            check_trade_mgmt()

            if time.time()-last_sig<COOLDOWN:
                await asyncio.sleep(30); continue

            sig=compute_signal("M15")
            if sig:
                sig5=compute_signal("M5")
                if sig5 and sig5["direction"]==sig["direction"]:
                    log.info(f"🎯 {sig['direction']} Entry:{sig['entry']} Sc:{sig['ob_score']}")
                    chart=generate_chart(state.m15_candles,"M15",
                        entry_price=sig["entry"],direction=sig["direction"],
                        chart_type="entry")
                    # ── Execution alert ONLY ──
                    await tg_async(
                        f"🎯 *TRADE EXECUTED — {state.pair_display}*\n\n"
                        f"Dir:`{sig['direction']}`  Entry:`{sig['entry']}`\n"
                        f"SL:`{sig['sl']}`\n"
                        f"TP1({state.tp1_r}R):`{sig['tp1']}`\n"
                        f"TP2({state.tp2_r}R):`{sig['tp2']}`\n"
                        f"TP3({state.tp3_r}R):`{sig['tp3']}`\n\n"
                        f"*Logic:* `{sig['logic']}`\n"
                        f"Stake:`{sig['stake']:.2f} {state.account_currency}`"
                        f"{'  💎' if state.small_acc_mode else ''}",
                        photo_path=chart)
                    if state.account_balance>0:
                        cid=await open_contract(sig["direction"],sig["stake"])
                        if cid: last_sig=time.time()
        except Exception as e:
            log.error(f"trading_loop: {e}\n{traceback.format_exc()}")
        await asyncio.sleep(30)

# ══════════════════════════════════════════════════════
# TELEGRAM POLL
# ══════════════════════════════════════════════════════
async def tg_poll_loop():
    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN not set."); return
    base=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    loop=asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None,lambda: requests.post(
            f"{base}/deleteWebhook",json={"drop_pending_updates":True},timeout=10))
        log.info("Webhook cleared.")
    except Exception as e:
        log.warning(f"Webhook: {e}")
    await asyncio.sleep(2)
    offset=0; ec=0
    while state.running:
        try:
            r=await loop.run_in_executor(None,lambda: requests.get(
                f"{base}/getUpdates",
                params={"offset":offset,"timeout":20,
                        "allowed_updates":["message","callback_query"]},
                timeout=25))
            data=r.json()
            if not data.get("ok"):
                desc=data.get("description","")
                log.error(f"TG: {desc}")
                if "Conflict" in desc:
                    await asyncio.sleep(30)
                    await loop.run_in_executor(None,lambda: requests.post(
                        f"{base}/deleteWebhook",json={"drop_pending_updates":True},timeout=10))
                else: await asyncio.sleep(10)
                ec+=1; continue
            ec=0
            for upd in data.get("result",[]):
                offset=upd["update_id"]+1
                await _handle_upd(upd)
        except Exception as e:
            ec+=1; log.error(f"TG poll: {type(e).__name__}: {e}")
            await asyncio.sleep(min(5*ec,60))
        await asyncio.sleep(.5)

async def _handle_upd(upd:dict):
    if "message" in upd:
        text=upd["message"].get("text","").strip()
        cid=str(upd["message"]["chat"]["id"])
        if TELEGRAM_CHAT_ID and cid!=TELEGRAM_CHAT_ID: return
        await _cmd(text)
    elif "callback_query" in upd:
        cq=upd["callback_query"]; data=cq.get("data","")
        cqid=cq["id"]; cid=str(cq["message"]["chat"]["id"])
        if TELEGRAM_CHAT_ID and cid!=TELEGRAM_CHAT_ID:
            tg_answer(cqid,"Unauthorized"); return
        tg_answer(cqid)
        await _cmd(data)

async def _cmd(cmd:str):
    cmd=cmd.lower().strip()

    if cmd in("/start","/help","cmd_back"):
        bl="🚫 BLOCKED" if state.block_trading else("🛑 PAUSED" if state.paused else "🟢 ACTIVE")
        await tg_async(
            f"🤖 *SMC ELITE EA v4*\n\n"
            f"Pair:`{state.pair_display}`  sym:`{state.active_symbol}`\n"
            f"Risk:`{state.risk_pct*100:.0f}%`  "
            f"Mode:`{'💎SMALL' if state.small_acc_mode else 'STD'}`\n"
            f"Status:{bl}\n"
            f"Bal:`{state.account_balance:.2f} {state.account_currency}`\n"
            f"H1:`{len(state.h1_candles)}` M15:`{len(state.m15_candles)}` M5:`{len(state.m5_candles)}`",
            reply_markup=kb_main())

    elif cmd in("/balance","cmd_balance"):
        if state.ws:
            try: await get_balance()
            except Exception: pass
        wr=f"{state.wins/(state.wins+state.losses)*100:.1f}%" if(state.wins+state.losses)>0 else "N/A"
        await tg_async(
            f"💰 *Balance*\n`{state.account_balance:.2f} {state.account_currency}`\n\n"
            f"Trades:`{state.trade_count}`  W/L:`{state.wins}/{state.losses}`  WR:`{wr}`\n"
            f"Total P&L:`{state.total_pnl:+.2f}`",reply_markup=kb_main())

    elif cmd in("/chart","cmd_chart"):
        m15=len(state.m15_candles)
        if m15>=20:
            path=generate_chart(state.m15_candles,"M15",chart_type="live")
            if path:
                pe="🟢" if state.premium_discount=="DISCOUNT" else"🔴" if state.premium_discount=="PREMIUM" else"⚪"
                bl="  🚫NEWS BLOCK" if state.block_trading else""
                await tg_async(
                    f"📊 *{state.pair_display}  M15*{bl}\n"
                    f"Price:`{state.current_price:.5f}`  Bias:`{state.trend_bias}`\n"
                    f"Zone:{pe}`{state.premium_discount}`  OB:`{state.ob_score}/100`\n"
                    f"Session:`{state.session_bias}`  ATR:`{'OK' if state.atr_filter_ok else 'LOW'}`\n"
                    f"IDM:{'✅' if state.active_idm and state.active_idm.get('swept') else '⏳'}  "
                    f"Trap:{'✅' if state.active_trap and state.active_trap.get('swept') else '⏳'}",
                    photo_path=path,reply_markup=kb_main())
                return
        gran_used=state.gran_actual.get(900,900)
        await tg_async(
            f"⚠️ *Chart not ready*\n\n"
            f"H1:`{len(state.h1_candles)}`  M15:`{m15}` (needs 20+)  M5:`{len(state.m5_candles)}`\n"
            f"sym:`{state.active_symbol}`  M15 gran:`{gran_used}s`\n"
            f"WS:`{'connected' if state.ws else 'disconnected'}`  Price:`{state.current_price}`\n"
            f"_Retrying automatically..._",reply_markup=kb_main())

    elif cmd in("/news","cmd_news"):
        # Refresh if stale (>1h)
        if time.time()-state.news_last_fetch > 3600 or not state.news_events:
            await tg_async("⏳ Fetching news from Forex Factory...",reply_markup=kb_main())
            loop=asyncio.get_event_loop()
            events=await loop.run_in_executor(None,fetch_news)
            state.news_events=events
            state.news_last_fetch=time.time()
            state.next_red_event=_next_red_event()
            path=await loop.run_in_executor(None,generate_news_chart)
            state.news_chart_path=path
        summary=_amharic_summary()
        nxt=state.next_red_event
        nxt_info=""
        if nxt and nxt.dt_utc:
            ny_t=nxt.dt_utc.astimezone(NY_TZ).strftime("%I:%M%p ET")
            mins=int((nxt.dt_utc-datetime.now(timezone.utc)).total_seconds()//60)
            nxt_info=f"\n\n🔴 Next Red: *{nxt.title}* @ `{ny_t}` (~{mins}m)"
        bl_s,bl_r=_news_block_status()
        block_info=f"\n\n{bl_r}" if bl_s else "\n\n✅ Trading not blocked right now."
        await tg_async(
            f"📰 *Economic Calendar — Today*\n\n"
            f"{summary}"
            f"{nxt_info}"
            f"{block_info}",
            photo_path=state.news_chart_path,reply_markup=kb_main())

    elif cmd in("/history","cmd_history"):
        if not state.trade_history:
            await tg_async("📋 No history yet.",reply_markup=kb_main()); return
        path=generate_history_chart()
        lines=[f"📋 *History — last 10*\n"]
        for t in state.trade_history[-10:]:
            s="+" if t["pnl"]>0 else ""
            lines.append(f"{'✅' if t['win'] else '❌'} `#{t['num']}` {t['direction']} "
                         f"`{t['entry']:.5f}`→`{t['exit']:.5f}` `{s}{t['pnl']:.2f}` _{t['ts']}_")
        wr=f"{state.wins/(state.wins+state.losses)*100:.1f}%" if(state.wins+state.losses)>0 else "N/A"
        lines.append(f"\nP&L:`{state.total_pnl:+.2f}`  WR:`{wr}`")
        await tg_async("\n".join(lines),photo_path=path,reply_markup=kb_main())

    elif cmd in("/status","cmd_status"):
        bl="🚫 "+state.block_reason if state.block_trading else("🛑 PAUSED" if state.paused else "🟢 SCANNING")
        sig_info=""
        if state.last_signal:
            s=state.last_signal
            sig_info=(f"\n\n*Last Signal*\n`{s['direction']}` @ `{s['entry']}`\n"
                      f"SL:`{s['sl']}` TP1:`{s['tp1']}`\n`{s['logic'][:60]}`")
        nxt=state.next_red_event
        news_s=f"\n🔴 Next: {nxt.title[:25]} @ {nxt.dt_utc.astimezone(NY_TZ).strftime('%H:%Mh ET')}" if nxt and nxt.dt_utc else ""
        await tg_async(
            f"⚡ *Status*\n"
            f"Mode:{bl}\n"
            f"Pair:`{state.pair_display}`  Price:`{state.current_price:.5f}`\n"
            f"Bias:`{state.trend_bias}`  Zone:`{state.premium_discount}`\n"
            f"Session:`{state.session_bias}`  ATR:`{'OK' if state.atr_filter_ok else 'LOW'}`\n"
            f"H1:`{len(state.h1_candles)}` M15:`{len(state.m15_candles)}` M5:`{len(state.m5_candles)}`\n"
            f"Open:`{len(state.open_contracts)}`  Bal:`{state.account_balance:.2f}`"
            f"{news_s}{sig_info}",reply_markup=kb_main())

    elif cmd in("/settings","cmd_settings"):
        await tg_async(
            f"⚙️ *Settings*\n"
            f"Pair:`{state.pair_display}`  sym:`{state.active_symbol}`\n"
            f"Risk:`{state.risk_pct*100:.0f}%`  TPs:`{state.tp1_r}R/{state.tp2_r}R/{state.tp3_r}R`\n"
            f"Small Acc:`{'ON 💎' if state.small_acc_mode else 'OFF'}`",
            reply_markup=kb_settings())

    elif cmd=="cmd_pair_menu":
        await tg_async("💱 *Select Pair:*",reply_markup=kb_pair_menu())

    elif cmd.startswith("cmd_pair_"):
        key=cmd.replace("cmd_pair_","").upper()
        if key in PAIR_REGISTRY:
            old=state.pair_key; state.pair_key=key; state.small_acc_mode=False
            if state.ws:
                try:
                    await tg_async(f"⏳ Switching to `{PAIR_REGISTRY[key][4]}`...",reply_markup=kb_main())
                    h,m,f=await subscribe_pair(key)
                    await tg_async(f"💱 Switched to `{state.pair_display}`\n"
                                   f"H1:`{h}` M15:`{m}` M5:`{f}` ✅",reply_markup=kb_main())
                except Exception as e:
                    state.pair_key=old
                    await tg_async(f"❌ Switch failed: {e}",reply_markup=kb_main())
            else:
                await tg_async(f"💱 Pair → `{state.pair_display}` (next connect)",reply_markup=kb_main())

    elif cmd=="cmd_small_acc":
        if state.small_acc_mode:
            state.small_acc_mode=False; state.risk_pct=0.01
            state.tp1_r,state.tp2_r,state.tp3_r=2.,4.,6.
            await tg_async("💎 Small Acc *OFF* — 1% risk",reply_markup=kb_settings())
        else:
            state.small_acc_mode=True
            if state.pair_key=="XAUUSD":
                state.risk_pct=0.02; state.tp1_r,state.tp2_r,state.tp3_r=1.5,3.,5.
                note="XAU/USD 2% risk"
            else:
                state.pair_key="GBPUSD"; state.risk_pct=0.05
                state.tp1_r,state.tp2_r,state.tp3_r=1.5,3.,4.5
                note="GBP/USD 5% risk"
            if state.ws:
                try: await subscribe_pair(state.pair_key)
                except Exception: pass
            await tg_async(f"💎 *Small Acc ON*\n{note}",reply_markup=kb_settings())

    elif cmd=="cmd_risk_1": state.risk_pct=0.01; state.small_acc_mode=False; await tg_async("✅ Risk→1%",reply_markup=kb_settings())
    elif cmd=="cmd_risk_3": state.risk_pct=0.03; state.small_acc_mode=False; await tg_async("✅ Risk→3%",reply_markup=kb_settings())
    elif cmd=="cmd_risk_5": state.risk_pct=0.05; state.small_acc_mode=False; await tg_async("✅ Risk→5%",reply_markup=kb_settings())

    elif cmd in("/close_all","cmd_stop"):
        state.paused=True; n=await close_all()
        await tg_async(f"🛑 *Emergency Stop*\nClosed `{n}` contracts. Bot *PAUSED*.",reply_markup=kb_main())

    elif cmd in("/resume","cmd_resume"):
        state.paused=False; state.block_trading=False
        await tg_async("▶️ Bot *RESUMED* — scanning.",reply_markup=kb_main())

# ══════════════════════════════════════════════════════
# WS ENGINE
# ══════════════════════════════════════════════════════
async def ws_reader(ws):
    async for raw in ws:
        try: await handle_msg(json.loads(raw))
        except Exception as e: log.error(f"Handler: {type(e).__name__}: {e}")

async def ws_run(ws):
    state.ws=ws
    async def setup():
        await asyncio.sleep(0.1)
        log.info("Authorizing...")
        await authorize(); await get_balance()
        log.info(f"Bal:{state.account_balance} {state.account_currency}")
        h,m,f=await subscribe_pair(state.pair_key)
        await tg_async(
            f"🤖 *SMC ELITE EA v4 Online*\n"
            f"Pair:`{state.pair_display}`  sym:`{state.active_symbol}`\n"
            f"Bal:`{state.account_balance:.2f} {state.account_currency}`\n"
            f"Risk:`{state.risk_pct*100:.0f}%`  Mode:`{'💎SMALL' if state.small_acc_mode else 'STD'}`\n"
            f"Bars H1:`{h}` M15:`{m}` M5:`{f}` ✅\n"
            f"_Fetching news..._ 📰",reply_markup=kb_main())
    task=asyncio.ensure_future(setup())
    try: await ws_reader(ws)
    finally:
        task.cancel()
        try: await task
        except (asyncio.CancelledError,Exception): pass

async def ws_loop():
    delay=5
    while state.running:
        try:
            log.info(f"Connecting: {DERIV_WS_BASE}")
            async with websockets.connect(
                DERIV_WS_BASE,ping_interval=25,ping_timeout=10,
                close_timeout=10,open_timeout=15) as ws:
                delay=5; await ws_run(ws)
        except websockets.ConnectionClosed as e:
            log.warning(f"WS closed: {e}. Retry {delay}s")
        except asyncio.TimeoutError as e:
            log.error(f"WS timeout: {e}")
        except Exception as e:
            log.error(f"WS error: {type(e).__name__}: {e}")
            log.error(traceback.format_exc())
        finally:
            state.ws=None
            for fut in state.pending_requests.values():
                if not fut.done(): fut.cancel()
            state.pending_requests.clear()
        await asyncio.sleep(delay); delay=min(delay*2,60)

# ══════════════════════════════════════════════════════
# HEALTH SERVER
# ══════════════════════════════════════════════════════
async def health(req):
    wr=f"{state.wins/(state.wins+state.losses)*100:.1f}" if(state.wins+state.losses)>0 else "0"
    return web.json_response({
        "status":"running" if state.running else "stopped",
        "paused":state.paused,"block_trading":state.block_trading,
        "block_reason":state.block_reason,
        "pair":state.pair_key,"symbol":state.active_symbol,
        "price":state.current_price,"trend":state.trend_bias,
        "zone":state.premium_discount,"session":state.session_bias,
        "ob_score":state.ob_score,"atr_ok":state.atr_filter_ok,
        "balance":state.account_balance,"risk_pct":state.risk_pct,
        "small_acc":state.small_acc_mode,
        "trades":state.trade_count,"wins":state.wins,"losses":state.losses,
        "winrate":wr,"total_pnl":state.total_pnl,
        "open":len(state.open_contracts),"history":len(state.trade_history),
        "h1":len(state.h1_candles),"m15":len(state.m15_candles),"m5":len(state.m5_candles),
        "news_events":len(state.news_events),
        "next_red":state.next_red_event.title if state.next_red_event else None,
        "gran_actual":state.gran_actual,
    })

async def start_health():
    app=web.Application()
    app.router.add_get("/",health); app.router.add_get("/health",health)
    runner=web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner,"0.0.0.0",PORT).start()
    log.info(f"Health :{PORT}")

# ══════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════
async def main():
    log.info("╔══════════════════════════════════════════╗")
    log.info("║  SMC ELITE EA v4 · News-Aware Forex Bot  ║")
    log.info("╚══════════════════════════════════════════╝")
    if not DERIV_API_TOKEN:
        log.error("DERIV_API_TOKEN not set. Exiting."); return
    await asyncio.gather(
        start_health(),
        ws_loop(),
        trading_loop(),
        tg_poll_loop(),
        chart_broadcast_loop(),
        news_refresh_loop(),
        news_block_monitor(),
    )

if __name__=="__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down…"); state.running=False
