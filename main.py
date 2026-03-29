"""
╔══════════════════════════════════════════════════════════════════╗
║   SMC ELITE EA  —  Multi-Pair Forex Bot  v3.0                   ║
║   Senior Quant SMC Logic | Deriv WebSocket | Railway-Ready      ║
║   Fix: Robust M15 candle fetching, separate history+subscribe   ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import logging
import os
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd
import requests
import websockets
from aiohttp import web

# ══════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("SMC-ELITE")

# ══════════════════════════════════════════════════════
# PAIR REGISTRY  (primary frx + OTC fallback)
# key → (primary_symbol, otc_symbol, pip_value, min_stake, display)
# ══════════════════════════════════════════════════════
PAIR_REGISTRY = {
    "XAUUSD": ("frxXAUUSD", "OTC_XAUUSD", 0.01,   1.0, "XAU/USD 🥇"),
    "EURUSD": ("frxEURUSD", "OTC_EURUSD", 0.0001, 1.0, "EUR/USD 🇪🇺"),
    "GBPUSD": ("frxGBPUSD", "OTC_GBPUSD", 0.0001, 1.0, "GBP/USD 🇬🇧"),
    "US100":  ("frxUS100",  "OTC_NDX",    0.1,    1.0, "NASDAQ 💻"),
}

# Deriv valid granularities (seconds).
# We try each in order until one returns data.
GRAN_FALLBACKS = {
    900:  [900, 600, 1800],   # M15 preferred → M10 → M30
    3600: [3600, 7200],       # H1  preferred → H2
    300:  [300, 180, 600],    # M5  preferred → M3 → M10
}

# ══════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════
DERIV_APP_ID     = os.getenv("DERIV_APP_ID", "1089")
DERIV_API_TOKEN  = os.getenv("DERIV_API_TOKEN", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PORT             = int(os.getenv("PORT", "8080"))
CHART_INTERVAL   = int(os.getenv("CHART_INTERVAL", "300"))
DERIV_WS_BASE    = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"

# ══════════════════════════════════════════════════════
# GLOBAL STATE
# ══════════════════════════════════════════════════════
class BotState:
    def __init__(self):
        self.running          = True
        self.paused           = False
        self.account_balance  = 0.0
        self.account_currency = "USD"

        self.pair_key         = "XAUUSD"
        self.active_symbol    = ""        # resolved symbol actually in use
        self.risk_pct         = 0.01
        self.tp1_r            = 2.0
        self.tp2_r            = 4.0
        self.tp3_r            = 6.0
        self.small_acc_mode   = False

        # Candle buffers — keyed by NOMINAL granularity (always 3600/900/300)
        self.h1_candles       = deque(maxlen=300)   # gran 3600
        self.m15_candles      = deque(maxlen=300)   # gran 900  (or fallback)
        self.m5_candles       = deque(maxlen=300)   # gran 300  (or fallback)

        # Map: actual_gran → nominal_gran (e.g. 1800→900 if M30 used as M15 slot)
        self.gran_actual       = {3600: 3600, 900: 900, 300: 300}

        self.current_price    = 0.0
        self.trend_bias       = "NEUTRAL"
        self.last_signal      = None
        self.active_ob        = None
        self.active_fvg       = None
        self.active_trap      = None
        self.active_idm       = None
        self.premium_discount = "NEUTRAL"
        self.ob_score         = 0

        self.ws               = None
        self.req_id           = 1
        self.pending_requests = {}
        self.subscribed_symbol = None

        self.open_contracts   = {}
        self.trade_count      = 0
        self.wins             = 0
        self.losses           = 0
        self.total_pnl        = 0.0
        self.trade_history    = []

    @property
    def pair_info(self):
        return PAIR_REGISTRY[self.pair_key]

    @property
    def pair_display(self):
        return self.pair_info[4]

    @property
    def pip_value(self):
        return self.pair_info[2]

state = BotState()

# ══════════════════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════════════════
def kb_main():
    return {"inline_keyboard": [
        [{"text":"💰 Balance",      "callback_data":"cmd_balance"},
         {"text":"📊 Chart",        "callback_data":"cmd_chart"}],
        [{"text":"⚡ Status",       "callback_data":"cmd_status"},
         {"text":"📋 History",      "callback_data":"cmd_history"}],
        [{"text":"⚙️ Settings",     "callback_data":"cmd_settings"},
         {"text":"🛑 Stop",         "callback_data":"cmd_stop"}],
        [{"text":"▶️ Resume",       "callback_data":"cmd_resume"}],
    ]}

def kb_settings():
    r  = state.risk_pct * 100
    sm = "✅ ON" if state.small_acc_mode else "OFF"
    return {"inline_keyboard": [
        [{"text":"💱 Select Pair",         "callback_data":"cmd_pair_menu"}],
        [{"text":f"💎 Small Acc ($10) [{sm}]","callback_data":"cmd_small_acc"}],
        [{"text":f"{'✅' if r==1 else ''}1% Risk",  "callback_data":"cmd_risk_1"},
         {"text":f"{'✅' if r==3 else ''}3% Risk",  "callback_data":"cmd_risk_3"},
         {"text":f"{'✅' if r==5 else ''}5% Risk",  "callback_data":"cmd_risk_5"}],
        [{"text":"⬅️ Back",                "callback_data":"cmd_back"}],
    ]}

def kb_pair_menu():
    rows = []
    row  = []
    for key, info in PAIR_REGISTRY.items():
        tick = "✅ " if key == state.pair_key else ""
        row.append({"text": tick + info[4], "callback_data": f"cmd_pair_{key}"})
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([{"text":"⬅️ Back","callback_data":"cmd_settings"}])
    return {"inline_keyboard": rows}

# ══════════════════════════════════════════════════════
# TELEGRAM HELPERS
# ══════════════════════════════════════════════════════
def tg_send(text: str, photo_path: str = None, reply_markup=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    base   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    markup = reply_markup if reply_markup is not None else kb_main()
    try:
        if photo_path:
            with open(photo_path, "rb") as fh:
                r = requests.post(f"{base}/sendPhoto", data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "caption": text[:1024],
                    "reply_markup": json.dumps(markup),
                    "parse_mode": "Markdown",
                }, files={"photo": fh}, timeout=20)
        else:
            r = requests.post(f"{base}/sendMessage", json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "reply_markup": markup,
                "parse_mode": "Markdown",
            }, timeout=10)
        if r.status_code not in (200, 201):
            log.warning(f"TG {r.status_code}: {r.text[:150]}")
    except Exception as e:
        log.error(f"tg_send: {e}")

def tg_answer_callback(cqid: str, text: str = ""):
    if not TELEGRAM_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": cqid, "text": text}, timeout=5)
    except Exception:
        pass

async def tg_async(text: str, photo_path: str = None, reply_markup=None):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: tg_send(text, photo_path, reply_markup))

# ══════════════════════════════════════════════════════
# CHART ENGINE
# ══════════════════════════════════════════════════════
CHART_BG  = "#0d1117"
PANEL_BG  = "#161b22"
BULL_C    = "#00e676"
BEAR_C    = "#ff1744"
OB_BULL_C = "#00bcd4"
OB_BEAR_C = "#ff9800"
FVG_C     = "#ce93d8"
TRAP_C    = "#ffeb3b"
IDM_C     = "#80cbc4"
ENTRY_C   = "#2979ff"
SL_C      = "#f44336"
TP_CS     = ["#69f0ae", "#40c4ff", "#b388ff"]
TEXT_C    = "#cdd9e5"
GRID_C    = "#1e2a38"

def _ax_style(ax):
    ax.set_facecolor(PANEL_BG)
    ax.tick_params(colors="#90a4ae", labelsize=7)
    for s in ax.spines.values():
        s.set_edgecolor(GRID_C)
    ax.grid(axis="y", color=GRID_C, linewidth=0.4, alpha=0.6)

def _calc_rsi(prices: np.ndarray, p: int = 14) -> np.ndarray:
    if len(prices) < p + 1:
        return np.full(len(prices), 50.0)
    d  = np.diff(prices)
    g  = np.where(d > 0, d, 0.0)
    l  = np.where(d < 0, -d, 0.0)
    ag = np.convolve(g, np.ones(p)/p, mode="valid")
    al = np.convolve(l, np.ones(p)/p, mode="valid")
    rs = np.where(al != 0, ag/al, 100.0)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    return np.concatenate([np.full(len(prices)-len(rsi), 50.0), rsi])

def generate_chart(
    candles: deque,
    timeframe: str = "M15",
    entry_price: float = None,
    exit_price: float = None,
    direction: str = None,
    pnl: float = None,
    chart_type: str = "live",
) -> Optional[str]:

    if len(candles) < 20:
        return None

    df = pd.DataFrame(list(candles)[-80:])
    df.columns = ["time","open","high","low","close"]
    df.reset_index(drop=True, inplace=True)

    fig = plt.figure(figsize=(16, 9), facecolor=CHART_BG)
    gs  = gridspec.GridSpec(4, 1, figure=fig, hspace=0.05,
                            height_ratios=[4, 0.6, 0.6, 0.5])
    ax_m = fig.add_subplot(gs[0])
    ax_v = fig.add_subplot(gs[1], sharex=ax_m)
    ax_r = fig.add_subplot(gs[2], sharex=ax_m)
    ax_l = fig.add_subplot(gs[3])
    for ax in (ax_m, ax_v, ax_r, ax_l):
        _ax_style(ax)

    # Candles
    for i, row in df.iterrows():
        c  = BULL_C if row["close"] >= row["open"] else BEAR_C
        bl = min(row["open"], row["close"])
        bh = max(row["open"], row["close"])
        ax_m.plot([i, i], [row["low"], row["high"]], color=c, lw=0.8, alpha=0.9)
        ax_m.add_patch(mpatches.FancyBboxPatch(
            (i-0.35, bl), 0.7, max(bh-bl, df["close"].mean()*0.00005),
            boxstyle="square,pad=0", fc=c, ec=c, alpha=0.85))

    # Volume
    for i, row in df.iterrows():
        c = BULL_C if row["close"] >= row["open"] else BEAR_C
        ax_v.bar(i, 1, color=c, alpha=0.4, width=0.7)
    ax_v.set_ylabel("Vol", color="#555d68", fontsize=6)

    # RSI
    rsi = _calc_rsi(df["close"].values, 14)
    ax_r.plot(range(len(rsi)), rsi, color="#90a4ae", lw=0.9)
    ax_r.axhline(70, color=BEAR_C, lw=0.5, ls="--", alpha=0.5)
    ax_r.axhline(30, color=BULL_C, lw=0.5, ls="--", alpha=0.5)
    ax_r.set_ylim(0, 100)
    ax_r.set_ylabel("RSI", color="#555d68", fontsize=6)

    # Order Block
    if state.active_ob:
        ob = state.active_ob
        oc = OB_BULL_C if ob["type"]=="BULL" else OB_BEAR_C
        xs = max(0, len(df)-35) / len(df)
        ax_m.axhspan(ob["low"], ob["high"], xmin=xs, alpha=0.15, color=oc)
        ax_m.axhline(ob["high"], color=oc, ls="--", lw=0.8, alpha=0.7)
        ax_m.axhline(ob["low"],  color=oc, ls="--", lw=0.8, alpha=0.7)
        ax_m.text(2, ob["high"], f" {ob['type']} OB  score:{state.ob_score}",
                  color=oc, fontsize=7, va="bottom", fontfamily="monospace")

    # FVG
    if state.active_fvg:
        fvg = state.active_fvg
        ax_m.axhspan(fvg["low"], fvg["high"], alpha=0.13, color=FVG_C)
        ax_m.text(2, fvg["high"], "  FVG", color=FVG_C,
                  fontsize=7, va="bottom", fontfamily="monospace")

    # IDM
    if state.active_idm:
        idm = state.active_idm
        swept = " ✅" if idm.get("swept") else " ⏳"
        ax_m.axhline(idm["level"], color=IDM_C, ls=":", lw=1.2, alpha=0.9)
        ax_m.text(2, idm["level"], f"  IDM{swept}",
                  color=IDM_C, fontsize=7, va="bottom", fontfamily="monospace")

    # Trap
    if state.active_trap:
        trap = state.active_trap
        ax_m.axhline(trap["level"], color=TRAP_C, ls=":", lw=1.4, alpha=0.9)
        ax_m.text(2, trap["level"], f"  TRAP {trap['side']}",
                  color=TRAP_C, fontsize=7, va="bottom", fontfamily="monospace")

    # Fib 0.5
    if state.last_signal and "fib_hi" in state.last_signal:
        sig = state.last_signal
        mid = (sig["fib_hi"] + sig["fib_lo"]) / 2
        ax_m.axhline(mid, color="#78909c", ls="-.", lw=0.6, alpha=0.5)
        ax_m.text(len(df)-2, mid, " 0.5", color="#78909c",
                  fontsize=6, ha="right", fontfamily="monospace")
        ax_m.axhspan(sig["fib_lo"], mid,       alpha=0.04, color=BULL_C)
        ax_m.axhspan(mid, sig["fib_hi"],        alpha=0.04, color=BEAR_C)

    # Signal lines
    if state.last_signal:
        sig = state.last_signal
        ax_m.axhline(sig["entry"], color=ENTRY_C, lw=1.6, ls="-")
        ax_m.axhline(sig["sl"],    color=SL_C,    lw=1.0, ls="--")
        for tp_k, tp_c in zip(["tp1","tp2","tp3"], TP_CS):
            if tp_k in sig:
                ax_m.axhline(sig[tp_k], color=tp_c, lw=0.8, ls="-.")

    # Entry/exit overlays
    if entry_price:
        ax_m.axhline(entry_price, color=ENTRY_C, lw=2.2, alpha=0.9)
        ax_m.annotate(f"▶ ENTRY {entry_price:.5f}",
                      xy=(len(df)-1, entry_price), color=ENTRY_C,
                      fontsize=8, ha="right", fontfamily="monospace")
    if exit_price:
        ec = BULL_C if (pnl and pnl > 0) else BEAR_C
        ax_m.axhline(exit_price, color=ec, lw=2.0, ls="--", alpha=0.9)
        ps = f"+{pnl:.2f}" if pnl and pnl > 0 else f"{pnl:.2f}"
        ax_m.annotate(f"◀ EXIT {exit_price:.5f}  P&L:{ps}",
                      xy=(len(df)-1, exit_price), color=ec,
                      fontsize=8, ha="right", fontfamily="monospace")

    # Swing markers
    swh, swl = detect_swing_points(df)
    for i in swh:
        ax_m.plot(i, df.iloc[i]["high"]*1.00015, "^", color=BULL_C, ms=4, alpha=0.5)
    for i in swl:
        ax_m.plot(i, df.iloc[i]["low"]*0.99985,  "v", color=BEAR_C, ms=4, alpha=0.5)

    tc = BULL_C if state.trend_bias=="BULLISH" else (BEAR_C if state.trend_bias=="BEARISH" else "#90a4ae")
    tl = {"live":"📡 LIVE","entry":"🎯 ENTRY","exit":"🏁 CLOSED"}.get(chart_type,"")
    actual_tf = timeframe
    # show actual granularity used if different
    nom_to_actual = {v: k for k, v in state.gran_actual.items()}
    ax_m.set_title(
        f"{tl}  {state.pair_display}  ·  {actual_tf}  "
        f"·  Bias:{state.trend_bias}  Zone:{state.premium_discount}  "
        f"·  {state.current_price:.5f}",
        color=tc, fontsize=10, fontfamily="monospace", pad=8)
    ax_m.set_ylabel("Price", color="#90a4ae", fontsize=8)

    ax_l.set_xlim(0,1); ax_l.set_ylim(0,1); ax_l.axis("off")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ax_l.text(0.01, 0.5,
        f"SMC ELITE EA  ·  {state.pair_display}  sym:{state.active_symbol}  "
        f"Bal:{state.account_balance:.2f} {state.account_currency}  "
        f"Risk:{state.risk_pct*100:.0f}%  OB:{state.ob_score}/100  "
        f"H1:{len(state.h1_candles)} M15:{len(state.m15_candles)} M5:{len(state.m5_candles)}  "
        f"{ts}",
        color="#555d68", fontsize=6, va="center", fontfamily="monospace")

    plt.setp(ax_m.get_xticklabels(), visible=False)
    plt.setp(ax_v.get_xticklabels(), visible=False)
    plt.setp(ax_r.get_xticklabels(), visible=False)

    path = f"/tmp/smc_chart_{chart_type}.png"
    plt.tight_layout()
    plt.savefig(path, dpi=130, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return path

def generate_history_chart() -> Optional[str]:
    history = state.trade_history[-20:]
    if not history:
        return None
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9), facecolor=CHART_BG,
                                    gridspec_kw={"height_ratios":[2,1]})
    for ax in (ax1, ax2):
        _ax_style(ax)
    labels = [f"#{t['num']}" for t in history]
    pnls   = [t["pnl"] for t in history]
    colors = [BULL_C if p>0 else BEAR_C for p in pnls]
    bars   = ax1.bar(labels, pnls, color=colors, alpha=0.85, ec=GRID_C)
    ax1.axhline(0, color=GRID_C, lw=0.8)
    for bar, val in zip(bars, pnls):
        s = "+" if val>=0 else ""
        ax1.text(bar.get_x()+bar.get_width()/2,
                 bar.get_height()+(0.05 if val>=0 else -0.15),
                 f"{s}{val:.2f}", ha="center", va="bottom",
                 color=TEXT_C, fontsize=7, fontfamily="monospace")
    wr = f"{state.wins/(state.wins+state.losses)*100:.1f}%" if (state.wins+state.losses)>0 else "N/A"
    ax1.set_title(
        f"📋 History  {state.wins}W/{state.losses}L  WR:{wr}  "
        f"P&L:{state.total_pnl:+.2f} {state.account_currency}  Bal:{state.account_balance:.2f}",
        color=TEXT_C, fontsize=10, fontfamily="monospace")
    ax1.set_ylabel("P&L", color="#90a4ae", fontsize=9)
    cum = np.cumsum(pnls)
    cc  = BULL_C if cum[-1]>=0 else BEAR_C
    ax2.plot(labels, cum, color=cc, lw=1.8, marker="o", ms=4)
    ax2.fill_between(labels, cum, alpha=0.12, color=cc)
    ax2.axhline(0, color=GRID_C, lw=0.8)
    ax2.set_ylabel("Cumulative", color="#90a4ae", fontsize=8)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fig.text(0.99, 0.01, f"SMC ELITE · {ts}", color="#444d56", fontsize=7, ha="right")
    path = "/tmp/smc_history.png"
    plt.tight_layout()
    plt.savefig(path, dpi=130, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return path

# ══════════════════════════════════════════════════════
# SMC ENGINE
# ══════════════════════════════════════════════════════
def detect_swing_points(df: pd.DataFrame, n: int = 5):
    highs, lows = [], []
    for i in range(n, len(df)-n):
        if df["high"].iloc[i] == df["high"].iloc[i-n:i+n+1].max():
            highs.append(i)
        if df["low"].iloc[i] == df["low"].iloc[i-n:i+n+1].min():
            lows.append(i)
    return highs, lows

def detect_bos_choch(df: pd.DataFrame):
    highs, lows = detect_swing_points(df)
    if len(highs)<2 or len(lows)<2:
        return None
    lsh, psh = highs[-1], highs[-2]
    lsl, psl = lows[-1],  lows[-2]
    lc = df["close"].iloc[-1]
    if lc > df["high"].iloc[lsh] and lsh > psh:
        return {"type":"BOS","direction":"BULLISH","level":df["high"].iloc[lsh]}
    if lc < df["low"].iloc[lsl] and lsl > psl:
        return {"type":"BOS","direction":"BEARISH","level":df["low"].iloc[lsl]}
    if df["high"].iloc[lsh] < df["high"].iloc[psh] and lc > df["high"].iloc[lsh]:
        return {"type":"CHoCH","direction":"BULLISH","level":df["high"].iloc[lsh]}
    if df["low"].iloc[lsl] > df["low"].iloc[psl] and lc < df["low"].iloc[lsl]:
        return {"type":"CHoCH","direction":"BEARISH","level":df["low"].iloc[lsl]}
    return None

def detect_inducement(df: pd.DataFrame, direction: str) -> Optional[dict]:
    highs, lows = detect_swing_points(df, n=3)
    if direction == "BULLISH" and len(lows) >= 2:
        lvl  = df["low"].iloc[lows[-2]]
        last = df.iloc[-1]
        return {"side":"BUY","level":lvl,"swept": last["low"]<lvl and last["close"]>lvl}
    if direction == "BEARISH" and len(highs) >= 2:
        lvl  = df["high"].iloc[highs[-2]]
        last = df.iloc[-1]
        return {"side":"SELL","level":lvl,"swept": last["high"]>lvl and last["close"]<lvl}
    return None

def detect_equal_hl(df: pd.DataFrame) -> Optional[dict]:
    tol    = 0.0003 if state.pair_key in ("EURUSD","GBPUSD") else 0.0005
    recent = df.iloc[-25:]
    hs     = recent["high"].values
    ls     = recent["low"].values
    for i in range(len(hs)-1, 1, -1):
        for j in range(i-1, max(i-8,0), -1):
            if abs(hs[i]-hs[j])/hs[j] < tol:
                lvl  = (hs[i]+hs[j])/2
                last = df.iloc[-1]
                if last["high"]>lvl and last["close"]<lvl:
                    return {"side":"SELL","level":lvl,"swept":True,"type":"EQL_HIGHS"}
    for i in range(len(ls)-1, 1, -1):
        for j in range(i-1, max(i-8,0), -1):
            if abs(ls[i]-ls[j])/ls[j] < tol:
                lvl  = (ls[i]+ls[j])/2
                last = df.iloc[-1]
                if last["low"]<lvl and last["close"]>lvl:
                    return {"side":"BUY","level":lvl,"swept":True,"type":"EQL_LOWS"}
    return None

def identify_ob(df: pd.DataFrame, direction: str) -> Optional[dict]:
    lookback = min(30, len(df)-3)
    recent   = df.iloc[-lookback:].reset_index(drop=True)
    avg_body = (recent["close"]-recent["open"]).abs().mean()
    if direction == "BULLISH":
        for i in range(len(recent)-3, 1, -1):
            c, nc = recent.iloc[i], recent.iloc[i+1]
            if (c["close"]<c["open"] and nc["close"]>c["high"] and
                    abs(nc["close"]-nc["open"]) > avg_body*1.5):
                return {"type":"BULL","high":c["high"],"low":c["low"],
                        "body_hi":max(c["open"],c["close"]),
                        "body_lo":min(c["open"],c["close"]),
                        "displacement":round(abs(nc["close"]-nc["open"])/avg_body,2)}
    elif direction == "BEARISH":
        for i in range(len(recent)-3, 1, -1):
            c, nc = recent.iloc[i], recent.iloc[i+1]
            if (c["close"]>c["open"] and nc["close"]<c["low"] and
                    abs(nc["close"]-nc["open"]) > avg_body*1.5):
                return {"type":"BEAR","high":c["high"],"low":c["low"],
                        "body_hi":max(c["open"],c["close"]),
                        "body_lo":min(c["open"],c["close"]),
                        "displacement":round(abs(nc["close"]-nc["open"])/avg_body,2)}
    return None

def identify_fvg(df: pd.DataFrame, ob: dict) -> Optional[dict]:
    if ob is None:
        return None
    thr    = 0.03 if state.pair_key in ("EURUSD","GBPUSD") else 0.05
    recent = df.iloc[-min(25, len(df)-3):].reset_index(drop=True)
    if ob["type"] == "BULL":
        for i in range(len(recent)-3, 0, -1):
            c1, c3  = recent.iloc[i], recent.iloc[i+2]
            gp = (c3["low"]-c1["high"])/c1["high"]*100
            if c1["high"]<c3["low"] and gp>=thr:
                return {"type":"BULL","high":c3["low"],"low":c1["high"],"gap_pct":gp}
    elif ob["type"] == "BEAR":
        for i in range(len(recent)-3, 0, -1):
            c1, c3  = recent.iloc[i], recent.iloc[i+2]
            gp = (c1["low"]-c3["high"])/c1["low"]*100
            if c1["low"]>c3["high"] and gp>=thr:
                return {"type":"BEAR","high":c1["low"],"low":c3["high"],"gap_pct":gp}
    return None

def score_ob(ob, fvg, trap, idm, rsi) -> int:
    s = 10
    if fvg:  s += 25
    if trap and trap.get("swept"):  s += 20
    if idm  and idm.get("swept"):   s += 20
    if ob and ob.get("displacement",0) >= 2.0:  s += 15
    if ob:
        if ob["type"]=="BULL" and rsi<45: s += 10
        if ob["type"]=="BEAR" and rsi>55: s += 10
    return min(s, 100)

def calc_pd_zone(df: pd.DataFrame):
    highs, lows = detect_swing_points(df, n=8)
    if not highs or not lows:
        return "NEUTRAL", 0, 0
    fh = df["high"].iloc[highs[-1]]
    fl = df["low"].iloc[lows[-1]]
    if fh == fl:
        return "NEUTRAL", fh, fl
    pct = (state.current_price - fl) / (fh - fl)
    return ("DISCOUNT" if pct<0.5 else "PREMIUM"), fh, fl

def analyze_h1_trend():
    if len(state.h1_candles) < 30:
        return "NEUTRAL"
    df = pd.DataFrame(list(state.h1_candles))
    df.columns = ["time","open","high","low","close"]
    r = detect_bos_choch(df)
    if r:
        state.trend_bias = r["direction"]
    else:
        c = df["close"].values[-20:]
        state.trend_bias = "BULLISH" if np.polyfit(np.arange(len(c)),c,1)[0]>0 else "BEARISH"
    return state.trend_bias

def compute_signal(timeframe="M15") -> Optional[dict]:
    buf = state.m15_candles if timeframe=="M15" else state.m5_candles
    if len(buf) < 40:
        return None
    df = pd.DataFrame(list(buf))
    df.columns = ["time","open","high","low","close"]

    bias = analyze_h1_trend()
    if bias == "NEUTRAL":
        return None

    struct = detect_bos_choch(df)
    if struct is None or struct["direction"] != bias:
        return None

    pd_zone, fib_hi, fib_lo = calc_pd_zone(df)
    state.premium_discount = pd_zone
    if bias=="BULLISH" and pd_zone!="DISCOUNT":
        return None
    if bias=="BEARISH" and pd_zone!="PREMIUM":
        return None

    idm = detect_inducement(df, bias)
    state.active_idm = idm
    if idm is None or not idm.get("swept"):
        return None

    trap = detect_equal_hl(df)
    state.active_trap = trap
    if trap is None or not trap.get("swept"):
        return None
    if trap["side"] != ("BUY" if bias=="BULLISH" else "SELL"):
        return None

    ob = identify_ob(df, bias)
    if ob is None:
        return None
    state.active_ob = ob

    fvg = identify_fvg(df, ob)
    state.active_fvg = fvg

    rsi_now = float(_calc_rsi(df["close"].values, 14)[-1])
    sc = score_ob(ob, fvg, trap, idm, rsi_now)
    state.ob_score = sc
    if sc < (45 if state.small_acc_mode else 55):
        log.info(f"OB score too low: {sc}")
        return None

    if bias == "BULLISH":
        entry = ob["body_hi"]
        sl    = ob["low"] * 0.9995
    else:
        entry = ob["body_lo"]
        sl    = ob["high"] * 1.0005

    risk = abs(entry - sl)
    if risk == 0:
        return None
    mult  = 1 if bias=="BULLISH" else -1
    tp1   = entry + risk*state.tp1_r*mult
    tp2   = entry + risk*state.tp2_r*mult
    tp3   = entry + risk*state.tp3_r*mult
    stake = max(1.0, round(max(
        PAIR_REGISTRY[state.pair_key][3],
        state.account_balance*state.risk_pct), 2))

    logic = (
        f"{timeframe} {struct['type']} {bias} | "
        f"IDM✅ | {trap['type']}✅ | "
        f"{ob['type']}OB(sc:{sc}) | "
        f"Zone:{pd_zone} | RSI:{rsi_now:.1f}"
    )
    sig = {
        "direction":"BUY" if bias=="BULLISH" else "SELL",
        "entry":round(entry,5),"sl":round(sl,5),
        "tp1":round(tp1,5),"tp2":round(tp2,5),"tp3":round(tp3,5),
        "risk_r":round(risk,5),"stake":stake,
        "struct":struct["type"],"ob":ob,"fvg":fvg,
        "trap":trap,"idm":idm,"ob_score":sc,"rsi":rsi_now,
        "pd_zone":pd_zone,"fib_hi":fib_hi,"fib_lo":fib_lo,
        "tf":timeframe,"bias":bias,"logic":logic,
        "ts":datetime.now(timezone.utc).isoformat(),
    }
    state.last_signal = sig
    return sig

def check_trade_mgmt():
    for cid, info in list(state.open_contracts.items()):
        sig = info.get("signal")
        if not sig:
            continue
        p, d = state.current_price, info["direction"]
        tp1  = sig["tp1"]
        if not info["be_moved"]:
            if (d=="BUY" and p>=tp1) or (d=="SELL" and p<=tp1):
                info["be_moved"] = True
                sig["sl"] = sig["entry"]
                asyncio.ensure_future(tg_async(
                    f"✅ *BreakEven* `{cid}`\nSL → entry `{sig['entry']}`"))
        if len(state.m15_candles) >= 10:
            df = pd.DataFrame(list(state.m15_candles)[-30:])
            df.columns = ["time","open","high","low","close"]
            swh, swl = detect_swing_points(df, n=3)
            if d=="BUY" and swl:
                t_sl = df["low"].iloc[swl[-1]]*0.9998
                if t_sl > sig["sl"]: sig["sl"] = t_sl
            elif d=="SELL" and swh:
                t_sl = df["high"].iloc[swh[-1]]*1.0002
                if t_sl < sig["sl"]: sig["sl"] = t_sl

# ══════════════════════════════════════════════════════
# DERIV WEBSOCKET
# ══════════════════════════════════════════════════════
async def send_request(payload: dict) -> dict:
    if state.ws is None:
        raise RuntimeError("WebSocket not connected")
    req_id = state.req_id
    state.req_id += 1
    payload["req_id"] = req_id
    future = asyncio.get_event_loop().create_future()
    state.pending_requests[req_id] = future
    await state.ws.send(json.dumps(payload))
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=20)
    except asyncio.TimeoutError:
        state.pending_requests.pop(req_id, None)
        raise asyncio.TimeoutError(f"Timeout {list(payload.keys())}")

async def authorize():
    resp = await send_request({"authorize": DERIV_API_TOKEN})
    if "error" in resp:
        raise RuntimeError(f"Auth: {resp['error']['message']}")
    log.info(f"Authorized: {resp['authorize']['loginid']}")
    return resp["authorize"]

async def get_balance():
    resp = await send_request({"balance": 1, "subscribe": 0})
    if "balance" in resp:
        state.account_balance  = resp["balance"]["balance"]
        state.account_currency = resp["balance"]["currency"]

# ── Core fix: fetch history & subscribe separately, with gran fallbacks ──
async def _fetch_candles(symbol: str, nominal_gran: int) -> int:
    """
    Fetch historical candles for one timeframe.
    Tries multiple granularities until one succeeds.
    Returns number of candles loaded.
    """
    gran_label = {3600:"H1", 900:"M15", 300:"M5"}
    candidates = GRAN_FALLBACKS.get(nominal_gran, [nominal_gran])

    for actual_gran in candidates:
        try:
            # Step 1: fetch history (no subscribe) — guaranteed to return candles
            resp = await send_request({
                "ticks_history": symbol,
                "end":           "latest",
                "count":         200,
                "granularity":   actual_gran,
                "style":         "candles",
                # NO subscribe:1 here — avoids the silent-failure bug
            })

            if "error" in resp:
                err = resp["error"]
                log.warning(
                    f"History fetch failed: sym={symbol} gran={actual_gran} "
                    f"→ {err.get('message','?')} ({err.get('code','?')})"
                )
                continue

            raw = resp.get("candles", [])
            if not raw:
                log.warning(f"Empty candles: sym={symbol} gran={actual_gran}")
                continue

            rows = [(int(c["epoch"]), float(c["open"]), float(c["high"]),
                     float(c["low"]),  float(c["close"])) for c in raw]

            # Store with nominal gran so _update_candles routes correctly
            state.gran_actual[nominal_gran] = actual_gran
            _store_candles(nominal_gran, rows)
            log.info(
                f"✅ Loaded {len(rows)} {gran_label.get(nominal_gran,'?')} "
                f"candles (gran={actual_gran}) for {symbol}"
            )

            # Step 2: subscribe for live updates (separate request, ignore response)
            try:
                asyncio.ensure_future(send_request({
                    "ticks_history": symbol,
                    "end":           "latest",
                    "count":         1,
                    "granularity":   actual_gran,
                    "style":         "candles",
                    "subscribe":     1,
                }))
            except Exception:
                pass

            return len(rows)

        except asyncio.TimeoutError:
            log.warning(f"Timeout fetching gran={actual_gran} for {symbol}")
            continue
        except Exception as e:
            log.error(f"_fetch_candles gran={actual_gran}: {type(e).__name__}: {e}")
            continue

    log.error(f"All granularity attempts failed for nom_gran={nominal_gran} sym={symbol}")
    return 0

def _store_candles(nominal_gran: int, rows: list):
    """Store candle rows into the correct buffer by nominal gran."""
    if nominal_gran == 3600:
        state.h1_candles.extend(rows)
    elif nominal_gran == 900:
        state.m15_candles.extend(rows)
        if rows:
            state.current_price = float(rows[-1][4])
    elif nominal_gran == 300:
        state.m5_candles.extend(rows)
        if rows:
            state.current_price = float(rows[-1][4])

async def _resolve_symbol(pair_key: str) -> str:
    """Test primary symbol, fall back to OTC if rejected."""
    info     = PAIR_REGISTRY[pair_key]
    primary  = info[0]
    fallback = info[1]
    for sym in [primary, fallback]:
        try:
            resp = await send_request({
                "ticks_history": sym, "end": "latest",
                "count": 1, "granularity": 3600, "style": "candles",
            })
            if "candles" in resp:
                log.info(f"Symbol resolved: {sym} ✅")
                return sym
            err = resp.get("error",{})
            log.warning(f"Symbol {sym} rejected: {err.get('message','?')}")
        except Exception as e:
            log.warning(f"Symbol test {sym}: {e}")
    log.error(f"No valid symbol for {pair_key}, using primary {primary}")
    return primary

async def subscribe_pair(pair_key: str):
    """Resolve symbol, fetch all timeframes, update state."""
    # Clear all buffers
    state.h1_candles.clear()
    state.m15_candles.clear()
    state.m5_candles.clear()
    state.last_signal      = None
    state.active_ob        = None
    state.active_fvg       = None
    state.active_trap      = None
    state.active_idm       = None
    state.current_price    = 0.0
    state.trend_bias       = "NEUTRAL"
    state.ob_score         = 0
    state.premium_discount = "NEUTRAL"

    sym = await _resolve_symbol(pair_key)
    state.active_symbol    = sym
    state.subscribed_symbol = sym

    # Fetch all three timeframes
    h1_count  = await _fetch_candles(sym, 3600)
    m15_count = await _fetch_candles(sym, 900)
    m5_count  = await _fetch_candles(sym, 300)

    log.info(
        f"subscribe_pair done: {sym} | "
        f"H1:{h1_count} M15:{m15_count} M5:{m5_count}"
    )
    return h1_count, m15_count, m5_count

async def open_contract(direction: str, amount: float):
    if state.paused:
        return None
    ct = "MULTUP" if direction=="BUY" else "MULTDOWN"
    try:
        resp = await send_request({
            "buy": 1, "price": round(amount,2),
            "parameters": {
                "contract_type": ct,
                "symbol": state.active_symbol or PAIR_REGISTRY[state.pair_key][0],
                "amount": round(amount,2),
                "currency": state.account_currency,
                "multiplier": 10, "basis": "stake", "stop_out": 1,
            },
        })
        if "error" in resp:
            log.error(f"Buy: {resp['error']['message']}")
            return None
        cid = resp["buy"]["contract_id"]
        state.open_contracts[cid] = {
            "direction": direction, "entry": state.current_price,
            "amount": amount, "signal": state.last_signal,
            "be_moved": False, "opened_at": time.time(),
        }
        state.trade_count += 1
        log.info(f"✅ Opened {cid} [{direction}] ${amount:.2f}")
        asyncio.ensure_future(send_request({
            "proposal_open_contract":1,"contract_id":cid,"subscribe":1}))
        return cid
    except Exception as e:
        log.error(f"open_contract: {e}")
        return None

async def close_contract(cid: str):
    try:
        resp = await send_request({"sell": cid, "price": 0})
        if "error" in resp:
            log.error(f"Close: {resp['error']['message']}")
            return False
        state.open_contracts.pop(cid, None)
        log.info(f"🔴 Closed {cid}")
        return True
    except Exception as e:
        log.error(f"close_contract: {e}")
        return False

async def close_all():
    ids = list(state.open_contracts.keys())
    for cid in ids:
        await close_contract(cid)
    return len(ids)

# ══════════════════════════════════════════════════════
# MESSAGE HANDLER
# ══════════════════════════════════════════════════════
def _update_candles(actual_gran: int, rows: list):
    """Map actual_gran → nominal_gran → correct buffer."""
    # Build reverse map: actual → nominal
    rev = {v: k for k, v in state.gran_actual.items()}
    nominal = rev.get(actual_gran, actual_gran)
    _store_candles(nominal, rows)

async def handle_message(msg: dict):
    req_id = msg.get("req_id")
    if req_id and req_id in state.pending_requests:
        fut = state.pending_requests.pop(req_id)
        if not fut.done():
            fut.set_result(msg)
        return

    mtype = msg.get("msg_type","")

    if mtype == "ohlc":
        c    = msg["ohlc"]
        gran = int(c.get("granularity", 0))
        _update_candles(gran, [(
            int(c["epoch"]), float(c["open"]), float(c["high"]),
            float(c["low"]),  float(c["close"]))])

    elif mtype == "candles":
        gran = int(msg.get("echo_req",{}).get("granularity", 0))
        rows = [(int(c["epoch"]), float(c["open"]), float(c["high"]),
                 float(c["low"]),  float(c["close"]))
                for c in msg.get("candles",[])]
        if rows:
            _update_candles(gran, rows)

    elif mtype == "tick":
        state.current_price = float(msg["tick"]["quote"])

    elif mtype == "proposal_open_contract":
        poc    = msg.get("proposal_open_contract",{})
        cid    = str(poc.get("contract_id",""))
        if cid not in state.open_contracts:
            return
        profit = float(poc.get("profit",0))
        status = poc.get("status","")
        exit_s = float(poc.get("exit_tick", state.current_price) or state.current_price)
        if status in ("sold","expired"):
            info = state.open_contracts.pop(cid,{})
            if profit>0: state.wins+=1
            else:        state.losses+=1
            state.total_pnl += profit
            tnum = state.trade_count
            state.trade_history.append({
                "num": tnum, "id": cid, "pair": state.pair_display,
                "direction": info.get("direction","?"),
                "entry": info.get("entry",0), "exit": exit_s,
                "pnl": round(profit,2), "win": profit>0,
                "ts": datetime.now(timezone.utc).strftime("%m/%d %H:%M"),
            })
            if len(state.trade_history) > 50:
                state.trade_history.pop(0)
            chart = generate_chart(
                state.m15_candles,"M15",
                entry_price=info.get("entry"),
                exit_price=exit_s,
                direction=info.get("direction"),
                pnl=profit, chart_type="exit")
            sign = "+" if profit>0 else ""
            asyncio.ensure_future(tg_async(
                f"{'✅ WIN' if profit>0 else '❌ LOSS'}  `#{tnum}`\n\n"
                f"Pair: `{state.pair_display}`\n"
                f"Dir:  `{info.get('direction','?')}`\n"
                f"Entry:`{info.get('entry',0):.5f}`  Exit:`{exit_s:.5f}`\n"
                f"P&L:  `{sign}{profit:.2f} {state.account_currency}`\n"
                f"W/L:{state.wins}/{state.losses}  "
                f"P&L:{state.total_pnl:+.2f}  Bal:{state.account_balance:.2f}",
                photo_path=chart))

    elif mtype == "balance":
        state.account_balance  = msg["balance"]["balance"]
        state.account_currency = msg["balance"]["currency"]

    elif "error" in msg:
        log.warning(f"WS API: {msg['error'].get('message','?')}")

# ══════════════════════════════════════════════════════
# AUTO CHART BROADCAST
# ══════════════════════════════════════════════════════
async def chart_broadcast_loop():
    await asyncio.sleep(45)
    while state.running:
        try:
            if state.current_price>0 and len(state.m15_candles)>=20:
                path = generate_chart(state.m15_candles,"M15",chart_type="live")
                if path:
                    be = "📈" if state.trend_bias=="BULLISH" else "📉" if state.trend_bias=="BEARISH" else "➡️"
                    pe = "🟢" if state.premium_discount=="DISCOUNT" else "🔴" if state.premium_discount=="PREMIUM" else "⚪"
                    await tg_async(
                        f"{be} *{state.pair_display}  M15 Live*\n"
                        f"Price:`{state.current_price:.5f}`  Bias:`{state.trend_bias}`\n"
                        f"Zone:{pe}`{state.premium_discount}`  OB:`{state.ob_score}/100`\n"
                        f"IDM:{'✅' if state.active_idm and state.active_idm.get('swept') else '⏳'}  "
                        f"Trap:{'✅' if state.active_trap and state.active_trap.get('swept') else '⏳'}  "
                        f"Open:`{len(state.open_contracts)}`\n"
                        f"Bars H1:`{len(state.h1_candles)}` "
                        f"M15:`{len(state.m15_candles)}` "
                        f"M5:`{len(state.m5_candles)}`")
        except Exception as e:
            log.error(f"Chart broadcast: {e}")
        await asyncio.sleep(CHART_INTERVAL)

# ══════════════════════════════════════════════════════
# TRADING LOOP
# ══════════════════════════════════════════════════════
async def trading_loop():
    await asyncio.sleep(30)
    last_sig = 0
    COOLDOWN = 300

    while state.running:
        try:
            if state.paused or state.current_price==0:
                await asyncio.sleep(30); continue
            check_trade_mgmt()
            if time.time()-last_sig < COOLDOWN:
                await asyncio.sleep(30); continue

            sig = compute_signal("M15")
            if sig:
                sig5 = compute_signal("M5")
                if sig5 and sig5["direction"]==sig["direction"]:
                    log.info(f"🎯 {sig['direction']} Entry:{sig['entry']} Score:{sig['ob_score']}")
                    chart = generate_chart(
                        state.m15_candles,"M15",
                        entry_price=sig["entry"],
                        direction=sig["direction"],chart_type="entry")
                    msg = (
                        f"🎯 *SIGNAL — {state.pair_display}*\n\n"
                        f"Dir:  `{sig['direction']}`\n"
                        f"Entry:`{sig['entry']}`  SL:`{sig['sl']}`\n"
                        f"TP1({state.tp1_r}R):`{sig['tp1']}`\n"
                        f"TP2({state.tp2_r}R):`{sig['tp2']}`\n"
                        f"TP3({state.tp3_r}R):`{sig['tp3']}`\n\n"
                        f"*Logic:* `{sig['logic']}`\n"
                        f"Stake:`{sig['stake']:.2f} {state.account_currency}`"
                        f"{'  💎' if state.small_acc_mode else ''}"
                    )
                    await tg_async(msg, photo_path=chart)
                    if state.account_balance > 0:
                        cid = await open_contract(sig["direction"], sig["stake"])
                        if cid:
                            last_sig = time.time()
        except Exception as e:
            log.error(f"Trading loop: {e}\n{traceback.format_exc()}")
        await asyncio.sleep(30)

# ══════════════════════════════════════════════════════
# TELEGRAM POLL
# ══════════════════════════════════════════════════════
async def telegram_poll_loop():
    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN not set.")
        return
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, lambda: requests.post(
            f"{base}/deleteWebhook",json={"drop_pending_updates":True},timeout=10))
        log.info("Telegram webhook cleared.")
    except Exception as e:
        log.warning(f"Webhook clear: {e}")
    await asyncio.sleep(2)
    offset    = 0
    err_count = 0
    while state.running:
        try:
            r = await loop.run_in_executor(None, lambda: requests.get(
                f"{base}/getUpdates",
                params={"offset":offset,"timeout":20,
                        "allowed_updates":["message","callback_query"]},
                timeout=25))
            data = r.json()
            if not data.get("ok"):
                desc = data.get("description","")
                log.error(f"TG: {desc}")
                if "Conflict" in desc:
                    await asyncio.sleep(30)
                    await loop.run_in_executor(None, lambda: requests.post(
                        f"{base}/deleteWebhook",
                        json={"drop_pending_updates":True},timeout=10))
                else:
                    await asyncio.sleep(10)
                err_count += 1
                continue
            err_count = 0
            for upd in data.get("result",[]):
                offset = upd["update_id"]+1
                await _handle_update(upd)
        except Exception as e:
            err_count += 1
            log.error(f"TG poll: {type(e).__name__}: {e}")
            await asyncio.sleep(min(5*err_count, 60))
        await asyncio.sleep(0.5)

async def _handle_update(upd: dict):
    if "message" in upd:
        text    = upd["message"].get("text","").strip()
        chat_id = str(upd["message"]["chat"]["id"])
        if TELEGRAM_CHAT_ID and chat_id!=TELEGRAM_CHAT_ID:
            return
        await _cmd(text)
    elif "callback_query" in upd:
        cq      = upd["callback_query"]
        data    = cq.get("data","")
        cqid    = cq["id"]
        chat_id = str(cq["message"]["chat"]["id"])
        if TELEGRAM_CHAT_ID and chat_id!=TELEGRAM_CHAT_ID:
            tg_answer_callback(cqid,"Unauthorized"); return
        tg_answer_callback(cqid)
        await _cmd(data)

async def _cmd(cmd: str):
    cmd = cmd.lower().strip()

    if cmd in ("/start","/help","cmd_back"):
        await tg_async(
            f"🤖 *SMC ELITE EA v3*\n\n"
            f"Pair:`{state.pair_display}`  sym:`{state.active_symbol}`\n"
            f"Risk:`{state.risk_pct*100:.0f}%`  "
            f"Mode:`{'💎SMALL' if state.small_acc_mode else 'STD'}`\n"
            f"Bal:`{state.account_balance:.2f} {state.account_currency}`\n"
            f"Bars H1:`{len(state.h1_candles)}` M15:`{len(state.m15_candles)}` M5:`{len(state.m5_candles)}`",
            reply_markup=kb_main())

    elif cmd in ("/balance","cmd_balance"):
        if state.ws:
            try: await get_balance()
            except Exception: pass
        wr = f"{state.wins/(state.wins+state.losses)*100:.1f}%" if (state.wins+state.losses)>0 else "N/A"
        await tg_async(
            f"💰 *Balance*\n`{state.account_balance:.2f} {state.account_currency}`\n\n"
            f"Trades:`{state.trade_count}`  W/L:`{state.wins}/{state.losses}`  WR:`{wr}`\n"
            f"Total P&L:`{state.total_pnl:+.2f}`",
            reply_markup=kb_main())

    elif cmd in ("/chart","cmd_chart"):
        m15 = len(state.m15_candles)
        if m15 >= 20:
            path = generate_chart(state.m15_candles,"M15",chart_type="live")
            if path:
                pe = "🟢" if state.premium_discount=="DISCOUNT" else "🔴" if state.premium_discount=="PREMIUM" else "⚪"
                await tg_async(
                    f"📊 *{state.pair_display}  M15*\n"
                    f"Price:`{state.current_price:.5f}`  Bias:`{state.trend_bias}`\n"
                    f"Zone:{pe}`{state.premium_discount}`  OB:`{state.ob_score}/100`\n"
                    f"H1:`{len(state.h1_candles)}`  M15:`{m15}`  M5:`{len(state.m5_candles)}`\n"
                    f"IDM:{'✅' if state.active_idm and state.active_idm.get('swept') else '⏳'}  "
                    f"Trap:{'✅' if state.active_trap and state.active_trap.get('swept') else '⏳'}",
                    photo_path=path, reply_markup=kb_main())
                return
        # Diagnostic when no chart yet
        gran_used = state.gran_actual.get(900, 900)
        await tg_async(
            f"⚠️ *Chart not ready*\n\n"
            f"Bars loaded:\n"
            f"  H1 : `{len(state.h1_candles)}`\n"
            f"  M15: `{m15}` ← needs 20+\n"
            f"  M5 : `{len(state.m5_candles)}`\n\n"
            f"Symbol: `{state.active_symbol or 'not set'}`\n"
            f"M15 gran in use: `{gran_used}s`\n"
            f"WS: `{'connected' if state.ws else 'disconnected'}`\n"
            f"Price: `{state.current_price}`\n\n"
            f"_The bot is retrying data fetch automatically._",
            reply_markup=kb_main())

    elif cmd in ("/history","cmd_history"):
        if not state.trade_history:
            await tg_async("📋 No trade history yet.", reply_markup=kb_main()); return
        path  = generate_history_chart()
        lines = [f"📋 *History — last 10*\n"]
        for t in state.trade_history[-10:]:
            s = "+" if t["pnl"]>0 else ""
            lines.append(
                f"{'✅' if t['win'] else '❌'} `#{t['num']}` {t['direction']} "
                f"`{t['entry']:.5f}`→`{t['exit']:.5f}` `{s}{t['pnl']:.2f}` _{t['ts']}_")
        wr = f"{state.wins/(state.wins+state.losses)*100:.1f}%" if (state.wins+state.losses)>0 else "N/A"
        lines.append(f"\nP&L:`{state.total_pnl:+.2f}`  WR:`{wr}`")
        await tg_async("\n".join(lines), photo_path=path, reply_markup=kb_main())

    elif cmd in ("/status","cmd_status"):
        mode = "🛑 PAUSED" if state.paused else "🟢 SCANNING"
        sig_info = ""
        if state.last_signal:
            s = state.last_signal
            sig_info = (f"\n\n*Last Signal*\n`{s['direction']}` @ `{s['entry']}`\n"
                        f"SL:`{s['sl']}` TP1:`{s['tp1']}`\n`{s['logic'][:70]}`")
        await tg_async(
            f"⚡ *Status*\n"
            f"Mode:{mode}  Pair:`{state.pair_display}`\n"
            f"Price:`{state.current_price:.5f}`\n"
            f"Bias:`{state.trend_bias}`  Zone:`{state.premium_discount}`\n"
            f"H1:`{len(state.h1_candles)}` M15:`{len(state.m15_candles)}` M5:`{len(state.m5_candles)}`\n"
            f"Open:`{len(state.open_contracts)}`  Bal:`{state.account_balance:.2f}`\n"
            f"Risk:`{state.risk_pct*100:.0f}%`  Mode:`{'💎SMALL' if state.small_acc_mode else 'STD'}`"
            + sig_info, reply_markup=kb_main())

    elif cmd in ("/settings","cmd_settings"):
        await tg_async(
            f"⚙️ *Settings*\n"
            f"Pair:`{state.pair_display}`  sym:`{state.active_symbol}`\n"
            f"Risk:`{state.risk_pct*100:.0f}%`  "
            f"TPs:`{state.tp1_r}R/{state.tp2_r}R/{state.tp3_r}R`\n"
            f"Small Acc:`{'ON 💎' if state.small_acc_mode else 'OFF'}`",
            reply_markup=kb_settings())

    elif cmd == "cmd_pair_menu":
        await tg_async("💱 *Select Pair:*", reply_markup=kb_pair_menu())

    elif cmd.startswith("cmd_pair_"):
        key = cmd.replace("cmd_pair_","").upper()
        if key in PAIR_REGISTRY:
            old_key = state.pair_key
            state.pair_key     = key
            state.small_acc_mode = False
            if state.ws:
                try:
                    await tg_async(f"⏳ Switching to `{PAIR_REGISTRY[key][4]}`...", reply_markup=kb_main())
                    h1c, m15c, m5c = await subscribe_pair(key)
                    await tg_async(
                        f"💱 Switched to `{state.pair_display}`\n"
                        f"sym:`{state.active_symbol}`\n"
                        f"H1:`{h1c}` M15:`{m15c}` M5:`{m5c}` bars ✅",
                        reply_markup=kb_main())
                except Exception as e:
                    state.pair_key = old_key
                    await tg_async(f"❌ Switch failed: {e}", reply_markup=kb_main())
            else:
                await tg_async(f"💱 Pair set → `{state.pair_display}` (next connect)", reply_markup=kb_main())

    elif cmd == "cmd_small_acc":
        if state.small_acc_mode:
            state.small_acc_mode = False
            state.risk_pct = 0.01
            state.tp1_r, state.tp2_r, state.tp3_r = 2.0, 4.0, 6.0
            await tg_async("💎 Small Acc *OFF* — reset 1% risk", reply_markup=kb_settings())
        else:
            state.small_acc_mode = True
            if state.pair_key == "XAUUSD":
                state.risk_pct = 0.02
                state.tp1_r, state.tp2_r, state.tp3_r = 1.5, 3.0, 5.0
                note = "XAU/USD 2% risk (tight TPs)"
            else:
                state.pair_key = "GBPUSD"
                state.risk_pct = 0.05
                state.tp1_r, state.tp2_r, state.tp3_r = 1.5, 3.0, 4.5
                note = "GBP/USD 5% risk"
            if state.ws:
                try: await subscribe_pair(state.pair_key)
                except Exception: pass
            await tg_async(f"💎 *Small Acc ON*\n{note}", reply_markup=kb_settings())

    elif cmd == "cmd_risk_1":
        state.risk_pct=0.01; state.small_acc_mode=False
        await tg_async("✅ Risk → *1%*", reply_markup=kb_settings())
    elif cmd == "cmd_risk_3":
        state.risk_pct=0.03; state.small_acc_mode=False
        await tg_async("✅ Risk → *3%*", reply_markup=kb_settings())
    elif cmd == "cmd_risk_5":
        state.risk_pct=0.05; state.small_acc_mode=False
        await tg_async("✅ Risk → *5%*", reply_markup=kb_settings())

    elif cmd in ("/close_all","cmd_stop"):
        state.paused = True
        n = await close_all()
        await tg_async(f"🛑 *Emergency Stop*\nClosed `{n}` contracts. Bot *PAUSED*.", reply_markup=kb_main())

    elif cmd in ("/resume","cmd_resume"):
        state.paused = False
        await tg_async("▶️ Bot *RESUMED* — scanning.", reply_markup=kb_main())

# ══════════════════════════════════════════════════════
# WS ENGINE
# ══════════════════════════════════════════════════════
async def ws_reader(ws):
    async for raw in ws:
        try:
            await handle_message(json.loads(raw))
        except Exception as e:
            log.error(f"Handler: {type(e).__name__}: {e}")

async def ws_setup_and_run(ws):
    state.ws = ws
    async def setup():
        await asyncio.sleep(0.1)
        log.info("Authorizing...")
        await authorize()
        await get_balance()
        log.info(f"Balance: {state.account_balance} {state.account_currency}")
        h1c, m15c, m5c = await subscribe_pair(state.pair_key)
        await tg_async(
            f"🤖 *SMC ELITE EA Online*\n"
            f"Pair:`{state.pair_display}`  sym:`{state.active_symbol}`\n"
            f"Bal:`{state.account_balance:.2f} {state.account_currency}`\n"
            f"Risk:`{state.risk_pct*100:.0f}%`  Mode:`{'💎SMALL' if state.small_acc_mode else 'STD'}`\n"
            f"Bars: H1:`{h1c}` M15:`{m15c}` M5:`{m5c}` ✅",
            reply_markup=kb_main())
    task = asyncio.ensure_future(setup())
    try:
        await ws_reader(ws)
    finally:
        task.cancel()
        try: await task
        except (asyncio.CancelledError, Exception): pass

async def ws_connect_loop():
    delay = 5
    while state.running:
        try:
            log.info(f"Connecting: {DERIV_WS_BASE}")
            async with websockets.connect(
                DERIV_WS_BASE,
                ping_interval=25, ping_timeout=10,
                close_timeout=10, open_timeout=15,
            ) as ws:
                delay = 5
                await ws_setup_and_run(ws)
        except websockets.ConnectionClosed as e:
            log.warning(f"WS closed: {e}. Retry {delay}s")
        except asyncio.TimeoutError as e:
            log.error(f"WS timeout: {e}")
        except Exception as e:
            log.error(f"WS error: {type(e).__name__}: {e}")
            log.error(traceback.format_exc())
        finally:
            state.ws = None
            for fut in state.pending_requests.values():
                if not fut.done(): fut.cancel()
            state.pending_requests.clear()
        await asyncio.sleep(delay)
        delay = min(delay*2, 60)

# ══════════════════════════════════════════════════════
# HEALTH SERVER
# ══════════════════════════════════════════════════════
async def health_handler(req):
    wr = f"{state.wins/(state.wins+state.losses)*100:.1f}" if (state.wins+state.losses)>0 else "0"
    return web.json_response({
        "status": "running" if state.running else "stopped",
        "paused": state.paused,
        "pair":   state.pair_key,
        "symbol": state.active_symbol,
        "price":  state.current_price,
        "trend":  state.trend_bias,
        "zone":   state.premium_discount,
        "ob_score": state.ob_score,
        "balance":  state.account_balance,
        "risk_pct": state.risk_pct,
        "small_acc": state.small_acc_mode,
        "trades":   state.trade_count,
        "wins":     state.wins,
        "losses":   state.losses,
        "winrate":  wr,
        "total_pnl": state.total_pnl,
        "open":   len(state.open_contracts),
        "h1_bars":  len(state.h1_candles),
        "m15_bars": len(state.m15_candles),
        "m5_bars":  len(state.m5_candles),
        "gran_actual": state.gran_actual,
    })

async def start_health_server():
    app = web.Application()
    app.router.add_get("/",       health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"Health :{PORT}")

# ══════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════
async def main():
    log.info("╔══════════════════════════════════════╗")
    log.info("║  SMC ELITE EA v3 · Multi-Pair Forex  ║")
    log.info("╚══════════════════════════════════════╝")
    if not DERIV_API_TOKEN:
        log.error("DERIV_API_TOKEN not set. Exiting.")
        return
    await asyncio.gather(
        start_health_server(),
        ws_connect_loop(),
        trading_loop(),
        telegram_poll_loop(),
        chart_broadcast_loop(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down…")
        state.running = False
