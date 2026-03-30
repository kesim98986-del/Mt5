"""
╔══════════════════════════════════════════════════════════════════════╗
║  SMC SNIPER EA  v5.1  —  Small Account Growth Edition              ║
║  $10→$100 Optimizer | Spread Guard | Partial Close | Deep OB Scan  ║
║  Dynamic News Shield | Visual News Table | Structural Trailing Stop ║
║  WS Heartbeat & State Recovery | Multi-Pair | Railway-Ready         ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import logging
import os
import re
import time
import traceback
from collections import deque
from datetime    import datetime, timedelta, timezone
from pathlib     import Path
from typing      import Dict, List, Optional, Tuple
from zoneinfo    import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.gridspec  as gridspec
import matplotlib.patches   as mpatches
import matplotlib.pyplot    as plt
import numpy  as np
import pandas as pd
import requests
import websockets
from aiohttp import web
from bs4     import BeautifulSoup

# ══════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(message)s",
    datefmt= "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("SNIPER-v51")

# ══════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════
NY_TZ = ZoneInfo("America/New_York")
UTC   = timezone.utc

# ── Pair registry  key:(primary, otc, pip_val, min_stake, display, category) ──
PAIR_REGISTRY: Dict[str, tuple] = {
    "XAUUSD": ("frxXAUUSD","OTC_XAUUSD", 0.01,   1.0, "XAU/USD 🥇", "METAL"),
    "EURUSD": ("frxEURUSD","OTC_EURUSD", 0.0001, 1.0, "EUR/USD 🇪🇺", "FOREX"),
    "GBPUSD": ("frxGBPUSD","OTC_GBPUSD", 0.0001, 1.0, "GBP/USD 🇬🇧", "FOREX"),
    "US100":  ("frxUS100", "OTC_NDX",    0.1,    1.0, "NASDAQ 💻",   "INDEX"),
}

GRAN_FALLBACKS = {
    3600: [3600, 7200],
    900:  [900, 600, 1800],
    300:  [300, 180, 600],
}

# ── Spread limits per pair (in price units) ──
MAX_SPREAD: Dict[str, float] = {
    "XAUUSD": 0.50,   # $0.50 for Gold
    "EURUSD": 0.0002, # 2 pips
    "GBPUSD": 0.0003, # 3 pips
    "US100":  2.0,    # 2 index points
}

# ── ATR minimums ──
ATR_MIN: Dict[str, float] = {
    "XAUUSD": 0.40, "EURUSD": 0.0004, "GBPUSD": 0.0005, "US100": 4.0,
}

TOKEN_FILE       = Path("/tmp/.deriv_token")
PORT             = int(os.getenv("PORT",           "8080"))
CHART_INTERVAL   = int(os.getenv("CHART_INTERVAL", "300"))
MIN_SCORE        = int(os.getenv("MIN_SCORE",      "75"))
DERIV_APP_ID     = os.getenv("DERIV_APP_ID",  "1089")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",  "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID","")
DERIV_WS_BASE    = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"

# ── News timing defaults (can be extended dynamically) ──
PRE_NEWS_BLOCK_DEFAULT  = 30 * 60
POST_NEWS_WAIT_DEFAULT  = 15 * 60
NEWS_INTERVAL           = 12 * 3600

# ══════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ══════════════════════════════════════════════════════════════════════
class NewsEvent:
    __slots__ = ("time_et","currency","impact","title","actual","forecast","prev","dt_utc","volatility_score")
    def __init__(self, time_et, currency, impact, title, actual="", forecast="", prev=""):
        self.time_et        = time_et
        self.currency       = currency
        self.impact         = impact
        self.title          = title
        self.actual         = actual
        self.forecast       = forecast
        self.prev           = prev
        self.dt_utc         = None
        self.volatility_score = 0   # 0-3; higher = extend block time
    @property
    def is_red(self):    return self.impact == "high"
    @property
    def is_orange(self): return self.impact == "medium"
    def dynamic_post_wait(self) -> int:
        """Return post-news wait in seconds based on forecast vs previous gap."""
        base = POST_NEWS_WAIT_DEFAULT
        return base + self.volatility_score * 5 * 60   # +5m per volatility point


class TradeReason:
    def __init__(self):
        self.h1_trend=""; self.structure=""; self.pd_zone=""
        self.idm_sweep=""; self.trap_sweep=""; self.ob_type=""
        self.fvg_present=False; self.session=""
        self.atr_state=""; self.ema_confirm=""; self.rsi_level=0.
        self.score=0; self.candle_conf=""; self.entry_logic=""

    def build_report(self, direction:str)->str:
        arrow="📈 BUY" if direction=="BUY" else"📉 SELL"
        return "\n".join([
            f"🧠 *Sniper Brain — Autonomous Trade Report*",
            f"Direction: *{arrow}*  Score: `{self.score}/100`",
            "",f"*Multi-Timeframe:*",
            f"• H1 Trend:  `{self.h1_trend}`",
            f"• Structure: `{self.structure}`",
            f"• Zone:      `{self.pd_zone}`",
            "",f"*Liquidity Intel:*",
            f"• IDM Sweep: `{self.idm_sweep}`",
            f"• Trap:      `{self.trap_sweep}`",
            f"• OB:        `{self.ob_type}`",
            f"• FVG:       `{'✅ Present' if self.fvg_present else '⚠️ None'}`",
            "",f"*Filters:*",
            f"• Session: `{self.session}`",
            f"• ATR: `{self.atr_state}`",
            f"• EMA50: `{self.ema_confirm}`",
            f"• RSI: `{self.rsi_level:.1f}`",
            f"• Candle: `{self.candle_conf}`",
            "",f"*Entry Logic:* `{self.entry_logic}`",
        ])

    def build_amharic(self, direction:str)->str:
        arrow="ወደ ላይ (BUY)" if direction=="BUY" else"ወደ ታች (SELL)"
        return (f"🤖 *ቦቱ ዝርዝር ምክንያት (v5.1)*\n"
                f"አቅጣጫ: `{arrow}`  ነጥብ: `{self.score}/100`\n"
                f"• H1 ዝንባሌ: `{self.h1_trend}`\n"
                f"• IDM: `{self.idm_sweep}`\n"
                f"• ወጥመድ: `{self.trap_sweep}`\n"
                f"• OB: `{self.ob_type}`\n"
                f"• ዞን: `{self.pd_zone}`  ሰሽን: `{self.session}`\n"
                f"• ምክንያት: `{self.entry_logic}`")


# ══════════════════════════════════════════════════════════════════════
# GLOBAL STATE
# ══════════════════════════════════════════════════════════════════════
class BotState:
    def __init__(self):
        # Broker
        self.deriv_token      = os.getenv("DERIV_API_TOKEN","")
        self.broker_connected = False
        self.account_type     = "unknown"
        self.account_id       = ""
        self.account_balance  = 0.0
        self.account_currency = "USD"
        self.awaiting_token   = False
        self.current_spread   = 0.0   # live spread from broker

        # Bot control
        self.running          = True
        self.paused           = False
        self.autonomous       = True
        self.block_trading    = False
        self.block_reason     = ""
        self.pre_news_block   = PRE_NEWS_BLOCK_DEFAULT
        self.post_news_wait   = POST_NEWS_WAIT_DEFAULT

        # Pair
        self.pair_key         = "XAUUSD"
        self.active_symbol    = ""
        self.risk_pct         = 0.01
        self.tp1_r            = 2.0
        self.tp2_r            = 4.0
        self.tp3_r            = 6.0
        self.small_acc_mode   = False

        # ── v5.1: deeper candle buffers for unmitigated OB detection ──
        self.h1_candles       = deque(maxlen=600)   # was 300
        self.m15_candles      = deque(maxlen=600)   # was 300
        self.m5_candles       = deque(maxlen=300)
        self.gran_actual      = {3600:3600, 900:900, 300:300}

        # Analysis
        self.current_price    = 0.0
        self.trend_bias       = "NEUTRAL"
        self.last_signal      = None
        self.active_ob        = None
        self.active_fvg       = None
        self.active_trap      = None
        self.active_idm       = None
        self.premium_discount = "NEUTRAL"
        self.ob_score         = 0
        self.session_now      = "UNKNOWN"
        self.atr_filter_ok    = True
        self.market_open      = True

        # WS
        self.ws               = None
        self.req_id           = 1
        self.pending_reqs     = {}
        self.subscribed_sym   = None
        self.last_ws_ping     = time.time()

        # Trades
        self.open_contracts   : Dict[str, dict] = {}
        self.trade_count      = 0
        self.wins             = 0
        self.losses           = 0
        self.total_pnl        = 0.0
        self.trade_history    : List[dict] = []
        self.last_trade_ts    = 0.0
        self.signal_cooldown  = 300

        # News
        self.news_events      : List[NewsEvent] = []
        self.news_last_fetch  = 0.0
        self.next_red_event   : Optional[NewsEvent] = None
        self.news_chart_path  : Optional[str] = None
        self.active_news_ev   : Optional[NewsEvent] = None  # currently in post-release window

    @property
    def pair_info(self):    return PAIR_REGISTRY[self.pair_key]
    @property
    def pair_display(self): return self.pair_info[4]
    @property
    def pair_category(self):return self.pair_info[5]
    @property
    def max_spread(self):   return MAX_SPREAD.get(self.pair_key, 0.001)

state = BotState()

def _load_saved_token():
    try:
        if TOKEN_FILE.exists():
            t = TOKEN_FILE.read_text().strip()
            if t: state.deriv_token=t; log.info("Loaded saved Deriv token")
    except Exception as e: log.warning(f"Token load: {e}")

# ══════════════════════════════════════════════════════════════════════
# MARKET HOURS
# ══════════════════════════════════════════════════════════════════════
def is_market_open() -> bool:
    now=datetime.now(UTC); wd=now.weekday(); h=now.hour
    if wd==4 and h>=21: return False
    if wd==5:           return False
    if wd==6 and h<22:  return False
    return True

def time_to_next_open() -> str:
    now=datetime.now(UTC); wd=now.weekday()
    days=(6-wd)%7
    if days==0 and now.hour>=22: days=7
    nxt=(now+timedelta(days=days)).replace(hour=22,minute=0,second=0,microsecond=0)
    d=nxt-now; h,rem=divmod(int(d.total_seconds()),3600); m=rem//60
    return f"{h}h {m}m"

def get_session() -> str:
    h=datetime.now(UTC).hour
    if  0<=h< 7: return "ASIA"
    if  7<=h<12: return "LONDON"
    if 12<=h<16: return "OVERLAP"
    if 12<=h<17: return "NY"
    return "CLOSE"

def market_header() -> str:
    if is_market_open():
        return f"🟢 Market is *OPEN*  ·  Session: `{get_session()}`"
    return f"🔴 Market is *CLOSED*  ·  Opens in `{time_to_next_open()}`"

# ══════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════════════════════════════════
def kb_main():
    bl=("🚫 Blocked" if state.block_trading
        else("🛑 Paused" if state.paused else"🟢 Active"))
    return {"inline_keyboard":[
        [{"text":"📊 Status",         "callback_data":"cmd_status"},
         {"text":"📰 News",           "callback_data":"cmd_news"}],
        [{"text":"📈 Chart",          "callback_data":"cmd_chart"},
         {"text":"📋 History",        "callback_data":"cmd_history"}],
        [{"text":"🔗 Connect Broker", "callback_data":"cmd_connect"},
         {"text":"⚙️ Settings",       "callback_data":"cmd_settings"}],
        [{"text":"💰 Balance",        "callback_data":"cmd_balance"},
         {"text":f"{bl}",             "callback_data":"cmd_toggle_pause"}],
        [{"text":"🛑 Emergency Stop", "callback_data":"cmd_stop"}],
    ]}

def kb_settings():
    r=state.risk_pct*100; sm="✅" if state.small_acc_mode else"○"
    return {"inline_keyboard":[
        [{"text":"💱 Select Pair",           "callback_data":"cmd_pair_menu"}],
        [{"text":f"{sm} 💎 Small Acc ($10)","callback_data":"cmd_small_acc"}],
        [{"text":f"{'✅' if r==1 else '○'} 1%","callback_data":"cmd_risk_1"},
         {"text":f"{'✅' if r==3 else '○'} 3%","callback_data":"cmd_risk_3"},
         {"text":f"{'✅' if r==5 else '○'} 5%","callback_data":"cmd_risk_5"}],
        [{"text":"⬅️ Back","callback_data":"cmd_back"}],
    ]}

def kb_pair_menu():
    rows,row=[],[]
    for key,info in PAIR_REGISTRY.items():
        tick="✅ " if key==state.pair_key else""
        row.append({"text":tick+info[4],"callback_data":f"cmd_pair_{key}"})
        if len(row)==2: rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([{"text":"⬅️ Back","callback_data":"cmd_settings"}])
    return {"inline_keyboard":rows}

def kb_connect():
    return {"inline_keyboard":[
        [{"text":"📋 How to get Token","callback_data":"cmd_token_help"}],
        [{"text":"⬅️ Cancel",          "callback_data":"cmd_back"}],
    ]}

# ══════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════
def tg_send(text:str, photo_path:str=None, reply_markup=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    base=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    mk=reply_markup if reply_markup is not None else kb_main()
    try:
        if photo_path:
            with open(photo_path,"rb") as fh:
                r=requests.post(f"{base}/sendPhoto",data={
                    "chat_id":TELEGRAM_CHAT_ID,"caption":text[:1024],
                    "reply_markup":json.dumps(mk),"parse_mode":"Markdown",
                },files={"photo":fh},timeout=20)
        else:
            r=requests.post(f"{base}/sendMessage",json={
                "chat_id":TELEGRAM_CHAT_ID,"text":text,
                "reply_markup":mk,"parse_mode":"Markdown",
            },timeout=10)
        if r.status_code not in(200,201): log.warning(f"TG {r.status_code}: {r.text[:100]}")
    except Exception as e: log.error(f"tg_send: {e}")

def tg_answer(cqid:str,text:str=""):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                      json={"callback_query_id":cqid,"text":text},timeout=5)
    except Exception: pass

async def tg_async(text:str, photo_path:str=None, reply_markup=None):
    loop=asyncio.get_event_loop()
    await loop.run_in_executor(None,lambda:tg_send(text,photo_path,reply_markup))

# ══════════════════════════════════════════════════════════════════════
# ── NEWS ENGINE v5.1 ─────────────────────────────────────────────────
#   • Volatility scoring (forecast vs previous gap)
#   • Dynamic block extension
#   • Visual styled table
#   • Post-release Telegram report
# ══════════════════════════════════════════════════════════════════════
FF_HDRS = {
    "User-Agent":("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"),
    "Accept-Language":"en-US,en;q=0.9",
}

def _parse_num(s:str)->Optional[float]:
    """Extract a float from strings like '225K', '0.3%', '-0.1'."""
    if not s: return None
    s=s.strip().replace(",","")
    m=re.search(r"[-+]?\d+\.?\d*",s)
    if not m: return None
    val=float(m.group())
    if "K" in s.upper(): val*=1000
    if "M" in s.upper(): val*=1e6
    if "B" in s.upper(): val*=1e9
    return val

def _volatility_score(ev:NewsEvent)->int:
    """
    Score 0-3 based on forecast vs previous gap.
    0 = small gap, 3 = very large gap → extend post-news wait.
    """
    f=_parse_num(ev.forecast); p=_parse_num(ev.prev)
    if f is None or p is None: return 1  # default medium
    if p==0: return 2
    gap_pct=abs(f-p)/max(abs(p),0.0001)*100
    if gap_pct<5:   return 0
    if gap_pct<15:  return 1
    if gap_pct<30:  return 2
    return 3

def _parse_ff_time(ts:str,base:datetime)->Optional[datetime]:
    ts=ts.strip().lower()
    if not ts or ts in("all day","tentative","","—"): return base.replace(hour=0,minute=0,second=0)
    try:
        t=datetime.strptime(ts,"%I:%M%p")
        return base.replace(hour=t.hour,minute=t.minute,second=0,tzinfo=NY_TZ).astimezone(UTC)
    except Exception: return None

def fetch_news()->List[NewsEvent]:
    events:List[NewsEvent]=[]
    today=datetime.now(NY_TZ)
    for offset in(0,1):
        target=today+timedelta(days=offset)
        url=f"https://www.forexfactory.com/calendar?day={target.strftime('%b%d.%Y').lower()}"
        try:
            resp=requests.get(url,headers=FF_HDRS,timeout=15)
            if resp.status_code!=200: continue
            soup=BeautifulSoup(resp.text,"html.parser")
            cur_time=""
            for row in soup.select("tr.calendar__row"):
                tc=row.select_one(".calendar__time")
                if tc:
                    t=tc.get_text(strip=True)
                    if t: cur_time=t
                cur_=row.select_one(".calendar__currency")
                currency=cur_.get_text(strip=True) if cur_ else""
                ic=row.select_one(".calendar__impact span")
                impact=""
                if ic:
                    cls=" ".join(ic.get("class",[]))
                    if "high"   in cls: impact="high"
                    elif "medium" in cls: impact="medium"
                    elif "low"  in cls: impact="low"
                ec_=row.select_one(".calendar__event-title")
                title=ec_.get_text(strip=True) if ec_ else""
                if not title or not currency: continue
                if currency not in{"USD","XAU"}: continue
                if impact not in{"high","medium"}: continue
                def _g(sel):
                    el=row.select_one(sel)
                    return el.get_text(strip=True) if el else""
                ev=NewsEvent(cur_time,currency,impact,title,_g(".calendar__actual"),
                             _g(".calendar__forecast"),_g(".calendar__previous"))
                ev.dt_utc=_parse_ff_time(cur_time,target)
                ev.volatility_score=_volatility_score(ev)
                events.append(ev)
        except Exception as e: log.error(f"FF scrape: {e}")
    events.sort(key=lambda e:e.dt_utc or datetime.min.replace(tzinfo=UTC))
    log.info(f"📰 {len(events)} events fetched")
    return events

def _next_red()->Optional[NewsEvent]:
    now=datetime.now(UTC)
    for ev in state.news_events:
        if ev.is_red and ev.dt_utc and ev.dt_utc>now: return ev
    return None

def _news_block()->Tuple[bool,str]:
    now=datetime.now(UTC)
    for ev in state.news_events:
        if not ev.is_red or not ev.dt_utc: continue
        until=(ev.dt_utc-now).total_seconds()
        after=(now-ev.dt_utc).total_seconds()
        pw=ev.dynamic_post_wait()
        if 0<until<=state.pre_news_block:
            return True,f"🚨 Red news in {int(until//60)}m: *{ev.title}*"
        if 0<after<=pw:
            remain=int((pw-after)//60)
            extra=f" (+{ev.volatility_score*5}m volatility)" if ev.volatility_score>0 else""
            return True,f"⏳ Post-news cooldown {remain}m left{extra} (*{ev.title}*)"
    return False,""

def _amharic_summary()->str:
    now=datetime.now(UTC)
    reds  =[e for e in state.news_events if e.is_red    and e.dt_utc and e.dt_utc>now]
    oranges=[e for e in state.news_events if e.is_orange and e.dt_utc and e.dt_utc>now]
    if not reds and not oranges:
        return("✅ *ደህንነቱ የተጠበቀ ቀን — Safe Day*\n"
               "ዛሬ ለወርቅ ትልቅ ዜና የለም።\n"
               "ቦቱ ያለ እገዳ ይሰራል።\n_ጥሩ ቀን ለትሬዲንግ!_")
    if reds:
        titles=", ".join(e.title[:28] for e in reds[:3])
        times=", ".join(e.dt_utc.astimezone(NY_TZ).strftime("%I:%M%p ET")
                        for e in reds[:3] if e.dt_utc)
        hi_vol=[e for e in reds if e.volatility_score>=2]
        vol_warn=f"\n⚡ ከፍተኛ ለውጥ ይጠበቃል: {', '.join(e.title[:20] for e in hi_vol)}" if hi_vol else""
        return(f"⚠️ *አደገኛ ቀን!*\n"
               f"ዜና: `{titles}`\nሰዓት: `{times}`{vol_warn}\n\n"
               f"📌 ቦቱ 30 ደቂቃ ቀደም ቆማ ይጠብቃል።\n"
               f"ዜናው ካለቀ {reds[0].dynamic_post_wait()//60} ደቂቃ ይጠብቃል።\n"
               f"_ወርቅን ዛሬ በጥንቃቄ ይንግዱ!_")
    return("🟡 *መካከለኛ ጥንቃቄ*\nዛሬ መካከለኛ ዜና አለ። ቦቱ ይሰራል።")

def _volatility_color(score:int)->str:
    return {0:"#2d4a2d",1:"#2d3a00",2:"#4a2d00",3:"#4a0000"}.get(score,"#161b22")

def generate_news_chart()->Optional[str]:
    evs=state.news_events
    if not evs: return None
    now_ny=datetime.now(UTC).astimezone(NY_TZ)

    rows_data=[]
    for ev in evs[:20]:
        t=ev.dt_utc.astimezone(NY_TZ).strftime("%I:%M%p") if ev.dt_utc else ev.time_et
        vs="⚡"*ev.volatility_score if ev.volatility_score else"—"
        rows_data.append([
            t, ev.currency,
            "🔴 HIGH" if ev.is_red else"🟠 MED",
            ev.title[:38], ev.forecast or"—", ev.prev or"—", vs])

    cols  =["Time ET","Curr","Impact","Event","Forecast","Previous","Vol"]
    widths=[0.09,0.05,0.07,0.38,0.10,0.10,0.06]
    fig,ax=plt.subplots(figsize=(15,max(4,len(rows_data)*0.44+2.0)),facecolor="#0d1117")
    ax.axis("off")
    tbl=ax.table(cellText=rows_data,colLabels=cols,cellLoc="left",loc="center",colWidths=widths)
    tbl.auto_set_font_size(False); tbl.set_fontsize(8); tbl.scale(1,1.6)

    # Header
    for j in range(len(cols)):
        tbl[0,j].set_facecolor("#1f2937")
        tbl[0,j].set_text_props(color="#cdd9e5",fontweight="bold",fontfamily="monospace")

    # Rows — color coded by impact + volatility
    for i,ev in enumerate(evs[:20]):
        rc=_volatility_color(ev.volatility_score) if ev.is_red else("#2d1a00" if ev.is_orange else"#161b22")
        tc=("#ff6b6b" if ev.is_red else("#ffa94d" if ev.is_orange else"#90a4ae"))
        for j in range(len(cols)):
            tbl[i+1,j].set_facecolor(rc)
            tbl[i+1,j].set_text_props(color=tc,fontfamily="monospace")

    nxt=state.next_red_event
    nxt_s=(f" | Next🔴: {nxt.title[:20]}@{nxt.dt_utc.astimezone(NY_TZ).strftime('%I:%M%p ET')}"
           f" vol:{'⚡'*nxt.volatility_score}" if nxt and nxt.dt_utc else"")
    ax.set_title(
        f"📰 Forex Factory — USD & XAU  ·  {now_ny.strftime('%A %b %d %Y  %I:%M%p ET')}{nxt_s}",
        color="#cdd9e5",fontsize=9,fontfamily="monospace",pad=12)
    path="/tmp/sniper_news51.png"
    plt.tight_layout()
    plt.savefig(path,dpi=120,bbox_inches="tight",facecolor="#0d1117")
    plt.close(fig); return path

async def news_refresh_loop():
    while state.running:
        try:
            loop=asyncio.get_event_loop()
            evs=await loop.run_in_executor(None,fetch_news)
            state.news_events=evs; state.news_last_fetch=time.time()
            state.next_red_event=_next_red()

            # Compute dynamic block times from highest-volatility event
            if evs:
                max_vs=max((e.volatility_score for e in evs if e.is_red),default=0)
                state.pre_news_block  = PRE_NEWS_BLOCK_DEFAULT + max_vs*5*60
                state.post_news_wait  = POST_NEWS_WAIT_DEFAULT + max_vs*5*60
                log.info(f"Dynamic block: pre={state.pre_news_block//60}m post={state.post_news_wait//60}m")

            path=await loop.run_in_executor(None,generate_news_chart)
            state.news_chart_path=path

            reds=[e for e in evs if e.is_red]
            if reds:
                nxt=state.next_red_event
                ni=(f"\nNext🔴: *{nxt.title}* @ `{nxt.dt_utc.astimezone(NY_TZ).strftime('%I:%M%p ET')}`"
                    f"  Vol: `{'⚡'*nxt.volatility_score}`" if nxt and nxt.dt_utc else"")
                await tg_async(
                    f"{market_header()}\n\n"
                    f"📰 *News Refresh — {len(reds)} Red Events*{ni}\n"
                    f"Pre-block: `{state.pre_news_block//60}m`  "
                    f"Post-wait: `{state.post_news_wait//60}m`\n\n"
                    f"{_amharic_summary()}",photo_path=path)
        except Exception as e: log.error(f"news_refresh_loop: {e}")
        await asyncio.sleep(NEWS_INTERVAL)

async def news_block_monitor():
    """Enforce trading blocks + send post-release report when actual value arrives."""
    await asyncio.sleep(30)
    reported_events=set()   # track which events we've sent post-release for
    while state.running:
        try:
            block,reason=_news_block()
            if block and not state.block_trading:
                state.block_trading=True; state.block_reason=reason
                log.info(f"🚫 BLOCKED: {reason}")
                await tg_async(f"{market_header()}\n\n🚫 *News Shield Active*\n{reason}\n"
                               f"_Bot auto-resumes._",reply_markup=kb_main())
            elif not block and state.block_trading:
                state.block_trading=False; state.block_reason=""
                log.info("✅ UNBLOCKED")
                await tg_async(f"{market_header()}\n\n✅ *Trading Resumed*\n"
                               f"News window cleared. Sniper Brain reactivated.",reply_markup=kb_main())

            # Post-release report: when a red event just passed and has actual value
            now=datetime.now(UTC)
            for ev in state.news_events:
                if not ev.is_red or not ev.dt_utc: continue
                ev_id=f"{ev.title}_{ev.dt_utc.isoformat()}"
                if ev_id in reported_events: continue
                secs_after=(now-ev.dt_utc).total_seconds()
                if 0<secs_after<=120 and ev.actual:   # within 2min of release with actual
                    reported_events.add(ev_id)
                    pw=ev.dynamic_post_wait()//60
                    vol_note=(f"\n⚡ *High Volatility Expected* (score:{ev.volatility_score}/3)" 
                              if ev.volatility_score>=2 else"")
                    await tg_async(
                        f"📊 *News Release Report*\n\n"
                        f"Event: *{ev.title}*\n"
                        f"Currency: `{ev.currency}`\n"
                        f"Actual:   `{ev.actual}`\n"
                        f"Forecast: `{ev.forecast or 'N/A'}`\n"
                        f"Previous: `{ev.prev or 'N/A'}`\n"
                        f"{vol_note}\n\n"
                        f"⏳ Trading will resume in `{pw}` minutes.",reply_markup=kb_main())
        except Exception as e: log.error(f"news_block_monitor: {e}")
        await asyncio.sleep(60)

# ══════════════════════════════════════════════════════════════════════
# CHART ENGINE
# ══════════════════════════════════════════════════════════════════════
BG="#0d1117"; PB="#161b22"; GR="#1e2a38"
BC="#00e676"; RC="#ff1744"; OBB="#00bcd4"; OBR="#ff9800"
FC="#ce93d8"; TC="#ffeb3b"; IC="#80cbc4"; EC="#2979ff"; SC="#f44336"
TPC=["#69f0ae","#40c4ff","#b388ff"]

def _ax_s(ax):
    ax.set_facecolor(PB); ax.tick_params(colors="#90a4ae",labelsize=7)
    for s in ax.spines.values(): s.set_edgecolor(GR)
    ax.grid(axis="y",color=GR,linewidth=0.4,alpha=0.6)

def _rsi(p:np.ndarray,n:int=14)->np.ndarray:
    if len(p)<n+1: return np.full(len(p),50.)
    d=np.diff(p); g=np.where(d>0,d,0.); l=np.where(d<0,-d,0.)
    ag=np.convolve(g,np.ones(n)/n,"valid"); al=np.convolve(l,np.ones(n)/n,"valid")
    rs=np.where(al!=0,ag/al,100.); rv=100.-100./(1.+rs)
    return np.concatenate([np.full(len(p)-len(rv),50.),rv])

def _sw(df:pd.DataFrame,n:int=5)->Tuple[list,list]:
    H,L=[],[]
    for i in range(n,len(df)-n):
        if df["high"].iloc[i]==df["high"].iloc[i-n:i+n+1].max(): H.append(i)
        if df["low"].iloc[i] ==df["low"].iloc[i-n:i+n+1].min():  L.append(i)
    return H,L

def generate_chart(candles:deque,tf:str="M15",
                   entry_price:float=None,exit_price:float=None,
                   direction:str=None,pnl:float=None,
                   chart_type:str="live",reason=None,
                   partial_close_price:float=None)->Optional[str]:
    if len(candles)<20: return None
    df=pd.DataFrame(list(candles)[-80:])
    df.columns=["time","open","high","low","close"]
    df.reset_index(drop=True,inplace=True)

    fig=plt.figure(figsize=(16,10),facecolor=BG)
    gs=gridspec.GridSpec(4,1,figure=fig,hspace=0.05,height_ratios=[4,.6,.6,.6])
    am=fig.add_subplot(gs[0]); av=fig.add_subplot(gs[1],sharex=am)
    ar=fig.add_subplot(gs[2],sharex=am); al=fig.add_subplot(gs[3])
    for a in(am,av,ar,al): _ax_s(a)

    for i,row in df.iterrows():
        c=BC if row["close"]>=row["open"] else RC
        bl=min(row["open"],row["close"]); bh=max(row["open"],row["close"])
        am.plot([i,i],[row["low"],row["high"]],color=c,lw=0.8,alpha=0.9)
        am.add_patch(mpatches.FancyBboxPatch((i-.35,bl),.7,
            max(bh-bl,df["close"].mean()*.00005),boxstyle="square,pad=0",fc=c,ec=c,alpha=0.85))

    for i,row in df.iterrows():
        av.bar(i,1,color=BC if row["close"]>=row["open"] else RC,alpha=0.4,width=.7)
    av.set_ylabel("Vol",color="#555d68",fontsize=6)

    rs=_rsi(df["close"].values)
    ar.plot(range(len(rs)),rs,color="#90a4ae",lw=0.9)
    ar.axhline(70,color=RC,lw=.5,ls="--",alpha=.5)
    ar.axhline(50,color=GR,lw=.4,alpha=.4)
    ar.axhline(30,color=BC,lw=.5,ls="--",alpha=.5)
    ar.set_ylim(0,100); ar.set_ylabel("RSI",color="#555d68",fontsize=6)

    if len(df)>=52:
        ema50=df["close"].ewm(span=50,adjust=False).mean()
        am.plot(range(len(ema50)),ema50.values,color="#78909c",lw=1.,ls="-.",alpha=.6,label="EMA50")

    if state.active_ob:
        ob=state.active_ob; oc=OBB if ob["type"]=="BULL" else OBR
        xs=max(0,len(df)-35)/len(df)
        am.axhspan(ob["low"],ob["high"],xmin=xs,alpha=.15,color=oc)
        am.axhline(ob["high"],color=oc,ls="--",lw=.8,alpha=.7)
        am.axhline(ob["low"], color=oc,ls="--",lw=.8,alpha=.7)
        am.text(2,ob["high"],f" {ob['type']} OB sc:{state.ob_score}/100",
                color=oc,fontsize=7,va="bottom",fontfamily="monospace")

    if state.active_fvg:
        fvg=state.active_fvg
        am.axhspan(fvg["low"],fvg["high"],alpha=.13,color=FC)
        am.text(2,fvg["high"],"  FVG",color=FC,fontsize=7,va="bottom",fontfamily="monospace")

    if state.active_idm:
        idm=state.active_idm
        am.axhline(idm["level"],color=IC,ls=":",lw=1.2,alpha=.9)
        am.text(2,idm["level"],f"  IDM {'✅' if idm.get('swept') else '⏳'}",
                color=IC,fontsize=7,va="bottom",fontfamily="monospace")

    if state.active_trap:
        trap=state.active_trap
        am.axhline(trap["level"],color=TC,ls=":",lw=1.4,alpha=.9)
        am.text(2,trap["level"],f"  TRAP {trap['side']}",
                color=TC,fontsize=7,va="bottom",fontfamily="monospace")

    if state.last_signal and "fib_hi" in state.last_signal:
        sig=state.last_signal; mid=(sig["fib_hi"]+sig["fib_lo"])/2
        am.axhline(mid,color="#78909c",ls="-.",lw=.6,alpha=.4)
        am.axhspan(sig["fib_lo"],mid,alpha=.04,color=BC)
        am.axhspan(mid,sig["fib_hi"],alpha=.04,color=RC)
        am.text(len(df)-2,mid," 0.5 Fib",color="#78909c",fontsize=6,ha="right",fontfamily="monospace")

    if state.last_signal:
        sig=state.last_signal
        am.axhline(sig["entry"],color=EC,lw=1.6,ls="-")
        am.axhline(sig["sl"],   color=SC,lw=1.0,ls="--")
        for tk,tc_ in zip(["tp1","tp2","tp3"],TPC):
            if tk in sig: am.axhline(sig[tk],color=tc_,lw=.8,ls="-.")

    if entry_price:
        am.axhline(entry_price,color=EC,lw=2.2,alpha=.9)
        am.annotate(f"▶ ENTRY {entry_price:.5f}",xy=(len(df)-1,entry_price),
                    color=EC,fontsize=8,ha="right",fontfamily="monospace")

    if partial_close_price:
        am.axhline(partial_close_price,color="#ff9800",lw=1.6,ls="-.",alpha=.9)
        am.annotate(f"📤 50% CLOSE {partial_close_price:.5f}",xy=(len(df)-1,partial_close_price),
                    color="#ff9800",fontsize=8,ha="right",fontfamily="monospace")

    if exit_price:
        xc=BC if(pnl and pnl>0) else RC
        am.axhline(exit_price,color=xc,lw=2.,ls="--",alpha=.9)
        ps=f"+{pnl:.2f}" if pnl and pnl>0 else f"{pnl:.2f}"
        am.annotate(f"◀ EXIT {exit_price:.5f}  P&L:{ps}",xy=(len(df)-1,exit_price),
                    color=xc,fontsize=8,ha="right",fontfamily="monospace")

    # News markers
    for ev in state.news_events:
        if not ev.dt_utc or not ev.is_red: continue
        ep=ev.dt_utc.timestamp()
        for ci,crow in df.iterrows():
            if crow["time"]>=ep:
                am.axvline(ci,color=RC,lw=1.,ls="--",alpha=.5)
                am.text(ci,df["high"].max(),"🔴",fontsize=8,ha="center"); break

    swh,swl=_sw(df)
    for i in swh: am.plot(i,df.iloc[i]["high"]*1.00015,"^",color=BC,ms=4,alpha=.5)
    for i in swl: am.plot(i,df.iloc[i]["low"]*0.99985, "v",color=RC,ms=4,alpha=.5)

    tc_=BC if state.trend_bias=="BULLISH" else(RC if state.trend_bias=="BEARISH" else"#90a4ae")
    tl={"live":"📡 LIVE","entry":"🎯 ENTRY","exit":"🏁 CLOSED"}.get(chart_type,"")
    blk=" 🚫NEWS" if state.block_trading else""
    mkt="🟢" if is_market_open() else"🔴"
    sprd=f" Sprd:{state.current_spread:.5f}" if state.current_spread>0 else""
    am.set_title(
        f"{tl}  {state.pair_display}  ·  {tf}  ·  {mkt}  "
        f"Bias:{state.trend_bias}  Zone:{state.premium_discount}  "
        f"·  {state.current_price:.5f}  Sess:{state.session_now}{sprd}{blk}",
        color=tc_,fontsize=10,fontfamily="monospace",pad=8)
    am.set_ylabel("Price",color="#90a4ae",fontsize=8)

    al.set_xlim(0,1); al.set_ylim(0,1); al.axis("off")
    ts=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    nxt=state.next_red_event
    nxt_s=(f" | 🔴{nxt.title[:16]}@{nxt.dt_utc.astimezone(NY_TZ).strftime('%H:%Mh')}"
           if nxt and nxt.dt_utc else"")
    al.text(.01,.5,
        f"SMC SNIPER v5.1  ·  {state.pair_display}  sym:{state.active_symbol}  "
        f"Bal:{state.account_balance:.2f}{state.account_currency}  Risk:{state.risk_pct*100:.0f}%  "
        f"Score:{state.ob_score}/100  "
        f"H1:{len(state.h1_candles)} M15:{len(state.m15_candles)} M5:{len(state.m5_candles)}"
        f"{nxt_s}  {ts}",
        color="#444d56",fontsize=6,va="center",fontfamily="monospace")

    if reason and chart_type=="entry":
        bxt=(f"Score:{reason.score}  {reason.structure}  {reason.pd_zone}  "
             f"IDM:{reason.idm_sweep}  Sess:{reason.session}")
        am.text(.01,.98,bxt,transform=am.transAxes,fontsize=7,va="top",fontfamily="monospace",
                color="#cdd9e5",bbox=dict(boxstyle="round,pad=.3",fc="#161b22",ec="#30363d",alpha=.85))

    plt.setp(am.get_xticklabels(),visible=False)
    plt.setp(av.get_xticklabels(),visible=False)
    plt.setp(ar.get_xticklabels(),visible=False)
    path=f"/tmp/sniper51_{chart_type}.png"
    plt.tight_layout()
    plt.savefig(path,dpi=130,bbox_inches="tight",facecolor=fig.get_facecolor())
    plt.close(fig); return path

def generate_history_chart()->Optional[str]:
    h=state.trade_history[-20:]
    if not h: return None
    fig,(ax1,ax2)=plt.subplots(2,1,figsize=(14,9),facecolor=BG,gridspec_kw={"height_ratios":[2,1]})
    for a in(ax1,ax2): _ax_s(a)
    labels=[f"#{t['num']}" for t in h]; pnls=[t["pnl"] for t in h]
    cols=[BC if p>0 else RC for p in pnls]
    bars=ax1.bar(labels,pnls,color=cols,alpha=.85,ec=GR)
    ax1.axhline(0,color=GR,lw=.8)
    for bar,val in zip(bars,pnls):
        s="+" if val>=0 else""
        ax1.text(bar.get_x()+bar.get_width()/2,bar.get_height()+(0.05 if val>=0 else-.15),
                 f"{s}{val:.2f}",ha="center",va="bottom",color="#cdd9e5",fontsize=7,fontfamily="monospace")
    wr=f"{state.wins/(state.wins+state.losses)*100:.1f}%" if(state.wins+state.losses)>0 else"N/A"
    ax1.set_title(
        f"📋 History  {state.wins}W/{state.losses}L  WR:{wr}  "
        f"P&L:{state.total_pnl:+.2f} {state.account_currency}  Bal:{state.account_balance:.2f}",
        color="#cdd9e5",fontsize=10,fontfamily="monospace")
    ax1.set_ylabel("P&L",color="#90a4ae",fontsize=9)
    cum=np.cumsum(pnls); cc=BC if cum[-1]>=0 else RC
    ax2.plot(labels,cum,color=cc,lw=1.8,marker="o",ms=4)
    ax2.fill_between(labels,cum,alpha=.12,color=cc)
    ax2.axhline(0,color=GR,lw=.8); ax2.set_ylabel("Cumulative",color="#90a4ae",fontsize=8)
    fig.text(.99,.01,f"SMC SNIPER v5.1 · {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
             color="#444d56",fontsize=7,ha="right")
    path="/tmp/sniper51_history.png"
    plt.tight_layout()
    plt.savefig(path,dpi=120,bbox_inches="tight",facecolor=BG)
    plt.close(fig); return path

# ══════════════════════════════════════════════════════════════════════
# SMC SNIPER BRAIN  (strategy unchanged from v5 — all filters intact)
# ══════════════════════════════════════════════════════════════════════
def _bos(df:pd.DataFrame)->Optional[dict]:
    H,L=_sw(df)
    if len(H)<2 or len(L)<2: return None
    lsh,psh=H[-1],H[-2]; lsl,psl=L[-1],L[-2]; lc=df["close"].iloc[-1]
    if lc>df["high"].iloc[lsh] and lsh>psh:
        return{"type":"BOS","direction":"BULLISH","level":df["high"].iloc[lsh]}
    if lc<df["low"].iloc[lsl]  and lsl>psl:
        return{"type":"BOS","direction":"BEARISH","level":df["low"].iloc[lsl]}
    if df["high"].iloc[lsh]<df["high"].iloc[psh] and lc>df["high"].iloc[lsh]:
        return{"type":"CHoCH","direction":"BULLISH","level":df["high"].iloc[lsh]}
    if df["low"].iloc[lsl]>df["low"].iloc[psl]   and lc<df["low"].iloc[lsl]:
        return{"type":"CHoCH","direction":"BEARISH","level":df["low"].iloc[lsl]}
    return None

def _idm_fn(df:pd.DataFrame,direction:str)->Optional[dict]:
    H,L=_sw(df,n=3)
    if direction=="BULLISH" and len(L)>=2:
        lvl=df["low"].iloc[L[-2]]; last=df.iloc[-1]
        return{"side":"BUY","level":lvl,"swept":last["low"]<lvl and last["close"]>lvl}
    if direction=="BEARISH" and len(H)>=2:
        lvl=df["high"].iloc[H[-2]]; last=df.iloc[-1]
        return{"side":"SELL","level":lvl,"swept":last["high"]>lvl and last["close"]<lvl}
    return None

def _trap_fn(df:pd.DataFrame)->Optional[dict]:
    tol=0.0003 if state.pair_key in("EURUSD","GBPUSD") else 0.0005
    r=df.iloc[-25:]; hs=r["high"].values; ls=r["low"].values
    for i in range(len(hs)-1,1,-1):
        for j in range(i-1,max(i-8,0),-1):
            if abs(hs[i]-hs[j])/hs[j]<tol:
                lvl=(hs[i]+hs[j])/2; last=df.iloc[-1]
                if last["high"]>lvl and last["close"]<lvl:
                    return{"side":"SELL","level":lvl,"swept":True,"type":"EQL_HIGHS"}
    for i in range(len(ls)-1,1,-1):
        for j in range(i-1,max(i-8,0),-1):
            if abs(ls[i]-ls[j])/ls[j]<tol:
                lvl=(ls[i]+ls[j])/2; last=df.iloc[-1]
                if last["low"]<lvl and last["close"]>lvl:
                    return{"side":"BUY","level":lvl,"swept":True,"type":"EQL_LOWS"}
    return None

def _ob_fn(df:pd.DataFrame,direction:str)->Optional[dict]:
    lk=min(30,len(df)-3); r=df.iloc[-lk:].reset_index(drop=True)
    ab=(r["close"]-r["open"]).abs().mean()
    if direction=="BULLISH":
        for i in range(len(r)-3,1,-1):
            c,nc=r.iloc[i],r.iloc[i+1]
            if c["close"]<c["open"] and nc["close"]>c["high"] and abs(nc["close"]-nc["open"])>ab*1.5:
                return{"type":"BULL","high":c["high"],"low":c["low"],
                       "body_hi":max(c["open"],c["close"]),
                       "body_lo":min(c["open"],c["close"]),
                       "displacement":round(abs(nc["close"]-nc["open"])/ab,2)}
    elif direction=="BEARISH":
        for i in range(len(r)-3,1,-1):
            c,nc=r.iloc[i],r.iloc[i+1]
            if c["close"]>c["open"] and nc["close"]<c["low"] and abs(nc["close"]-nc["open"])>ab*1.5:
                return{"type":"BEAR","high":c["high"],"low":c["low"],
                       "body_hi":max(c["open"],c["close"]),
                       "body_lo":min(c["open"],c["close"]),
                       "displacement":round(abs(nc["close"]-nc["open"])/ab,2)}
    return None

def _fvg_fn(df:pd.DataFrame,ob:dict)->Optional[dict]:
    if ob is None: return None
    thr=0.03 if state.pair_key in("EURUSD","GBPUSD") else 0.05
    r=df.iloc[-min(25,len(df)-3):].reset_index(drop=True)
    if ob["type"]=="BULL":
        for i in range(len(r)-3,0,-1):
            c1,c3=r.iloc[i],r.iloc[i+2]; gp=(c3["low"]-c1["high"])/c1["high"]*100
            if c1["high"]<c3["low"] and gp>=thr:
                return{"type":"BULL","high":c3["low"],"low":c1["high"],"gap_pct":gp}
    elif ob["type"]=="BEAR":
        for i in range(len(r)-3,0,-1):
            c1,c3=r.iloc[i],r.iloc[i+2]; gp=(c1["low"]-c3["high"])/c1["low"]*100
            if c1["low"]>c3["high"] and gp>=thr:
                return{"type":"BEAR","high":c1["low"],"low":c3["high"],"gap_pct":gp}
    return None

def _atr_val(df:pd.DataFrame,n:int=14)->float:
    if len(df)<n+1: return 0.
    tr=pd.concat([df["high"]-df["low"],
                  (df["high"]-df["close"].shift()).abs(),
                  (df["low"] -df["close"].shift()).abs()],axis=1).max(axis=1)
    return float(tr.rolling(n).mean().iloc[-1])

def _pd_zone(df:pd.DataFrame):
    H,L=_sw(df,n=8)
    if not H or not L: return"NEUTRAL",0,0
    fh=df["high"].iloc[H[-1]]; fl=df["low"].iloc[L[-1]]
    if fh==fl: return"NEUTRAL",fh,fl
    pct=(state.current_price-fl)/(fh-fl)
    return("DISCOUNT" if pct<.5 else"PREMIUM"),fh,fl

def _h1_trend()->str:
    if len(state.h1_candles)<30: return"NEUTRAL"
    df=pd.DataFrame(list(state.h1_candles)); df.columns=["time","open","high","low","close"]
    r=_bos(df)
    if r: state.trend_bias=r["direction"]
    else:
        c=df["close"].values[-20:]
        state.trend_bias="BULLISH" if np.polyfit(np.arange(len(c)),c,1)[0]>0 else"BEARISH"
    return state.trend_bias

def sniper_score(ob,fvg,trap,idm,rsi,session,atr_ok,ema_ok,candle_ok,struct_type,disp)->Tuple[int,list]:
    s=0; reasons=[]
    if fvg: s+=20; reasons.append(f"FVG+20")
    if trap and trap.get("swept"): s+=15; reasons.append("Trap✅+15")
    if idm  and idm.get("swept"):  s+=15; reasons.append("IDM✅+15")
    if ob   and disp>=2.0:         s+=10; reasons.append(f"Disp{disp:.1f}x+10")
    if struct_type=="BOS":   s+=10; reasons.append("BOS+10")
    elif struct_type=="CHoCH":s+=8; reasons.append("CHoCH+8")
    if session=="OVERLAP":   s+=10; reasons.append("Overlap+10")
    elif session in("LONDON","NY"): s+=7; reasons.append(f"{session}+7")
    if atr_ok:    s+=5; reasons.append("ATR✅+5")
    if ema_ok:    s+=5; reasons.append("EMA✅+5")
    if candle_ok: s+=5; reasons.append("Candle✅+5")
    if ob:
        if ob["type"]=="BULL" and rsi<40: s+=5; reasons.append(f"RSI{rsi:.0f}+5")
        elif ob["type"]=="BEAR" and rsi>60: s+=5; reasons.append(f"RSI{rsi:.0f}+5")
    return min(s,100),reasons

def compute_signal(tf:str="M15")->Optional[dict]:
    buf=state.m15_candles if tf=="M15" else state.m5_candles
    if len(buf)<40: return None
    df=pd.DataFrame(list(buf)); df.columns=["time","open","high","low","close"]

    bias=_h1_trend()
    if bias=="NEUTRAL": return None

    struct=_bos(df)
    if struct is None or struct["direction"]!=bias: return None

    pd_zone,fib_hi,fib_lo=_pd_zone(df)
    state.premium_discount=pd_zone
    if bias=="BULLISH" and pd_zone!="DISCOUNT": return None
    if bias=="BEARISH" and pd_zone!="PREMIUM":  return None

    idm=_idm_fn(df,bias); state.active_idm=idm
    if idm is None or not idm.get("swept"): return None

    trap=_trap_fn(df); state.active_trap=trap
    if trap is None or not trap.get("swept"): return None
    if trap["side"]!=("BUY" if bias=="BULLISH" else"SELL"): return None

    ob=_ob_fn(df,bias)
    if ob is None: return None
    state.active_ob=ob

    fvg=_fvg_fn(df,ob); state.active_fvg=fvg
    rsi_now=float(_rsi(df["close"].values)[-1])

    session=get_session(); state.session_now=session
    if state.pair_key=="XAUUSD" and session not in("LONDON","NY","OVERLAP"): return None
    if session=="CLOSE": return None

    atr_v=_atr_val(df)
    mn=ATR_MIN.get(state.pair_key,.0001)
    atr_ok=atr_v>=mn; state.atr_filter_ok=atr_ok
    if not atr_ok: return None

    ema50=df["close"].ewm(span=50,adjust=False).mean().iloc[-1]
    price=df["close"].iloc[-1]
    ema_ok=(price>ema50 if bias=="BULLISH" else price<ema50)
    if not ema_ok: return None

    last=df.iloc[-1]
    candle_ok=(last["close"]>last["open"] if bias=="BULLISH" else last["close"]<last["open"])
    disp=ob.get("displacement",0)

    sc,score_reasons=sniper_score(ob,fvg,trap,idm,rsi_now,session,atr_ok,ema_ok,candle_ok,struct["type"],disp)
    state.ob_score=sc
    if sc<MIN_SCORE: log.info(f"Score {sc}<{MIN_SCORE}"); return None

    if bias=="BULLISH":
        entry=ob["body_hi"]; sl=ob["low"]*.9995
    else:
        entry=ob["body_lo"]; sl=ob["high"]*1.0005

    risk=abs(entry-sl)
    if risk==0: return None
    mult=1 if bias=="BULLISH" else-1
    tp1=entry+risk*state.tp1_r*mult
    tp2=entry+risk*state.tp2_r*mult
    tp3=entry+risk*state.tp3_r*mult
    stake=max(1.,round(max(PAIR_REGISTRY[state.pair_key][3],state.account_balance*state.risk_pct),2))

    reason=TradeReason()
    reason.h1_trend  =f"H1 {bias}"
    reason.structure =f"{struct['type']} {bias}"
    reason.pd_zone   =pd_zone
    reason.idm_sweep ="✅ Swept" if idm.get("swept") else"⚠️ Partial"
    reason.trap_sweep=f"✅ {trap['type']}"
    reason.ob_type   =f"{ob['type']} OB (disp:{disp:.1f}x)"
    reason.fvg_present=fvg is not None
    reason.session   =session
    reason.atr_state =f"ATR={atr_v:.4f}✅"
    reason.ema_confirm=f"EMA50={ema50:.4f}({'above' if price>ema50 else 'below'})✅"
    reason.rsi_level =rsi_now
    reason.candle_conf=f"{'Bullish' if candle_ok and bias=='BULLISH' else 'Bearish'} close✅"
    reason.score     =sc
    reason.entry_logic=" + ".join(score_reasons)

    sig={
        "direction":"BUY" if bias=="BULLISH" else"SELL",
        "entry":round(entry,5),"sl":round(sl,5),
        "tp1":round(tp1,5),"tp2":round(tp2,5),"tp3":round(tp3,5),
        "risk_r":round(risk,5),"stake":stake,
        "struct":struct["type"],"ob":ob,"fvg":fvg,"trap":trap,"idm":idm,
        "ob_score":sc,"rsi":rsi_now,"pd_zone":pd_zone,
        "fib_hi":fib_hi,"fib_lo":fib_lo,
        "session":session,"tf":tf,"bias":bias,
        "reason":reason,"score_reasons":score_reasons,
        "ts":datetime.now(UTC).isoformat(),
        "partial_closed":False,    # v5.1 partial close tracking
        "be_moved":False,
        "trailing_active":False,
        "trailing_sl":None,
    }
    state.last_signal=sig
    return sig

# ══════════════════════════════════════════════════════════════════════
# TRADE MANAGEMENT v5.1
# Features: Immediate BE at 1:1, 50% partial close at TP1,
#           M5-structural trailing stop after TP2
# ══════════════════════════════════════════════════════════════════════
def _m5_structural_trailing(info:dict)->Optional[float]:
    """
    After TP2: trailing stop follows M5 market structure.
    BUY:  trail below the last M5 higher low.
    SELL: trail above the last M5 lower high.
    """
    if len(state.m5_candles)<15: return None
    df=pd.DataFrame(list(state.m5_candles)[-40:])
    df.columns=["time","open","high","low","close"]
    swh,swl=_sw(df,n=3)
    d=info["direction"]
    if d=="BUY" and swl:
        return float(df["low"].iloc[swl[-1]])*0.9998
    if d=="SELL" and swh:
        return float(df["high"].iloc[swh[-1]])*1.0002
    return None

async def _partial_close(cid:str,info:dict)->bool:
    """Close 50% of position at TP1 / 1:1 RR."""
    sig=info.get("signal",{}) or {}
    amt=info.get("amount",0)
    half=max(1.,round(amt/2,2))
    try:
        # Deriv doesn't support partial close directly — sell full, re-open half
        full_ok=await close_contract(cid)
        if not full_ok: return False
        log.info(f"Partial close done for {cid} — re-opening {half:.2f}")
        await tg_async(
            f"📤 *Partial Close (50%)*\n"
            f"Contract `{cid}` — Locked profit at `{state.current_price:.5f}`\n"
            f"Remaining stake: `{half:.2f}`  SL → Entry (BreakEven)")
        # Re-open the remaining half with SL at entry
        cid2=await open_contract(info["direction"],half)
        if cid2:
            state.open_contracts[cid2]["signal"]=sig
            state.open_contracts[cid2]["be_moved"]=True   # already at BE
            state.open_contracts[cid2]["entry"]=sig.get("entry",state.current_price)
            if sig: sig["sl"]=sig.get("entry",state.current_price)
        return True
    except Exception as e:
        log.error(f"_partial_close: {e}"); return False

async def check_trade_mgmt():
    """Full trade management: BE, partial close, structural trailing."""
    for cid,info in list(state.open_contracts.items()):
        sig=info.get("signal",{}) or {}
        if not sig: continue
        p=state.current_price; d=info.get("direction","BUY")
        entry=sig.get("entry",p)
        tp1=sig.get("tp1",p); tp2=sig.get("tp2",p)
        risk=abs(entry-sig.get("sl",entry))

        # ── 1:1 RR → immediate Break-Even ──
        one_r=entry+(risk*(1 if d=="BUY" else-1))
        if not sig.get("be_moved"):
            hit_1r=(d=="BUY" and p>=one_r) or(d=="SELL" and p<=one_r)
            if hit_1r:
                sig["be_moved"]=True; sig["sl"]=entry
                info["be_moved"]=True
                log.info(f"[{cid}] BE triggered at 1:1 RR")
                await tg_async(f"✅ *BreakEven* `{cid}`\nSL → Entry `{entry}` (1:1 RR reached)")

        # ── TP1 hit → 50% partial close ──
        if not sig.get("partial_closed") and not info.get("partial_closed"):
            hit_tp1=(d=="BUY" and p>=tp1) or(d=="SELL" and p<=tp1)
            if hit_tp1:
                sig["partial_closed"]=True; info["partial_closed"]=True
                chart=generate_chart(state.m15_candles,"M15",
                                     partial_close_price=state.current_price,chart_type="live")
                await tg_async(
                    f"🎯 *TP1 Hit — Partial Close*\n"
                    f"Price: `{p:.5f}`  TP1: `{tp1:.5f}`\n"
                    f"Closing 50% to lock profit.",photo_path=chart)
                await _partial_close(cid,info)
                continue

        # ── TP2 hit → activate M5 structural trailing ──
        hit_tp2=(d=="BUY" and p>=tp2) or(d=="SELL" and p<=tp2)
        if hit_tp2 and not sig.get("trailing_active"):
            sig["trailing_active"]=True
            log.info(f"[{cid}] M5 structural trailing activated")
            await tg_async(f"📐 *Trailing Stop Activated*\n"
                           f"TP2 reached! Now following M5 structure.")

        # ── Structural trailing logic ──
        if sig.get("trailing_active"):
            new_sl=_m5_structural_trailing(info)
            if new_sl is not None:
                cur_sl=sig.get("sl",0)
                if d=="BUY"  and new_sl>cur_sl:
                    sig["sl"]=new_sl; log.info(f"[{cid}] Trail SL→{new_sl:.5f}")
                elif d=="SELL" and new_sl<cur_sl:
                    sig["sl"]=new_sl; log.info(f"[{cid}] Trail SL→{new_sl:.5f}")

# ══════════════════════════════════════════════════════════════════════
# DERIV WEBSOCKET
# ══════════════════════════════════════════════════════════════════════
async def send_req(payload:dict)->dict:
    if state.ws is None: raise RuntimeError("WS not connected")
    rid=state.req_id; state.req_id+=1; payload["req_id"]=rid
    fut=asyncio.get_event_loop().create_future()
    state.pending_reqs[rid]=fut
    await state.ws.send(json.dumps(payload))
    try:    return await asyncio.wait_for(asyncio.shield(fut),timeout=20)
    except asyncio.TimeoutError:
        state.pending_reqs.pop(rid,None)
        raise asyncio.TimeoutError(f"Timeout {list(payload.keys())}")

async def authorize(token:str=None)->dict:
    t=token or state.deriv_token
    if not t: raise RuntimeError("No token")
    resp=await send_req({"authorize":t})
    if "error" in resp: raise RuntimeError(resp["error"]["message"])
    auth=resp["authorize"]
    state.broker_connected=True
    state.account_id  =auth.get("loginid","")
    state.account_type=auth.get("account_type","demo")
    if token:
        state.deriv_token=token
        try: TOKEN_FILE.write_text(token)
        except Exception: pass
    log.info(f"Authorized: {state.account_id} ({state.account_type})")
    return auth

async def get_balance():
    r=await send_req({"balance":1,"subscribe":0})
    if "balance" in r:
        state.account_balance =r["balance"]["balance"]
        state.account_currency=r["balance"]["currency"]

async def get_spread()->float:
    """Fetch current spread via proposal API."""
    try:
        r=await send_req({
            "proposal":1,"subscribe":0,
            "amount":1,"basis":"stake",
            "contract_type":"MULTUP",
            "currency":state.account_currency,
            "duration":1,"duration_unit":"d",
            "symbol":state.active_symbol or PAIR_REGISTRY[state.pair_key][0],
        })
        if "proposal" in r:
            spot     =float(r["proposal"].get("spot","0") or 0)
            spot_time=float(r["proposal"].get("spot_time","0") or 0)
            bid      =float(r["proposal"].get("ask_price","0") or 0)
            state.current_spread=abs(bid-spot)
            return state.current_spread
    except Exception as e:
        log.debug(f"get_spread: {e}")
    return 0.

def _store(nom:int,rows:list):
    if nom==3600: state.h1_candles.extend(rows)
    elif nom==900:
        state.m15_candles.extend(rows)
        if rows: state.current_price=float(rows[-1][4])
    elif nom==300:
        state.m5_candles.extend(rows)
        if rows: state.current_price=float(rows[-1][4])

async def _fetch(sym:str,nom:int)->int:
    lbl={3600:"H1",900:"M15",300:"M5"}
    for ag in GRAN_FALLBACKS.get(nom,[nom]):
        try:
            r=await send_req({"ticks_history":sym,"end":"latest",
                              "count":200,"granularity":ag,"style":"candles"})
            if "error" in r: log.warning(f"Fetch {sym} g={ag}: {r['error'].get('message','')}"); continue
            raw=r.get("candles",[])
            if not raw: continue
            rows=[(int(c["epoch"]),float(c["open"]),float(c["high"]),
                   float(c["low"]),float(c["close"])) for c in raw]
            state.gran_actual[nom]=ag; _store(nom,rows)
            log.info(f"✅ {len(rows)} {lbl.get(nom,'?')} (g={ag}) {sym}")
            asyncio.ensure_future(send_req({"ticks_history":sym,"end":"latest",
                "count":1,"granularity":ag,"style":"candles","subscribe":1}))
            return len(rows)
        except asyncio.TimeoutError: log.warning(f"Timeout g={ag}")
        except Exception as e:       log.error(f"_fetch g={ag}: {e}")
    return 0

async def _resolve_sym(key:str)->str:
    pri,otc=PAIR_REGISTRY[key][0],PAIR_REGISTRY[key][1]
    for sym in(pri,otc):
        try:
            r=await send_req({"ticks_history":sym,"end":"latest","count":1,"granularity":3600,"style":"candles"})
            if "candles" in r: log.info(f"Symbol OK: {sym}"); return sym
            log.warning(f"Symbol {sym}: {r.get('error',{}).get('message','?')}")
        except Exception as e: log.warning(f"Sym test {sym}: {e}")
    return pri

async def subscribe_pair(key:str)->Tuple[int,int,int]:
    state.h1_candles.clear(); state.m15_candles.clear(); state.m5_candles.clear()
    state.last_signal=None; state.active_ob=None; state.active_fvg=None
    state.active_trap=None; state.active_idm=None
    state.current_price=0.; state.trend_bias="NEUTRAL"
    state.ob_score=0; state.premium_discount="NEUTRAL"
    sym=await _resolve_sym(key); state.active_symbol=sym; state.subscribed_sym=sym
    h=await _fetch(sym,3600); m=await _fetch(sym,900); f=await _fetch(sym,300)
    log.info(f"subscribe_pair: {sym} H1:{h} M15:{m} M5:{f}")
    return h,m,f

async def open_contract(direction:str,amount:float)->Optional[str]:
    if state.paused or state.block_trading: return None
    # ── Spread Guard ──
    spread=await get_spread()
    if spread>0 and spread>state.max_spread:
        log.warning(f"Spread guard: {spread:.5f} > {state.max_spread:.5f} — ORDER CANCELED")
        await tg_async(
            f"⚠️ *Spread Guard — Order Canceled*\n"
            f"Current spread: `{spread:.5f}`\n"
            f"Max allowed:    `{state.max_spread:.5f}`\n"
            f"_Waiting for spread to normalize..._")
        return None
    ct="MULTUP" if direction=="BUY" else"MULTDOWN"
    try:
        r=await send_req({"buy":1,"price":round(amount,2),"parameters":{
            "contract_type":ct,
            "symbol":state.active_symbol or PAIR_REGISTRY[state.pair_key][0],
            "amount":round(amount,2),"currency":state.account_currency,
            "multiplier":10,"basis":"stake","stop_out":1}})
        if "error" in r: log.error(f"Buy: {r['error']['message']}"); return None
        cid=r["buy"]["contract_id"]
        state.open_contracts[cid]={
            "direction":direction,"entry":state.current_price,
            "amount":amount,"signal":state.last_signal,
            "be_moved":False,"partial_closed":False,
            "opened_at":time.time()}
        state.trade_count+=1
        log.info(f"✅ Opened {cid} [{direction}] ${amount:.2f}  spread:{spread:.5f}")
        asyncio.ensure_future(send_req({"proposal_open_contract":1,"contract_id":cid,"subscribe":1}))
        return cid
    except Exception as e: log.error(f"open_contract: {e}"); return None

async def close_contract(cid:str)->bool:
    try:
        r=await send_req({"sell":cid,"price":0})
        if "error" in r: log.error(f"Close: {r['error']['message']}"); return False
        state.open_contracts.pop(cid,None); log.info(f"🔴 Closed {cid}"); return True
    except Exception as e: log.error(f"close_contract: {e}"); return False

async def close_all()->int:
    ids=list(state.open_contracts.keys())
    for cid in ids: await close_contract(cid)
    return len(ids)

async def _resync_open_contracts():
    """
    After WS reconnect: re-subscribe to all known open contracts
    to restore SL/TP tracking state.
    """
    if not state.open_contracts: return
    log.info(f"Resyncing {len(state.open_contracts)} open contracts...")
    for cid in list(state.open_contracts.keys()):
        try:
            r=await send_req({"proposal_open_contract":1,"contract_id":cid,"subscribe":1})
            poc=r.get("proposal_open_contract",{})
            if poc.get("status") in("sold","expired"):
                log.info(f"Contract {cid} already closed during reconnect — removing")
                info=state.open_contracts.pop(cid,{})
                profit=float(poc.get("profit",0))
                if profit>0: state.wins+=1
                else:        state.losses+=1
                state.total_pnl+=profit
            else:
                log.info(f"Contract {cid} still open: profit={poc.get('profit',0)}")
        except Exception as e:
            log.error(f"Resync {cid}: {e}")

# ══════════════════════════════════════════════════════════════════════
# MESSAGE HANDLER
# ══════════════════════════════════════════════════════════════════════
def _upd_buf(actual_gran:int,rows:list):
    rev={v:k for k,v in state.gran_actual.items()}
    nom=rev.get(actual_gran,actual_gran); _store(nom,rows)

async def handle_msg(msg:dict):
    rid=msg.get("req_id")
    if rid and rid in state.pending_reqs:
        fut=state.pending_reqs.pop(rid)
        if not fut.done(): fut.set_result(msg)
        return
    mt=msg.get("msg_type","")
    if mt=="ohlc":
        c=msg["ohlc"]; gran=int(c.get("granularity",0))
        _upd_buf(gran,[(int(c["epoch"]),float(c["open"]),float(c["high"]),
                        float(c["low"]),float(c["close"]))])
        state.last_ws_ping=time.time()
    elif mt=="candles":
        gran=int(msg.get("echo_req",{}).get("granularity",0))
        rows=[(int(c["epoch"]),float(c["open"]),float(c["high"]),
               float(c["low"]),float(c["close"])) for c in msg.get("candles",[])]
        if rows: _upd_buf(gran,rows)
    elif mt=="tick":
        state.current_price=float(msg["tick"]["quote"])
        state.last_ws_ping=time.time()
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
            sig=info.get("signal",{}) or {}
            state.trade_history.append({
                "num":tnum,"id":cid,"pair":state.pair_display,
                "direction":info.get("direction","?"),
                "entry":info.get("entry",0.),"exit":exit_s,
                "pnl":round(profit,2),"win":profit>0,
                "score":sig.get("ob_score",0),
                "session":sig.get("session","?"),
                "ts":datetime.now(UTC).strftime("%m/%d %H:%M")})
            if len(state.trade_history)>50: state.trade_history.pop(0)
            chart=generate_chart(state.m15_candles,"M15",
                entry_price=info.get("entry"),exit_price=exit_s,
                direction=info.get("direction"),pnl=profit,chart_type="exit")
            sign="+" if profit>0 else""
            wr=f"{state.wins/(state.wins+state.losses)*100:.1f}%" if(state.wins+state.losses)>0 else"N/A"
            reason_obj=sig.get("reason")
            post=(reason_obj.build_report(info.get("direction","?")) if reason_obj else"")
            amh =(reason_obj.build_amharic(info.get("direction","?")) if reason_obj else"")
            await tg_async(
                f"{'✅ WIN' if profit>0 else '❌ LOSS'}  `#{tnum}` — *Post-Trade Report v5.1*\n\n"
                f"Pair:`{state.pair_display}`  Dir:`{info.get('direction','?')}`\n"
                f"Entry:`{info.get('entry',0):.5f}`  Exit:`{exit_s:.5f}`\n"
                f"P&L:`{sign}{profit:.2f} {state.account_currency}`\n"
                f"Score:`{sig.get('ob_score',0)}/100`  Session:`{sig.get('session','?')}`\n"
                f"Partial Close:`{'✅ Yes' if info.get('partial_closed') else '❌ No'}`\n"
                f"W/L:{state.wins}/{state.losses}  WR:{wr}  "
                f"Total:{state.total_pnl:+.2f}  Bal:{state.account_balance:.2f}\n\n"
                f"{post}",photo_path=chart)
            if amh: await tg_async(amh)
    elif mt=="balance":
        state.account_balance =msg["balance"]["balance"]
        state.account_currency=msg["balance"]["currency"]
    elif "error" in msg: log.warning(f"API: {msg['error'].get('message','?')}")

# ══════════════════════════════════════════════════════════════════════
# AUTO CHART BROADCAST
# ══════════════════════════════════════════════════════════════════════
async def chart_loop():
    await asyncio.sleep(50)
    while state.running:
        try:
            if state.current_price>0 and len(state.m15_candles)>=20:
                path=generate_chart(state.m15_candles,"M15",chart_type="live")
                if path:
                    be="📈" if state.trend_bias=="BULLISH" else"📉" if state.trend_bias=="BEARISH" else"➡️"
                    pe="🟢" if state.premium_discount=="DISCOUNT" else"🔴" if state.premium_discount=="PREMIUM" else"⚪"
                    blk=" | 🚫NEWS" if state.block_trading else""
                    sprd=(f"  Spread:`{state.current_spread:.5f}`{'🔴' if state.current_spread>state.max_spread else'✅'}"
                          if state.current_spread>0 else"")
                    await tg_async(
                        f"{market_header()}{blk}\n\n"
                        f"{be} *{state.pair_display}  M15*\n"
                        f"Price:`{state.current_price:.5f}`  Bias:`{state.trend_bias}`\n"
                        f"Zone:{pe}`{state.premium_discount}`  Score:`{state.ob_score}/100`\n"
                        f"Session:`{state.session_now}`  ATR:`{'OK' if state.atr_filter_ok else 'LOW'}`{sprd}\n"
                        f"Open:`{len(state.open_contracts)}`  Bal:`{state.account_balance:.2f}`",photo_path=path)
        except Exception as e: log.error(f"chart_loop: {e}")
        await asyncio.sleep(CHART_INTERVAL)

# ══════════════════════════════════════════════════════════════════════
# AUTONOMOUS TRADING LOOP
# ══════════════════════════════════════════════════════════════════════
async def trading_loop():
    await asyncio.sleep(35)
    while state.running:
        try:
            if(state.paused or state.block_trading or
               state.current_price==0 or not state.broker_connected):
                await asyncio.sleep(30); continue
            if not is_market_open():
                state.market_open=False; await asyncio.sleep(60); continue
            state.market_open=True

            await check_trade_mgmt()

            if time.time()-state.last_trade_ts<state.signal_cooldown:
                await asyncio.sleep(30); continue

            sig=compute_signal("M15")
            if sig:
                sig5=compute_signal("M5")
                if sig5 and sig5["direction"]==sig["direction"]:
                    reason=sig.get("reason")
                    chart=generate_chart(state.m15_candles,"M15",
                        entry_price=sig["entry"],direction=sig["direction"],
                        chart_type="entry",reason=reason)
                    sc_txt=" + ".join(sig.get("score_reasons",[])[:5])
                    await tg_async(
                        f"🎯 *SNIPER ENTRY v5.1 — {state.pair_display}*\n"
                        f"Score:`{sig['ob_score']}/100`  Auto-Exec ✅\n\n"
                        f"Dir:  `{sig['direction']}`\n"
                        f"Entry:`{sig['entry']}`  SL:`{sig['sl']}`\n"
                        f"TP1({state.tp1_r}R):`{sig['tp1']}`  [50% close here]\n"
                        f"TP2({state.tp2_r}R):`{sig['tp2']}`  [Trail starts]\n"
                        f"TP3({state.tp3_r}R):`{sig['tp3']}`\n"
                        f"MaxSpread:`{state.max_spread}`\n\n"
                        f"*Score:* `{sc_txt}`\n"
                        f"Stake:`{sig['stake']:.2f} {state.account_currency}`"
                        f"{'  💎' if state.small_acc_mode else''}",photo_path=chart)
                    cid=await open_contract(sig["direction"],sig["stake"])
                    if cid: state.last_trade_ts=time.time()
        except Exception as e:
            log.error(f"trading_loop: {e}\n{traceback.format_exc()}")
        await asyncio.sleep(30)

# ══════════════════════════════════════════════════════════════════════
# TELEGRAM POLLING
# ══════════════════════════════════════════════════════════════════════
async def tg_poll_loop():
    if not TELEGRAM_TOKEN: log.warning("No TELEGRAM_TOKEN"); return
    base=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    loop=asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None,lambda: requests.post(
            f"{base}/deleteWebhook",json={"drop_pending_updates":True},timeout=10))
        log.info("Webhook cleared.")
    except Exception as e: log.warning(f"Webhook: {e}")
    await asyncio.sleep(2)
    offset=0; ec=0
    while state.running:
        try:
            r=await loop.run_in_executor(None,lambda: requests.get(
                f"{base}/getUpdates",
                params={"offset":offset,"timeout":20,
                        "allowed_updates":["message","callback_query"]},timeout=25))
            data=r.json()
            if not data.get("ok"):
                desc=data.get("description",""); log.error(f"TG: {desc}")
                if "Conflict" in desc:
                    await asyncio.sleep(30)
                    await loop.run_in_executor(None,lambda: requests.post(
                        f"{base}/deleteWebhook",json={"drop_pending_updates":True},timeout=10))
                else: await asyncio.sleep(10)
                ec+=1; continue
            ec=0
            for upd in data.get("result",[]):
                offset=upd["update_id"]+1; await _handle_upd(upd)
        except Exception as e:
            ec+=1; log.error(f"TG poll: {e}"); await asyncio.sleep(min(5*ec,60))
        await asyncio.sleep(.5)

async def _handle_upd(upd:dict):
    if "message" in upd:
        text=upd["message"].get("text","").strip()
        cid =str(upd["message"]["chat"]["id"])
        if TELEGRAM_CHAT_ID and cid!=TELEGRAM_CHAT_ID: return
        if state.awaiting_token and text and not text.startswith("/"): await _process_token(text); return
        await _cmd(text)
    elif "callback_query" in upd:
        cq=upd["callback_query"]; data=cq.get("data",""); cqid=cq["id"]
        cid=str(cq["message"]["chat"]["id"])
        if TELEGRAM_CHAT_ID and cid!=TELEGRAM_CHAT_ID: tg_answer(cqid,"Unauthorized"); return
        tg_answer(cqid); await _cmd(data)

async def _process_token(token:str):
    state.awaiting_token=False
    if not state.ws:
        await tg_async("⚠️ WS not ready. Please wait.",reply_markup=kb_main()); return
    await tg_async("🔄 Authenticating...",reply_markup=kb_main())
    try:
        await authorize(token); await get_balance()
        icon="🔴 REAL" if state.account_type=="real" else"🟢 DEMO"
        await tg_async(
            f"✅ *Broker Connected!*\n\n"
            f"Account: `{state.account_id}`  Type: {icon}\n"
            f"Balance: `{state.account_balance:.2f} {state.account_currency}`\n\n"
            f"_Sniper Brain armed. Scanning..._",reply_markup=kb_main())
        h,m,f=await subscribe_pair(state.pair_key)
        log.info(f"Re-subscribed after token: H1:{h} M15:{m} M5:{f}")
    except Exception as e:
        await tg_async(f"❌ Auth failed: `{e}`",reply_markup=kb_connect())

async def _cmd(cmd:str):
    cmd=cmd.lower().strip()

    if cmd in("/start","/help","cmd_back"):
        bl=("🚫 "+state.block_reason if state.block_trading
            else("🛑 PAUSED" if state.paused else"🟢 AUTONOMOUS"))
        conn="✅ Connected" if state.broker_connected else"❌ Tap 🔗 Connect Broker"
        await tg_async(
            f"{market_header()}\n\n🤖 *SMC SNIPER EA v5.1*\n\n"
            f"Status: {bl}\nBroker: {conn}\n"
            f"Acct:`{state.account_id}` ({state.account_type.upper()})\n"
            f"Bal:`{state.account_balance:.2f} {state.account_currency}`\n"
            f"Pair:`{state.pair_display}`  Risk:`{state.risk_pct*100:.0f}%`\n"
            f"MinScore:`{MIN_SCORE}/100`  Spread limit:`{state.max_spread}`\n"
            f"Buffers H1:`{len(state.h1_candles)}/600` M15:`{len(state.m15_candles)}/600`",
            reply_markup=kb_main())

    elif cmd in("/status","cmd_status"):
        bl=("🚫 "+state.block_reason if state.block_trading
            else("🛑 PAUSED" if state.paused else"🟢 SCANNING"))
        nxt=state.next_red_event; nxt_s=""
        if nxt and nxt.dt_utc:
            m_away=int((nxt.dt_utc-datetime.now(UTC)).total_seconds()//60)
            nxt_s=(f"\n🔴 Next Red: *{nxt.title}* @ "
                   f"`{nxt.dt_utc.astimezone(NY_TZ).strftime('%I:%M%p ET')}` (~{m_away}m)")
        sig_s=""
        if state.last_signal:
            s=state.last_signal
            sig_s=(f"\n\n*Last Setup:*\n`{s['direction']}` @ `{s['entry']}`  "
                   f"SL:`{s['sl']}`\nScore:`{s['ob_score']}/100`  {s['struct']}  {s['pd_zone']}")
        sprd=(f"\nSpread:`{state.current_spread:.5f}` "
              f"{'🔴 HIGH' if state.current_spread>state.max_spread else '✅ OK'}"
              if state.current_spread>0 else"")
        await tg_async(
            f"{market_header()}\n\n⚡ *Sniper Status v5.1*\n"
            f"Mode:{bl}\nPair:`{state.pair_display}`  Price:`{state.current_price:.5f}`\n"
            f"Bias:`{state.trend_bias}`  Zone:`{state.premium_discount}`\n"
            f"Session:`{state.session_now}`  ATR:`{'OK' if state.atr_filter_ok else 'LOW'}`{sprd}\n"
            f"Open:`{len(state.open_contracts)}`\n"
            f"Acct:`{state.account_type.upper()}`  Bal:`{state.account_balance:.2f}`\n"
            f"W/L:`{state.wins}/{state.losses}`  P&L:`{state.total_pnl:+.2f}`"
            f"{nxt_s}{sig_s}",reply_markup=kb_main())

    elif cmd in("/news","cmd_news"):
        if time.time()-state.news_last_fetch>3600 or not state.news_events:
            await tg_async("⏳ Fetching from Forex Factory...",reply_markup=kb_main())
            loop=asyncio.get_event_loop()
            evs=await loop.run_in_executor(None,fetch_news)
            state.news_events=evs; state.news_last_fetch=time.time()
            state.next_red_event=_next_red()
            path=await loop.run_in_executor(None,generate_news_chart)
            state.news_chart_path=path
        nxt=state.next_red_event; ni=""
        if nxt and nxt.dt_utc:
            m_away=int((nxt.dt_utc-datetime.now(UTC)).total_seconds()//60)
            ni=(f"\n\n🔴 Next: *{nxt.title}* @ `{nxt.dt_utc.astimezone(NY_TZ).strftime('%I:%M%p ET')}`"
                f" (~{m_away}m)  Vol:`{'⚡'*nxt.volatility_score}`"
                f"\nPost-wait: `{nxt.dynamic_post_wait()//60}m`")
        bl_s,bl_r=_news_block(); bi=f"\n\n{bl_r}" if bl_s else"\n\n✅ No active block."
        await tg_async(
            f"{market_header()}\n\n📰 *Economic Calendar v5.1*\n\n"
            f"{_amharic_summary()}{ni}{bi}",
            photo_path=state.news_chart_path,reply_markup=kb_main())

    elif cmd in("/chart","cmd_chart"):
        m15=len(state.m15_candles)
        if m15>=20:
            path=generate_chart(state.m15_candles,"M15",chart_type="live")
            if path:
                pe="🟢" if state.premium_discount=="DISCOUNT" else"🔴" if state.premium_discount=="PREMIUM" else"⚪"
                await tg_async(
                    f"{market_header()}\n\n"
                    f"📊 *{state.pair_display}  M15*\n"
                    f"Price:`{state.current_price:.5f}`  Bias:`{state.trend_bias}`\n"
                    f"Zone:{pe}`{state.premium_discount}`  Score:`{state.ob_score}/100`\n"
                    f"Spread:`{state.current_spread:.5f}` {'🔴' if state.current_spread>state.max_spread else '✅'}",
                    photo_path=path,reply_markup=kb_main()); return
        await tg_async(
            f"{market_header()}\n\n⚠️ *Chart not ready*\n"
            f"M15:`{m15}` (needs 20+)  sym:`{state.active_symbol}`",reply_markup=kb_main())

    elif cmd in("/history","cmd_history"):
        if not state.trade_history:
            await tg_async("📋 No history yet.",reply_markup=kb_main()); return
        path=generate_history_chart()
        lines=["📋 *Trade History — Last 10 (v5.1)*\n"]
        for t in state.trade_history[-10:]:
            s="+" if t["pnl"]>0 else""
            lines.append(f"{'✅' if t['win'] else '❌'} `#{t['num']}` {t['direction']} "
                         f"`{t['entry']:.5f}`→`{t['exit']:.5f}` "
                         f"`{s}{t['pnl']:.2f}` sc:`{t.get('score',0)}` _{t['ts']}_")
        wr=f"{state.wins/(state.wins+state.losses)*100:.1f}%" if(state.wins+state.losses)>0 else"N/A"
        lines.append(f"\nTotal:`{state.total_pnl:+.2f}`  WR:`{wr}`")
        await tg_async("\n".join(lines),photo_path=path,reply_markup=kb_main())

    elif cmd in("/balance","cmd_balance"):
        if state.ws:
            try: await get_balance()
            except Exception: pass
        wr=f"{state.wins/(state.wins+state.losses)*100:.1f}%" if(state.wins+state.losses)>0 else"N/A"
        icon="🔴 REAL" if state.account_type=="real" else"🟢 DEMO"
        await tg_async(
            f"💰 *Balance*\n`{state.account_balance:.2f} {state.account_currency}`\n"
            f"Type:{icon}  ID:`{state.account_id}`\n\n"
            f"Trades:`{state.trade_count}`  W/L:`{state.wins}/{state.losses}`  WR:`{wr}`\n"
            f"Total P&L:`{state.total_pnl:+.2f}`",reply_markup=kb_main())

    elif cmd in("/connect","cmd_connect"):
        state.awaiting_token=True
        await tg_async(
            f"🔗 *Connect Broker — Deriv*\n\n"
            f"Please send your *Deriv API Token* as the next message.\n\n"
            f"Token needs: ✅ Read + ✅ Trade scopes.\n\n"
            f"Get it at: `app.deriv.com/account/api-token`\n"
            f"_Token saved securely on server._",reply_markup=kb_connect())

    elif cmd=="cmd_token_help":
        await tg_async(
            f"📋 *How to Get Deriv Token:*\n\n"
            f"1. Open `app.deriv.com`\n"
            f"2. Account Settings → API Token\n"
            f"3. Create → Enable *Read + Trade*\n"
            f"4. Copy and paste the token here",reply_markup=kb_connect())

    elif cmd in("/settings","cmd_settings"):
        await tg_async(
            f"⚙️ *Settings v5.1*\n"
            f"Pair:`{state.pair_display}`  sym:`{state.active_symbol}`\n"
            f"Risk:`{state.risk_pct*100:.0f}%`  TPs:`{state.tp1_r}R/{state.tp2_r}R/{state.tp3_r}R`\n"
            f"MinScore:`{MIN_SCORE}/100`  MaxSpread:`{state.max_spread}`\n"
            f"Pre-block:`{state.pre_news_block//60}m`  Post-wait:`{state.post_news_wait//60}m`\n"
            f"Buffer depth: H1:`600` M15:`600` M5:`300`\n"
            f"Small Acc:`{'ON 💎' if state.small_acc_mode else 'OFF'}`",
            reply_markup=kb_settings())

    elif cmd=="cmd_pair_menu": await tg_async("💱 *Select Pair:*",reply_markup=kb_pair_menu())

    elif cmd.startswith("cmd_pair_"):
        key=cmd.replace("cmd_pair_","").upper()
        if key in PAIR_REGISTRY:
            old=state.pair_key; state.pair_key=key; state.small_acc_mode=False
            if state.ws:
                try:
                    await tg_async(f"⏳ Switching to `{PAIR_REGISTRY[key][4]}`...",reply_markup=kb_main())
                    h,m,f=await subscribe_pair(key)
                    await tg_async(f"💱 Switched → `{state.pair_display}`\nH1:`{h}` M15:`{m}` M5:`{f}` ✅",reply_markup=kb_main())
                except Exception as e:
                    state.pair_key=old; await tg_async(f"❌ {e}",reply_markup=kb_main())
            else: await tg_async(f"💱 Pair → `{state.pair_display}` (next connect)",reply_markup=kb_main())

    elif cmd=="cmd_small_acc":
        if state.small_acc_mode:
            state.small_acc_mode=False; state.risk_pct=0.01
            state.tp1_r,state.tp2_r,state.tp3_r=2.,4.,6.
            await tg_async("💎 Small Acc *OFF* — 1% risk",reply_markup=kb_settings())
        else:
            state.small_acc_mode=True
            if state.pair_key=="XAUUSD":
                state.risk_pct=0.02; state.tp1_r,state.tp2_r,state.tp3_r=1.5,3.,5.; note="XAU 2%"
            else:
                state.pair_key="GBPUSD"; state.risk_pct=0.05
                state.tp1_r,state.tp2_r,state.tp3_r=1.5,3.,4.5; note="GBP/USD 5%"
            if state.ws:
                try: await subscribe_pair(state.pair_key)
                except Exception: pass
            await tg_async(f"💎 *Small Acc ON*\n{note}\n"
                           f"Partial close at TP1 ✅  Structural trailing after TP2 ✅",
                           reply_markup=kb_settings())

    elif cmd=="cmd_risk_1": state.risk_pct=0.01; state.small_acc_mode=False; await tg_async("✅ Risk→1%",reply_markup=kb_settings())
    elif cmd=="cmd_risk_3": state.risk_pct=0.03; state.small_acc_mode=False; await tg_async("✅ Risk→3%",reply_markup=kb_settings())
    elif cmd=="cmd_risk_5": state.risk_pct=0.05; state.small_acc_mode=False; await tg_async("✅ Risk→5%",reply_markup=kb_settings())

    elif cmd in("/stop","cmd_stop"):
        state.paused=True; n=await close_all()
        await tg_async(f"🛑 *Emergency Stop*\nClosed `{n}` contracts.",reply_markup=kb_main())

    elif cmd=="cmd_toggle_pause":
        if state.paused or state.block_trading:
            state.paused=False; state.block_trading=False; state.block_reason=""
            await tg_async("▶️ *RESUMED* — Sniper Brain active.",reply_markup=kb_main())
        else:
            state.paused=True; await tg_async("⏸ *PAUSED*",reply_markup=kb_main())

# ══════════════════════════════════════════════════════════════════════
# WS ENGINE  with heartbeat watchdog
# ══════════════════════════════════════════════════════════════════════
async def ws_heartbeat_watchdog():
    """Detect stale WS connection and force reconnect."""
    await asyncio.sleep(60)
    while state.running:
        if state.ws and (time.time()-state.last_ws_ping)>90:
            log.warning("WS heartbeat timeout — forcing reconnect")
            try: await state.ws.close()
            except Exception: pass
        await asyncio.sleep(30)

async def ws_reader(ws):
    async for raw in ws:
        try: await handle_msg(json.loads(raw))
        except Exception as e: log.error(f"Handler: {type(e).__name__}: {e}")

async def ws_run(ws):
    state.ws=ws; state.last_ws_ping=time.time()
    async def setup():
        await asyncio.sleep(0.1)
        log.info("Authorizing...")
        await authorize(); await get_balance()
        log.info(f"Bal:{state.account_balance} {state.account_currency} ({state.account_type})")
        h,m,f=await subscribe_pair(state.pair_key)
        # Re-sync any open contracts after reconnect
        await _resync_open_contracts()
        icon="🔴 REAL" if state.account_type=="real" else"🟢 DEMO"
        await tg_async(
            f"{market_header()}\n\n🤖 *SMC SNIPER EA v5.1 Online*\n"
            f"Broker:{icon}  `{state.account_id}`\n"
            f"Bal:`{state.account_balance:.2f} {state.account_currency}`\n"
            f"Pair:`{state.pair_display}`  sym:`{state.active_symbol}`\n"
            f"H1:`{h}` M15:`{m}` M5:`{f}` ✅\n"
            f"MaxSpread:`{state.max_spread}`  MinScore:`{MIN_SCORE}/100`\n"
            f"_Sniper Brain armed. Fetching news..._ 🎯",reply_markup=kb_main())
    task=asyncio.ensure_future(setup())
    try: await ws_reader(ws)
    finally:
        task.cancel()
        try: await task
        except(asyncio.CancelledError,Exception): pass

async def ws_loop():
    delay=5
    while state.running:
        try:
            log.info(f"Connecting: {DERIV_WS_BASE}")
            async with websockets.connect(
                DERIV_WS_BASE,ping_interval=25,ping_timeout=10,
                close_timeout=10,open_timeout=15) as ws:
                delay=5; await ws_run(ws)
        except websockets.ConnectionClosed as e: log.warning(f"WS closed: {e}")
        except asyncio.TimeoutError as e:        log.error(f"WS timeout: {e}")
        except Exception as e:
            log.error(f"WS: {type(e).__name__}: {e}")
            log.error(traceback.format_exc())
        finally:
            state.ws=None; state.broker_connected=False
            for fut in state.pending_reqs.values():
                if not fut.done(): fut.cancel()
            state.pending_reqs.clear()
        await asyncio.sleep(delay); delay=min(delay*2,60)

# ══════════════════════════════════════════════════════════════════════
# HEALTH SERVER
# ══════════════════════════════════════════════════════════════════════
async def health(req):
    wr=f"{state.wins/(state.wins+state.losses)*100:.1f}" if(state.wins+state.losses)>0 else"0"
    return web.json_response({
        "version":"5.1","status":"running" if state.running else"stopped",
        "paused":state.paused,"block_trading":state.block_trading,
        "block_reason":state.block_reason,"autonomous":state.autonomous,
        "market_open":is_market_open(),"session":get_session(),
        "broker_connected":state.broker_connected,
        "account_id":state.account_id,"account_type":state.account_type,
        "pair":state.pair_key,"symbol":state.active_symbol,
        "price":state.current_price,"spread":state.current_spread,
        "max_spread":state.max_spread,"trend":state.trend_bias,
        "zone":state.premium_discount,"session_now":state.session_now,
        "ob_score":state.ob_score,"atr_ok":state.atr_filter_ok,
        "min_score":MIN_SCORE,"balance":state.account_balance,
        "risk_pct":state.risk_pct,"small_acc":state.small_acc_mode,
        "trades":state.trade_count,"wins":state.wins,"losses":state.losses,
        "winrate":wr,"total_pnl":state.total_pnl,
        "open_contracts":len(state.open_contracts),"history":len(state.trade_history),
        "h1":len(state.h1_candles),"m15":len(state.m15_candles),"m5":len(state.m5_candles),
        "h1_maxlen":600,"m15_maxlen":600,
        "news_events":len(state.news_events),
        "next_red":state.next_red_event.title if state.next_red_event else None,
        "pre_news_block":state.pre_news_block//60,
        "post_news_wait":state.post_news_wait//60,
        "gran_actual":state.gran_actual,
        "last_ws_ping":round(time.time()-state.last_ws_ping),
    })

async def start_health():
    app=web.Application()
    app.router.add_get("/",health); app.router.add_get("/health",health)
    runner=web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner,"0.0.0.0",PORT).start()
    log.info(f"Health :{PORT}")

# ══════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════
async def main():
    log.info("╔═══════════════════════════════════════════════╗")
    log.info("║  SMC SNIPER EA v5.1  ·  Small Acc Growth Ed  ║")
    log.info("║  Spread Guard | Partial Close | Deep OB Scan  ║")
    log.info("║  Dynamic News Shield | Structural Trail Stop  ║")
    log.info("╚═══════════════════════════════════════════════╝")
    _load_saved_token()
    if not state.deriv_token:
        log.warning("No token — user must /connect via Telegram")
    await asyncio.gather(
        start_health(),
        ws_loop(),
        trading_loop(),
        tg_poll_loop(),
        chart_loop(),
        news_refresh_loop(),
        news_block_monitor(),
        ws_heartbeat_watchdog(),
    )

if __name__=="__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down…"); state.running=False
