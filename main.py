"""
╔══════════════════════════════════════════════════════════════════╗
║   SMC ELITE EA  —  Multi-Pair Forex Bot                         ║
║   Senior Quant SMC Logic | Deriv WebSocket | Railway-Ready      ║
║   Features: IDM, OB Scoring, Premium/Discount, Trap Detection   ║
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
# PAIR REGISTRY
# Symbol → (Deriv symbol, pip value, min_stake, display)
# ══════════════════════════════════════════════════════
PAIR_REGISTRY = {
    "XAUUSD":  ("frxXAUUSD",  0.01,  1.0,  "XAU/USD 🥇"),
    "EURUSD":  ("frxEURUSD",  0.0001, 1.0,  "EUR/USD 🇪🇺"),
    "GBPUSD":  ("frxGBPUSD",  0.0001, 1.0,  "GBP/USD 🇬🇧"),
    "US100":   ("OTC_NDX",    0.1,   1.0,  "NASDAQ 💻"),
}

# ══════════════════════════════════════════════════════
# CONFIG  (env vars with sensible defaults)
# ══════════════════════════════════════════════════════
DERIV_APP_ID     = os.getenv("DERIV_APP_ID", "1089")
DERIV_API_TOKEN  = os.getenv("DERIV_API_TOKEN", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PORT             = int(os.getenv("PORT", "8080"))
CHART_INTERVAL   = int(os.getenv("CHART_INTERVAL", "300"))   # auto-chart secs
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

        # ── Active pair settings ──
        self.pair_key         = "XAUUSD"           # active pair key
        self.risk_pct         = 0.01               # 1%
        self.tp1_r            = 2.0
        self.tp2_r            = 4.0
        self.tp3_r            = 6.0
        self.small_acc_mode   = False

        # ── Candle buffers (keyed by granularity) ──
        self.h1_candles       = deque(maxlen=300)
        self.m15_candles      = deque(maxlen=300)
        self.m5_candles       = deque(maxlen=300)

        # ── Price & analysis ──
        self.current_price    = 0.0
        self.trend_bias       = "NEUTRAL"
        self.last_signal      = None
        self.active_ob        = None
        self.active_fvg       = None
        self.active_trap      = None
        self.active_idm       = None       # Inducement level
        self.premium_discount = "NEUTRAL"  # PREMIUM | DISCOUNT | NEUTRAL
        self.ob_score         = 0          # 0-100

        # ── WebSocket ──
        self.ws               = None
        self.req_id           = 1
        self.pending_requests = {}
        self.subscribed_pair  = None       # track what we're subscribed to

        # ── Trade tracking ──
        self.open_contracts   = {}
        self.trade_count      = 0
        self.wins             = 0
        self.losses           = 0
        self.total_pnl        = 0.0
        self.trade_history    = []         # max 50

    @property
    def pair_symbol(self):
        return PAIR_REGISTRY[self.pair_key][0]

    @property
    def pip_value(self):
        return PAIR_REGISTRY[self.pair_key][1]

    @property
    def pair_display(self):
        return PAIR_REGISTRY[self.pair_key][3]

    @property
    def min_stake(self):
        return PAIR_REGISTRY[self.pair_key][2]

state = BotState()

# ══════════════════════════════════════════════════════
# TELEGRAM KEYBOARD FACTORY
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
    r = state.risk_pct * 100
    sm = "✅ ON" if state.small_acc_mode else "OFF"
    return {"inline_keyboard": [
        [{"text":"💱 Select Pair",       "callback_data":"cmd_pair_menu"}],
        [{"text":f"💎 Small Acc ($10) [{sm}]", "callback_data":"cmd_small_acc"}],
        [{"text":f"{'✅' if r==1 else ''}1% Risk",  "callback_data":"cmd_risk_1"},
         {"text":f"{'✅' if r==3 else ''}3% Risk",  "callback_data":"cmd_risk_3"},
         {"text":f"{'✅' if r==5 else ''}5% Risk",  "callback_data":"cmd_risk_5"}],
        [{"text":"⬅️ Back",              "callback_data":"cmd_back"}],
    ]}

def kb_pair_menu():
    active = state.pair_key
    rows = []
    pairs = [("XAUUSD","XAU/USD 🥇"),("EURUSD","EUR/USD 🇪🇺"),
             ("GBPUSD","GBP/USD 🇬🇧"),("US100","NASDAQ 💻")]
    row = []
    for key, label in pairs:
        tick = "✅ " if key == active else ""
        row.append({"text": tick+label, "callback_data": f"cmd_pair_{key}"})
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([{"text":"⬅️ Back", "callback_data":"cmd_settings"}])
    return {"inline_keyboard": rows}

# ══════════════════════════════════════════════════════
# TELEGRAM HELPERS
# ══════════════════════════════════════════════════════
def tg_send(text: str, photo_path: str = None, reply_markup=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
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
            log.warning(f"TG HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"tg_send: {type(e).__name__}: {e}")

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
# ─────────────────────────────────────────────────────
#   CHART ENGINE
# ─────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════
CHART_BG    = "#0d1117"
CHART_PANEL = "#161b22"
BULL_CLR    = "#00e676"
BEAR_CLR    = "#ff1744"
OB_BULL_CLR = "#00bcd4"
OB_BEAR_CLR = "#ff9800"
FVG_CLR     = "#ce93d8"
TRAP_CLR    = "#ffeb3b"
IDM_CLR     = "#80cbc4"
ENTRY_CLR   = "#2979ff"
SL_CLR      = "#f44336"
TP_CLRS     = ["#69f0ae", "#40c4ff", "#b388ff"]
TEXT_CLR    = "#cdd9e5"
GRID_CLR    = "#1e2a38"


def _apply_ax_style(ax):
    ax.set_facecolor(CHART_PANEL)
    ax.tick_params(colors="#90a4ae", labelsize=7)
    for spine in ax.spines.values():
        spine.set_edgecolor(GRID_CLR)
    ax.grid(axis="y", color=GRID_CLR, linewidth=0.4, alpha=0.6)


def generate_chart(
    candles: deque,
    timeframe: str = "M15",
    entry_price: float = None,
    exit_price: float = None,
    direction: str = None,
    pnl: float = None,
    chart_type: str = "live",   # live | entry | exit
) -> Optional[str]:

    if len(candles) < 20:
        return None

    df = pd.DataFrame(list(candles)[-80:])
    df.columns = ["time", "open", "high", "low", "close"]
    df.reset_index(drop=True, inplace=True)

    fig = plt.figure(figsize=(16, 9), facecolor=CHART_BG)
    gs  = gridspec.GridSpec(4, 1, figure=fig,
                            hspace=0.05,
                            height_ratios=[4, 0.6, 0.6, 0.6])
    ax_main  = fig.add_subplot(gs[0])
    ax_vol   = fig.add_subplot(gs[1], sharex=ax_main)
    ax_rsi   = fig.add_subplot(gs[2], sharex=ax_main)
    ax_label = fig.add_subplot(gs[3])

    for ax in (ax_main, ax_vol, ax_rsi, ax_label):
        _apply_ax_style(ax)

    # ── Candles ──
    for i, row in df.iterrows():
        c = BULL_CLR if row["close"] >= row["open"] else BEAR_CLR
        bl = min(row["open"], row["close"])
        bh = max(row["open"], row["close"])
        ax_main.plot([i, i], [row["low"], row["high"]],
                     color=c, linewidth=0.8, alpha=0.9)
        ax_main.add_patch(mpatches.FancyBboxPatch(
            (i-0.35, bl), 0.7, max(bh-bl, df["close"].mean()*0.00005),
            boxstyle="square,pad=0", fc=c, ec=c, alpha=0.85))

    # ── Volume bars ──
    if "volume" not in df.columns:
        df["volume"] = 1   # placeholder
    for i, row in df.iterrows():
        c = BULL_CLR if row["close"] >= row["open"] else BEAR_CLR
        ax_vol.bar(i, row["volume"], color=c, alpha=0.5, width=0.7)
    ax_vol.set_ylabel("Vol", color="#555d68", fontsize=6)

    # ── RSI ──
    closes = df["close"].values
    rsi_vals = _calc_rsi(closes, 14)
    ax_rsi.plot(range(len(rsi_vals)), rsi_vals, color="#90a4ae",
                linewidth=0.9, alpha=0.9)
    ax_rsi.axhline(70, color=BEAR_CLR, linewidth=0.5, linestyle="--", alpha=0.5)
    ax_rsi.axhline(30, color=BULL_CLR, linewidth=0.5, linestyle="--", alpha=0.5)
    ax_rsi.axhline(50, color=GRID_CLR, linewidth=0.4, alpha=0.5)
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_ylabel("RSI", color="#555d68", fontsize=6)

    # ── Order Block ──
    if state.active_ob:
        ob = state.active_ob
        oc = OB_BULL_CLR if ob["type"] == "BULL" else OB_BEAR_CLR
        xs = max(0, len(df)-35) / len(df)
        ax_main.axhspan(ob["low"], ob["high"], xmin=xs, alpha=0.15,
                        color=oc, zorder=2)
        ax_main.axhline(ob["high"], color=oc, ls="--", lw=0.8, alpha=0.7)
        ax_main.axhline(ob["low"],  color=oc, ls="--", lw=0.8, alpha=0.7)
        score_txt = f" {ob['type']} OB  score:{state.ob_score}/100"
        ax_main.text(2, ob["high"], score_txt, color=oc,
                     fontsize=7, va="bottom", fontfamily="monospace", zorder=5)

    # ── FVG / Imbalance ──
    if state.active_fvg:
        fvg = state.active_fvg
        ax_main.axhspan(fvg["low"], fvg["high"], alpha=0.13,
                        color=FVG_CLR, zorder=2, label="FVG")
        ax_main.text(2, fvg["high"], "  FVG", color=FVG_CLR,
                     fontsize=7, va="bottom", fontfamily="monospace")

    # ── Inducement Level ──
    if state.active_idm:
        idm = state.active_idm
        ax_main.axhline(idm["level"], color=IDM_CLR, ls=":",
                        lw=1.2, alpha=0.9, zorder=3)
        side_txt = "IDM BUY" if idm["side"] == "BUY" else "IDM SELL"
        swept_txt = " ✅ SWEPT" if idm.get("swept") else " ⏳ waiting"
        ax_main.text(2, idm["level"], f"  {side_txt}{swept_txt}",
                     color=IDM_CLR, fontsize=7, va="bottom", fontfamily="monospace")

    # ── Liquidity Trap ──
    if state.active_trap:
        trap = state.active_trap
        ax_main.axhline(trap["level"], color=TRAP_CLR, ls=":",
                        lw=1.4, alpha=0.9, zorder=3)
        ax_main.text(2, trap["level"], f"  TRAP {trap['side']}",
                     color=TRAP_CLR, fontsize=7, va="bottom", fontfamily="monospace")

    # ── Premium / Discount Fib ──
    if state.last_signal:
        sig = state.last_signal
        if "fib_hi" in sig and "fib_lo" in sig:
            fib_mid = (sig["fib_hi"] + sig["fib_lo"]) / 2
            ax_main.axhline(fib_mid, color="#78909c", ls="-.", lw=0.7, alpha=0.5)
            ax_main.text(len(df)-2, fib_mid, " 0.5 Fib",
                         color="#78909c", fontsize=6, va="bottom",
                         ha="right", fontfamily="monospace")
            # Shade discount/premium
            ax_main.axhspan(sig["fib_lo"], fib_mid, alpha=0.04,
                            color=BULL_CLR)   # discount
            ax_main.axhspan(fib_mid, sig["fib_hi"], alpha=0.04,
                            color=BEAR_CLR)   # premium

    # ── Signal lines ──
    if state.last_signal:
        sig = state.last_signal
        ax_main.axhline(sig["entry"], color=ENTRY_CLR, lw=1.6,
                        ls="-", zorder=4, label=f"Entry {sig['direction']}")
        ax_main.axhline(sig["sl"], color=SL_CLR, lw=1.0,
                        ls="--", zorder=4, label="SL")
        for idx, (tp_k, tp_c) in enumerate(
                zip(["tp1","tp2","tp3"], TP_CLRS)):
            if tp_k in sig:
                ax_main.axhline(sig[tp_k], color=tp_c, lw=0.8,
                                ls="-.", zorder=4, label=tp_k.upper())

    # ── Entry/Exit overlays (for trade screenshots) ──
    if entry_price:
        e_col = BULL_CLR if direction == "BUY" else BEAR_CLR
        ax_main.axhline(entry_price, color=ENTRY_CLR, lw=2.2,
                        ls="-", alpha=0.9, zorder=5)
        ax_main.annotate(f"▶ ENTRY {entry_price:.5f}",
                         xy=(len(df)-1, entry_price), color=ENTRY_CLR,
                         fontsize=8, fontfamily="monospace",
                         ha="right", va="bottom")

    if exit_price:
        ex_col = BULL_CLR if (pnl and pnl > 0) else BEAR_CLR
        ax_main.axhline(exit_price, color=ex_col, lw=2.0,
                        ls="--", alpha=0.9, zorder=5)
        pnl_s = f"+{pnl:.2f}" if pnl and pnl > 0 else f"{pnl:.2f}"
        ax_main.annotate(f"◀ EXIT {exit_price:.5f}  P&L:{pnl_s}",
                         xy=(len(df)-1, exit_price), color=ex_col,
                         fontsize=8, fontfamily="monospace",
                         ha="right", va="top")

    # ── Swing H/L markers ──
    swh, swl = detect_swing_points(df)
    for i in swh:
        ax_main.plot(i, df.iloc[i]["high"]*1.00015, "^",
                     color=BULL_CLR, ms=4, alpha=0.55)
    for i in swl:
        ax_main.plot(i, df.iloc[i]["low"]*0.99985, "v",
                     color=BEAR_CLR, ms=4, alpha=0.55)

    # ── Premium/Discount label ──
    pd_color = BULL_CLR if state.premium_discount=="DISCOUNT" \
        else (BEAR_CLR if state.premium_discount=="PREMIUM" else "#90a4ae")
    trend_c = BULL_CLR if state.trend_bias=="BULLISH" \
        else (BEAR_CLR if state.trend_bias=="BEARISH" else "#90a4ae")
    type_lbl = {"live":"📡 LIVE","entry":"🎯 ENTRY","exit":"🏁 CLOSED"}.get(chart_type,"")

    ax_main.set_title(
        f"{type_lbl}  {state.pair_display}  ·  {timeframe}  "
        f"·  Bias: {state.trend_bias}  "
        f"·  Zone: {state.premium_discount}  "
        f"·  {state.current_price:.5f}",
        color=trend_c, fontsize=10, fontfamily="monospace", pad=8)
    ax_main.set_ylabel("Price", color="#90a4ae", fontsize=8)

    # ── Legend ──
    legend_elements = []
    if state.active_ob:
        oc = OB_BULL_CLR if state.active_ob["type"]=="BULL" else OB_BEAR_CLR
        legend_elements.append(mpatches.Patch(fc=oc, alpha=0.4, label=f"OB ({state.active_ob['type']})"))
    if state.active_fvg:
        legend_elements.append(mpatches.Patch(fc=FVG_CLR, alpha=0.4, label="FVG"))
    if state.active_idm:
        legend_elements.append(Line2D([0],[0], color=IDM_CLR, ls=":", lw=1.5, label="IDM"))
    if state.active_trap:
        legend_elements.append(Line2D([0],[0], color=TRAP_CLR, ls=":", lw=1.5, label="TRAP"))
    if legend_elements:
        ax_main.legend(handles=legend_elements, loc="upper left",
                       fontsize=7, facecolor=CHART_BG,
                       edgecolor=GRID_CLR, labelcolor=TEXT_CLR)

    # ── Bottom label bar ──
    ax_label.set_xlim(0, 1); ax_label.set_ylim(0, 1)
    ax_label.axis("off")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    risk_txt = f"Risk:{state.risk_pct*100:.0f}%  Mode:{'💎SMALL' if state.small_acc_mode else 'STD'}"
    ax_label.text(0.01, 0.5,
        f"SMC ELITE EA  ·  {state.pair_display}  ·  Bal:{state.account_balance:.2f} {state.account_currency}"
        f"  ·  {risk_txt}  ·  OB Score:{state.ob_score}/100  ·  {ts}",
        color="#555d68", fontsize=7, va="center", fontfamily="monospace")

    plt.setp(ax_main.get_xticklabels(),  visible=False)
    plt.setp(ax_vol.get_xticklabels(),   visible=False)
    plt.setp(ax_rsi.get_xticklabels(),   visible=False)

    path = f"/tmp/smc_chart_{chart_type}.png"
    plt.tight_layout()
    plt.savefig(path, dpi=130, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def generate_history_chart() -> Optional[str]:
    history = state.trade_history[-20:]
    if not history:
        return None

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9),
                                    facecolor=CHART_BG,
                                    gridspec_kw={"height_ratios":[2,1]})
    for ax in (ax1, ax2):
        _apply_ax_style(ax)

    labels = [f"#{t['num']}" for t in history]
    pnls   = [t["pnl"] for t in history]
    colors = [BULL_CLR if p > 0 else BEAR_CLR for p in pnls]
    bars   = ax1.bar(labels, pnls, color=colors, alpha=0.85, ec=GRID_CLR)
    ax1.axhline(0, color=GRID_CLR, lw=0.8)
    for bar, val in zip(bars, pnls):
        sign = "+" if val >= 0 else ""
        ax1.text(bar.get_x()+bar.get_width()/2,
                 bar.get_height()+(0.05 if val>=0 else -0.15),
                 f"{sign}{val:.2f}", ha="center", va="bottom",
                 color=TEXT_CLR, fontsize=7, fontfamily="monospace")
    wr = f"{state.wins/(state.wins+state.losses)*100:.1f}%" if (state.wins+state.losses)>0 else "N/A"
    ax1.set_title(
        f"📋 Trade History  ·  {state.wins}W/{state.losses}L  WR:{wr}  "
        f"Total P&L:{state.total_pnl:+.2f} {state.account_currency}  "
        f"Bal:{state.account_balance:.2f}",
        color=TEXT_CLR, fontsize=10, fontfamily="monospace", pad=8)
    ax1.set_ylabel("P&L (USD)", color="#90a4ae", fontsize=9)

    cumulative = np.cumsum(pnls)
    cc = BULL_CLR if cumulative[-1] >= 0 else BEAR_CLR
    ax2.plot(labels, cumulative, color=cc, lw=1.8, marker="o", ms=4)
    ax2.fill_between(labels, cumulative, alpha=0.12, color=cc)
    ax2.axhline(0, color=GRID_CLR, lw=0.8)
    ax2.set_ylabel("Cum. P&L", color="#90a4ae", fontsize=8)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fig.text(0.99, 0.01, f"SMC ELITE · {ts}",
             color="#444d56", fontsize=7, ha="right", fontfamily="monospace")

    path = "/tmp/smc_history.png"
    plt.tight_layout()
    plt.savefig(path, dpi=130, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


# ══════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────
#   SMC INTELLIGENCE ENGINE
#   "Think like a bank trader, not a retail trader"
# ─────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════

def _calc_rsi(prices: np.ndarray, period: int = 14) -> np.ndarray:
    if len(prices) < period + 1:
        return np.full(len(prices), 50.0)
    deltas = np.diff(prices)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g  = np.convolve(gains,  np.ones(period)/period, mode="valid")
    avg_l  = np.convolve(losses, np.ones(period)/period, mode="valid")
    rs     = np.where(avg_l != 0, avg_g / avg_l, 100.0)
    rsi    = 100.0 - 100.0 / (1.0 + rs)
    pad    = len(prices) - len(rsi)
    return np.concatenate([np.full(pad, 50.0), rsi])


def detect_swing_points(df: pd.DataFrame, n: int = 5):
    """Swing H/L using candle bodies only for robustness."""
    highs, lows = [], []
    for i in range(n, len(df)-n):
        # Use HIGH of candle for swing high
        if df["high"].iloc[i] == df["high"].iloc[i-n:i+n+1].max():
            highs.append(i)
        if df["low"].iloc[i] == df["low"].iloc[i-n:i+n+1].min():
            lows.append(i)
    return highs, lows


def detect_bos_choch_body(df: pd.DataFrame):
    """
    BOS/CHoCH confirmed ONLY on candle BODY CLOSE (not wick).
    A true BOS needs the close to exceed the prior swing, not just the wick.
    """
    highs, lows = detect_swing_points(df)
    if len(highs) < 2 or len(lows) < 2:
        return None

    last_sh, prev_sh = highs[-1], highs[-2]
    last_sl, prev_sl = lows[-1],  lows[-2]
    last_close = df["close"].iloc[-1]   # ← body close, not wick

    sh_level = df["high"].iloc[last_sh]
    sl_level = df["low"].iloc[last_sl]

    # Bullish BOS: body CLOSES above prior swing high
    if last_close > sh_level and last_sh > prev_sh:
        return {"type":"BOS","direction":"BULLISH","level":sh_level,"idx":len(df)-1}

    # Bearish BOS: body CLOSES below prior swing low
    if last_close < sl_level and last_sl > prev_sl:
        return {"type":"BOS","direction":"BEARISH","level":sl_level,"idx":len(df)-1}

    # Bullish CHoCH: series of LH broken to upside (body close)
    if df["high"].iloc[last_sh] < df["high"].iloc[prev_sh] and last_close > sh_level:
        return {"type":"CHoCH","direction":"BULLISH","level":sh_level,"idx":len(df)-1}

    # Bearish CHoCH: series of HL broken to downside (body close)
    if df["low"].iloc[last_sl] > df["low"].iloc[prev_sl] and last_close < sl_level:
        return {"type":"CHoCH","direction":"BEARISH","level":sl_level,"idx":len(df)-1}

    return None


def detect_inducement(df: pd.DataFrame, direction: str) -> Optional[dict]:
    """
    Inducement (IDM): A minor swing point BEFORE the major swing that creates
    a liquidity pool. Smart money will sweep this minor level first before the
    real move. We wait for the sweep confirmation.

    Bullish IDM: Minor swing LOW that price will sweep below before rallying.
    Bearish IDM: Minor swing HIGH that price will sweep above before dropping.
    """
    highs, lows = detect_swing_points(df, n=3)  # smaller n = minor swings

    if direction == "BULLISH" and len(lows) >= 2:
        # The second-to-last low is the inducement (minor low before major move)
        idm_idx   = lows[-2]
        idm_level = df["low"].iloc[idm_idx]
        # Is it swept? Last candle's LOW went below IDM, but CLOSE is above it
        last = df.iloc[-1]
        swept = last["low"] < idm_level and last["close"] > idm_level
        return {"side":"BUY","level":idm_level,"idx":idm_idx,"swept":swept}

    elif direction == "BEARISH" and len(highs) >= 2:
        idm_idx   = highs[-2]
        idm_level = df["high"].iloc[idm_idx]
        last = df.iloc[-1]
        swept = last["high"] > idm_level and last["close"] < idm_level
        return {"side":"SELL","level":idm_level,"idx":idm_idx,"swept":swept}

    return None


def detect_equal_highs_lows(df: pd.DataFrame) -> Optional[dict]:
    """
    Equal Highs/Lows create retail-visible levels that smart money targets.
    We detect them and wait for the sweep + rejection.
    Tolerance: 0.03% for forex, 0.05% for gold/indices.
    """
    tol = 0.0003 if state.pair_key in ("EURUSD","GBPUSD") else 0.0005
    recent = df.iloc[-25:]
    highs  = recent["high"].values
    lows   = recent["low"].values

    # Equal Highs → expect bear trap / sell-side liquidity hunt
    for i in range(len(highs)-1, 1, -1):
        for j in range(i-1, max(i-8, 0), -1):
            if abs(highs[i]-highs[j])/highs[j] < tol:
                lvl  = (highs[i]+highs[j])/2
                last = df.iloc[-1]
                # Swept = wick above, then body closed back below
                if last["high"] > lvl and last["close"] < lvl:
                    return {"side":"SELL","level":lvl,"swept":True,"type":"EQL_HIGHS"}
                elif abs(last["high"]-lvl)/lvl < tol*2:
                    return {"side":"SELL","level":lvl,"swept":False,"type":"EQL_HIGHS"}

    # Equal Lows → expect bull trap / buy-side liquidity hunt
    for i in range(len(lows)-1, 1, -1):
        for j in range(i-1, max(i-8, 0), -1):
            if abs(lows[i]-lows[j])/lows[j] < tol:
                lvl  = (lows[i]+lows[j])/2
                last = df.iloc[-1]
                if last["low"] < lvl and last["close"] > lvl:
                    return {"side":"BUY","level":lvl,"swept":True,"type":"EQL_LOWS"}
                elif abs(last["low"]-lvl)/lvl < tol*2:
                    return {"side":"BUY","level":lvl,"swept":False,"type":"EQL_LOWS"}

    return None


def identify_order_block(df: pd.DataFrame, direction: str) -> Optional[dict]:
    """
    Quality Order Block detection:
    - Bull OB: Last BEARISH candle before a strong BULLISH impulse that breaks structure.
    - Bear OB: Last BULLISH candle before a strong BEARISH impulse that breaks structure.
    - Must have a displacement candle (body > 1.5x average body size).
    """
    lookback = min(30, len(df)-3)
    recent   = df.iloc[-lookback:].reset_index(drop=True)
    avg_body = (recent["close"] - recent["open"]).abs().mean()

    if direction == "BULLISH":
        for i in range(len(recent)-3, 1, -1):
            c    = recent.iloc[i]
            next_c = recent.iloc[i+1]
            body_next = abs(next_c["close"] - next_c["open"])
            # Bearish OB candle + displacement (strong bull follow-through)
            if (c["close"] < c["open"] and
                    next_c["close"] > c["high"] and
                    body_next > avg_body * 1.5):
                return {
                    "type":  "BULL",
                    "high":  c["high"],
                    "low":   c["low"],
                    "body_hi": max(c["open"], c["close"]),
                    "body_lo": min(c["open"], c["close"]),
                    "idx":   i,
                    "displacement": round(body_next / avg_body, 2),
                }

    elif direction == "BEARISH":
        for i in range(len(recent)-3, 1, -1):
            c    = recent.iloc[i]
            next_c = recent.iloc[i+1]
            body_next = abs(next_c["close"] - next_c["open"])
            if (c["close"] > c["open"] and
                    next_c["close"] < c["low"] and
                    body_next > avg_body * 1.5):
                return {
                    "type":  "BEAR",
                    "high":  c["high"],
                    "low":   c["low"],
                    "body_hi": max(c["open"], c["close"]),
                    "body_lo": min(c["open"], c["close"]),
                    "idx":   i,
                    "displacement": round(body_next / avg_body, 2),
                }
    return None


def score_order_block(ob: dict, fvg: Optional[dict],
                      trap: Optional[dict], idm: Optional[dict],
                      rsi_val: float) -> int:
    """
    OB Quality Score (0–100). Professional scoring:
      +25  FVG / Imbalance leads away from OB (displacement confirmed)
      +20  Liquidity sweep (trap) swept before OB entry
      +20  IDM swept (inducement confirmed)
      +15  Displacement ratio > 2.0 (strong impulse)
      +10  RSI confluence (OB in oversold/overbought zone)
      +10  OB aligns with H1 trend bias
    Minimum score to trade: 55
    """
    score = 0
    if fvg:
        score += 25
    if trap and trap.get("swept"):
        score += 20
    if idm and idm.get("swept"):
        score += 20
    if ob and ob.get("displacement", 0) >= 2.0:
        score += 15
    # RSI confluence
    if ob:
        if ob["type"] == "BULL" and rsi_val < 45:
            score += 10
        elif ob["type"] == "BEAR" and rsi_val > 55:
            score += 10
    score += 10  # base alignment bonus
    return min(score, 100)


def identify_fvg(df: pd.DataFrame, ob: dict) -> Optional[dict]:
    """
    Fair Value Gap (3-candle imbalance) near the OB.
    Bull FVG: candle[i].high < candle[i+2].low  (gap between them)
    Bear FVG: candle[i].low  > candle[i+2].high
    """
    if ob is None:
        return None
    lookback = min(25, len(df)-3)
    recent   = df.iloc[-lookback:].reset_index(drop=True)

    # Adaptive threshold per pair
    fvg_thr = 0.03 if state.pair_key in ("EURUSD","GBPUSD") else 0.05

    if ob["type"] == "BULL":
        for i in range(len(recent)-3, 0, -1):
            c1, c3  = recent.iloc[i], recent.iloc[i+2]
            gap_pct = (c3["low"]-c1["high"]) / c1["high"] * 100
            if c1["high"] < c3["low"] and gap_pct >= fvg_thr:
                return {"type":"BULL","high":c3["low"],"low":c1["high"],"gap_pct":gap_pct}
    elif ob["type"] == "BEAR":
        for i in range(len(recent)-3, 0, -1):
            c1, c3  = recent.iloc[i], recent.iloc[i+2]
            gap_pct = (c1["low"]-c3["high"]) / c1["low"] * 100
            if c1["low"] > c3["high"] and gap_pct >= fvg_thr:
                return {"type":"BEAR","high":c1["low"],"low":c3["high"],"gap_pct":gap_pct}
    return None


def calc_premium_discount(df: pd.DataFrame) -> tuple:
    """
    Premium/Discount zones based on the recent swing range (Fibonacci 0.5).
    BUY only in Discount (<50% retracement).
    SELL only in Premium (>50% retracement).
    Returns: (zone_str, fib_hi, fib_lo)
    """
    highs, lows = detect_swing_points(df, n=8)
    if not highs or not lows:
        return "NEUTRAL", 0, 0
    fib_hi = df["high"].iloc[highs[-1]]
    fib_lo = df["low"].iloc[lows[-1]]
    if fib_hi == fib_lo:
        return "NEUTRAL", fib_hi, fib_lo
    current = state.current_price
    fib_pct = (current - fib_lo) / (fib_hi - fib_lo)
    if fib_pct < 0.5:
        return "DISCOUNT", fib_hi, fib_lo
    elif fib_pct > 0.5:
        return "PREMIUM", fib_hi, fib_lo
    return "NEUTRAL", fib_hi, fib_lo


def analyze_h1_trend():
    if len(state.h1_candles) < 40:
        return "NEUTRAL"
    df = pd.DataFrame(list(state.h1_candles))
    df.columns = ["time","open","high","low","close"]
    result = detect_bos_choch_body(df)
    if result:
        state.trend_bias = result["direction"]
    else:
        c = df["close"].values[-30:]
        state.trend_bias = "BULLISH" if np.polyfit(np.arange(len(c)), c, 1)[0] > 0 \
            else "BEARISH"
    return state.trend_bias


def compute_signal(timeframe="M15") -> Optional[dict]:
    """
    Full SMC signal pipeline (bank-grade logic):
    1. H1 trend bias via BOS/CHoCH (body close)
    2. BOS/CHoCH confirmation on execution TF
    3. Premium/Discount zone filter (Fib 0.5)
    4. Inducement (IDM) sweep confirmation
    5. Equal H/L trap sweep confirmation
    6. Order Block identification + displacement check
    7. FVG leading away from OB (imbalance)
    8. OB quality score >= 55
    9. Entry, SL (OB low/high), TP1/2/3
    """
    candles = state.m15_candles if timeframe == "M15" else state.m5_candles
    if len(candles) < 40:
        return None

    df = pd.DataFrame(list(candles))
    df.columns = ["time","open","high","low","close"]

    # ── Step 1: H1 bias ──
    bias = analyze_h1_trend()
    if bias == "NEUTRAL":
        return None

    # ── Step 2: Structure confirmation on execution TF ──
    struct = detect_bos_choch_body(df)
    if struct is None or struct["direction"] != bias:
        return None

    # ── Step 3: Premium/Discount filter ──
    pd_zone, fib_hi, fib_lo = calc_premium_discount(df)
    state.premium_discount = pd_zone
    # BUY only in DISCOUNT, SELL only in PREMIUM
    if bias == "BULLISH" and pd_zone != "DISCOUNT":
        return None
    if bias == "BEARISH" and pd_zone != "PREMIUM":
        return None

    # ── Step 4: Inducement sweep ──
    idm = detect_inducement(df, bias)
    state.active_idm = idm
    # Require IDM to be swept (bank swept the retail stop)
    if idm is None or not idm.get("swept"):
        return None

    # ── Step 5: Liquidity trap (Equal H/L sweep) ──
    trap = detect_equal_highs_lows(df)
    state.active_trap = trap
    # Only enter after trap is swept
    if trap is None or not trap.get("swept"):
        return None
    if trap["side"] != ("BUY" if bias == "BULLISH" else "SELL"):
        return None

    # ── Step 6: Order Block ──
    ob = identify_order_block(df, bias)
    if ob is None:
        return None
    state.active_ob = ob

    # ── Step 7: FVG from OB ──
    fvg = identify_fvg(df, ob)
    state.active_fvg = fvg
    # FVG is strongly preferred but not mandatory if score compensates
    # (relaxed for small acc mode)

    # ── Step 8: OB Score ──
    closes   = df["close"].values
    rsi_vals = _calc_rsi(closes, 14)
    rsi_now  = float(rsi_vals[-1])
    ob_score = score_order_block(ob, fvg, trap, idm, rsi_now)
    state.ob_score = ob_score
    min_score = 45 if state.small_acc_mode else 55
    if ob_score < min_score:
        log.info(f"OB score too low: {ob_score} < {min_score}")
        return None

    # ── Step 9: Calculate Entry / SL / TPs ──
    if bias == "BULLISH":
        # Enter at OB body high (not wick), SL below OB low
        entry = ob["body_hi"]
        sl    = ob["low"] * 0.9995
    else:
        # Enter at OB body low, SL above OB high
        entry = ob["body_lo"]
        sl    = ob["high"] * 1.0005

    risk   = abs(entry - sl)
    if risk == 0:
        return None

    mult   = 1 if bias == "BULLISH" else -1
    tp1    = entry + risk * state.tp1_r * mult
    tp2    = entry + risk * state.tp2_r * mult
    tp3    = entry + risk * state.tp3_r * mult
    units  = round(state.account_balance * state.risk_pct / risk, 4) \
        if state.account_balance > 0 else 1.0

    # Stake: at least min_stake, always at least $1
    stake_usd = max(state.min_stake, state.account_balance * state.risk_pct)
    stake_usd = max(1.0, round(stake_usd, 2))

    # ── Build logic caption for Telegram ──
    logic_parts = [
        f"{'M15' if timeframe=='M15' else 'M5'} {struct['type']} {bias}",
        f"IDM sweep ✅",
        f"{'EQL_HIGHS' if trap['type']=='EQL_HIGHS' else 'EQL_LOWS'} trap swept ✅",
        f"{ob['type']} OB (score:{ob_score})",
    ]
    if fvg:
        logic_parts.append(f"FVG {fvg['gap_pct']:.3f}% imbalance ✅")
    logic_parts.append(f"Zone: {pd_zone}")
    logic_parts.append(f"RSI: {rsi_now:.1f}")
    logic_caption = " | ".join(logic_parts)

    signal = {
        "direction": "BUY" if bias=="BULLISH" else "SELL",
        "entry": round(entry, 5), "sl": round(sl, 5),
        "tp1": round(tp1, 5), "tp2": round(tp2, 5), "tp3": round(tp3, 5),
        "risk_r": round(risk, 5), "units": units, "stake": stake_usd,
        "struct": struct["type"], "ob": ob, "fvg": fvg,
        "trap": trap, "idm": idm,
        "ob_score": ob_score, "rsi": rsi_now,
        "pd_zone": pd_zone, "fib_hi": fib_hi, "fib_lo": fib_lo,
        "tf": timeframe, "bias": bias,
        "logic": logic_caption,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    state.last_signal = signal
    return signal


# ══════════════════════════════════════════════════════
# TRADE MANAGEMENT  (BE + Trailing Stop)
# ══════════════════════════════════════════════════════
def check_trade_management():
    for cid, info in list(state.open_contracts.items()):
        sig = info.get("signal")
        if sig is None:
            continue
        price     = state.current_price
        direction = info["direction"]
        entry     = sig["entry"]
        tp1       = sig["tp1"]

        # Break-Even once TP1 is touched
        if not info["be_moved"]:
            hit = (direction=="BUY" and price>=tp1) or \
                  (direction=="SELL" and price<=tp1)
            if hit:
                info["be_moved"] = True
                sig["sl"] = entry
                log.info(f"[{cid}] BE → SL = {entry}")
                asyncio.ensure_future(tg_async(
                    f"✅ *BreakEven* `{cid}`\nSL → entry `{entry}`"
                ))

        # Dynamic trailing stop
        if len(state.m15_candles) >= 10:
            df = pd.DataFrame(list(state.m15_candles)[-30:])
            df.columns = ["time","open","high","low","close"]
            swh, swl = detect_swing_points(df, n=3)
            if direction=="BUY" and swl:
                t_sl = df["low"].iloc[swl[-1]] * 0.9998
                if t_sl > sig["sl"]:
                    sig["sl"] = t_sl
            elif direction=="SELL" and swh:
                t_sl = df["high"].iloc[swh[-1]] * 1.0002
                if t_sl < sig["sl"]:
                    sig["sl"] = t_sl


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
        raise asyncio.TimeoutError(f"Timeout keys={list(payload.keys())}")


async def authorize():
    resp = await send_request({"authorize": DERIV_API_TOKEN})
    if "error" in resp:
        raise RuntimeError(f"Auth failed: {resp['error']['message']}")
    log.info(f"Authorized: {resp['authorize']['loginid']}")
    return resp["authorize"]


async def get_account_info():
    resp = await send_request({"balance": 1, "subscribe": 0})
    if "balance" in resp:
        state.account_balance  = resp["balance"]["balance"]
        state.account_currency = resp["balance"]["currency"]
    return resp


async def subscribe_pair(symbol: str):
    """Subscribe H1/M15/M5 and immediately load historical candles."""
    state.h1_candles.clear()
    state.m15_candles.clear()
    state.m5_candles.clear()
    state.last_signal   = None
    state.active_ob     = None
    state.active_fvg    = None
    state.active_trap   = None
    state.active_idm    = None
    state.current_price = 0.0
    state.trend_bias    = "NEUTRAL"
    state.ob_score      = 0

    gran_map = {3600: "H1", 900: "M15", 300: "M5"}
    for gran in (3600, 900, 300):
        try:
            resp = await send_request({
                "ticks_history": symbol, "end": "latest",
                "count": 200, "granularity": gran,
                "style": "candles", "subscribe": 1,
            })
            # The bulk candles response carries req_id so handle_message routes
            # it to the pending_requests future — we get it back here directly.
            # Unpack the candles NOW so buffers are filled immediately.
            candles_raw = resp.get("candles", [])
            if candles_raw:
                rows = [
                    (int(c["epoch"]), float(c["open"]), float(c["high"]),
                     float(c["low"]),  float(c["close"]))
                    for c in candles_raw
                ]
                _update_candles(gran, rows)
                log.info(f"Loaded {len(rows)} {gran_map[gran]} candles for {symbol}")
            else:
                log.warning(f"No candles in response gran={gran}: {list(resp.keys())}")
        except Exception as e:
            log.error(f"subscribe_pair gran={gran}: {e}")

    state.subscribed_pair = symbol
    log.info(
        f"Subscribed {symbol} | "
        f"H1:{len(state.h1_candles)} "
        f"M15:{len(state.m15_candles)} "
        f"M5:{len(state.m5_candles)}"
    )


async def open_contract(direction: str, amount: float):
    if state.paused:
        return None
    contract_type = "MULTUP" if direction=="BUY" else "MULTDOWN"
    try:
        resp = await send_request({
            "buy": 1, "price": round(amount, 2),
            "parameters": {
                "contract_type": contract_type,
                "symbol": state.pair_symbol,
                "amount": round(amount, 2),
                "currency": state.account_currency,
                "multiplier": 10, "basis": "stake", "stop_out": 1,
            },
        })
        if "error" in resp:
            log.error(f"Buy error: {resp['error']['message']}")
            return None
        cid = resp["buy"]["contract_id"]
        state.open_contracts[cid] = {
            "direction": direction,
            "entry": state.current_price,
            "amount": amount,
            "signal": state.last_signal,
            "be_moved": False,
            "opened_at": time.time(),
        }
        state.trade_count += 1
        log.info(f"✅ Opened {cid} [{direction}] ${amount:.2f}")
        # Subscribe to live contract updates
        asyncio.ensure_future(send_request({
            "proposal_open_contract": 1,
            "contract_id": cid, "subscribe": 1
        }))
        return cid
    except Exception as e:
        log.error(f"open_contract: {e}")
        return None


async def close_contract(cid: str):
    try:
        resp = await send_request({"sell": cid, "price": 0})
        if "error" in resp:
            log.error(f"Close error: {resp['error']['message']}")
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
def _update_candles(gran: int, candles):
    if gran == 3600:
        state.h1_candles.extend(candles)
    elif gran == 900:
        state.m15_candles.extend(candles)
        state.current_price = float(candles[-1][4])
    elif gran == 300:
        state.m5_candles.extend(candles)
        state.current_price = float(candles[-1][4])


async def handle_message(msg: dict):
    req_id = msg.get("req_id")
    if req_id and req_id in state.pending_requests:
        fut = state.pending_requests.pop(req_id)
        if not fut.done():
            fut.set_result(msg)
        return

    mtype = msg.get("msg_type", "")

    if mtype == "ohlc":
        # granularity may arrive as int or string — normalize to int
        gran = int(msg["ohlc"].get("granularity", 0))
        c    = msg["ohlc"]
        _update_candles(gran, [(
            int(c["epoch"]), float(c["open"]), float(c["high"]),
            float(c["low"]), float(c["close"])
        )])

    elif mtype == "candles":
        # This branch handles unsolicited candle pushes (no req_id)
        gran = int(msg.get("echo_req", {}).get("granularity", 0))
        rows = [(int(c["epoch"]), float(c["open"]), float(c["high"]),
                 float(c["low"]), float(c["close"]))
                for c in msg.get("candles", [])]
        if rows:
            _update_candles(gran, rows)
            log.debug(f"candles msg: gran={gran} rows={len(rows)}")

    elif mtype == "tick":
        state.current_price = float(msg["tick"]["quote"])

    elif mtype == "proposal_open_contract":
        poc    = msg.get("proposal_open_contract", {})
        cid    = str(poc.get("contract_id",""))
        if cid not in state.open_contracts:
            return
        profit = float(poc.get("profit", 0))
        status = poc.get("status","")
        exit_s = float(poc.get("exit_tick", state.current_price) or state.current_price)

        if status in ("sold","expired"):
            info = state.open_contracts.pop(cid, {})
            if profit > 0:
                state.wins += 1
            else:
                state.losses += 1
            state.total_pnl += profit

            tnum = state.trade_count
            state.trade_history.append({
                "num":       tnum,
                "id":        cid,
                "pair":      state.pair_display,
                "direction": info.get("direction","?"),
                "entry":     info.get("entry", 0),
                "exit":      exit_s,
                "pnl":       round(profit, 2),
                "win":       profit > 0,
                "ts":        datetime.now(timezone.utc).strftime("%m/%d %H:%M"),
            })
            if len(state.trade_history) > 50:
                state.trade_history.pop(0)

            # Exit chart
            exit_chart = generate_chart(
                state.m15_candles, "M15",
                entry_price=info.get("entry"),
                exit_price=exit_s,
                direction=info.get("direction"),
                pnl=profit, chart_type="exit"
            )
            sign    = "+" if profit > 0 else ""
            icon    = "✅ WIN" if profit > 0 else "❌ LOSS"
            msg_out = (
                f"{icon}  Trade `#{tnum}` Closed\n\n"
                f"Pair:      `{state.pair_display}`\n"
                f"Direction: `{info.get('direction','?')}`\n"
                f"Entry:     `{info.get('entry',0):.5f}`\n"
                f"Exit:      `{exit_s:.5f}`\n"
                f"P&L:       `{sign}{profit:.2f} {state.account_currency}`\n\n"
                f"W/L: `{state.wins}/{state.losses}`  "
                f"Total P&L: `{state.total_pnl:+.2f} {state.account_currency}`\n"
                f"Balance: `{state.account_balance:.2f} {state.account_currency}`"
            )
            asyncio.ensure_future(tg_async(msg_out, photo_path=exit_chart))

    elif mtype == "balance":
        state.account_balance  = msg["balance"]["balance"]
        state.account_currency = msg["balance"]["currency"]

    elif "error" in msg:
        log.warning(f"WS API error: {msg['error'].get('message','?')}")


# ══════════════════════════════════════════════════════
# AUTO CHART BROADCAST
# ══════════════════════════════════════════════════════
async def chart_broadcast_loop():
    await asyncio.sleep(40)
    while state.running:
        try:
            if state.current_price > 0 and len(state.m15_candles) >= 20:
                path = generate_chart(state.m15_candles, "M15", chart_type="live")
                if path:
                    bias_e = "📈" if state.trend_bias=="BULLISH" \
                        else "📉" if state.trend_bias=="BEARISH" else "➡️"
                    pd_e   = "🟢" if state.premium_discount=="DISCOUNT" \
                        else "🔴" if state.premium_discount=="PREMIUM" else "⚪"
                    caption = (
                        f"{bias_e} *{state.pair_display}  M15 Live*\n"
                        f"Price: `{state.current_price:.5f}`  "
                        f"Bias: `{state.trend_bias}`\n"
                        f"Zone: {pd_e}`{state.premium_discount}`  "
                        f"OB Score: `{state.ob_score}/100`\n"
                        f"IDM: {'✅' if state.active_idm and state.active_idm.get('swept') else '⏳'}  "
                        f"Trap: {'✅' if state.active_trap and state.active_trap.get('swept') else '⏳'}  "
                        f"Open: `{len(state.open_contracts)}`"
                    )
                    await tg_async(caption, photo_path=path)
        except Exception as e:
            log.error(f"Chart broadcast: {e}")
        await asyncio.sleep(CHART_INTERVAL)


# ══════════════════════════════════════════════════════
# TRADING LOOP
# ══════════════════════════════════════════════════════
async def trading_loop():
    await asyncio.sleep(25)
    last_signal_ts = 0
    COOLDOWN = 300  # 5 min cooldown between signals

    while state.running:
        try:
            if state.paused or state.current_price == 0:
                await asyncio.sleep(30)
                continue

            check_trade_management()

            if time.time() - last_signal_ts < COOLDOWN:
                await asyncio.sleep(30)
                continue

            # Try M15 first, confirm with M5
            sig = compute_signal("M15")
            if sig:
                sig5 = compute_signal("M5")
                if sig5 and sig5["direction"] == sig["direction"]:
                    log.info(
                        f"🎯 SIGNAL {sig['direction']} "
                        f"Entry:{sig['entry']} SL:{sig['sl']} "
                        f"Score:{sig['ob_score']} Zone:{sig['pd_zone']}"
                    )

                    entry_chart = generate_chart(
                        state.m15_candles, "M15",
                        entry_price=sig["entry"],
                        direction=sig["direction"],
                        chart_type="entry"
                    )

                    tg_msg = (
                        f"🎯 *NEW SIGNAL — {state.pair_display}*\n\n"
                        f"Direction: `{sig['direction']}`\n"
                        f"Entry:    `{sig['entry']}`\n"
                        f"SL:       `{sig['sl']}`\n"
                        f"TP1 ({state.tp1_r}R): `{sig['tp1']}`\n"
                        f"TP2 ({state.tp2_r}R): `{sig['tp2']}`\n"
                        f"TP3 ({state.tp3_r}R): `{sig['tp3']}`\n\n"
                        f"*SMC Logic:*\n`{sig['logic']}`\n\n"
                        f"OB Score: `{sig['ob_score']}/100`\n"
                        f"Zone: `{sig['pd_zone']}`  RSI: `{sig['rsi']:.1f}`\n"
                        f"Stake: `{sig['stake']:.2f} {state.account_currency}`\n"
                        f"{'💎 Small Acc Mode' if state.small_acc_mode else ''}"
                    )
                    await tg_async(tg_msg.strip(), photo_path=entry_chart)

                    if state.account_balance > 0:
                        cid = await open_contract(sig["direction"], sig["stake"])
                        if cid:
                            last_signal_ts = time.time()

        except Exception as e:
            log.error(f"Trading loop: {e}\n{traceback.format_exc()}")
        await asyncio.sleep(30)


# ══════════════════════════════════════════════════════
# TELEGRAM POLLING + COMMAND HANDLER
# ══════════════════════════════════════════════════════
async def telegram_poll_loop():
    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN not set.")
        return

    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    loop = asyncio.get_event_loop()

    # Clear webhook + pending updates (prevents Conflict error)
    try:
        await loop.run_in_executor(None, lambda: requests.post(
            f"{base}/deleteWebhook", json={"drop_pending_updates": True}, timeout=10))
        log.info("Telegram webhook cleared.")
    except Exception as e:
        log.warning(f"Webhook clear: {e}")

    await asyncio.sleep(2)
    offset = 0
    err_count = 0

    while state.running:
        try:
            def poll():
                return requests.get(f"{base}/getUpdates", params={
                    "offset": offset, "timeout": 20,
                    "allowed_updates": ["message","callback_query"]
                }, timeout=25)
            r    = await loop.run_in_executor(None, poll)
            data = r.json()

            if not data.get("ok"):
                desc = data.get("description","")
                log.error(f"TG poll: {desc}")
                if "Conflict" in desc:
                    await asyncio.sleep(30)
                    await loop.run_in_executor(None, lambda: requests.post(
                        f"{base}/deleteWebhook",
                        json={"drop_pending_updates": True}, timeout=10))
                else:
                    await asyncio.sleep(10)
                err_count += 1
                continue

            err_count = 0
            for upd in data.get("result",[]):
                offset = upd["update_id"] + 1
                await _handle_update(upd)

        except Exception as e:
            err_count += 1
            log.error(f"TG poll exception: {type(e).__name__}: {e}")
            await asyncio.sleep(min(5*err_count, 60))
            continue
        await asyncio.sleep(0.5)


async def _handle_update(upd: dict):
    if "message" in upd:
        text    = upd["message"].get("text","").strip()
        chat_id = str(upd["message"]["chat"]["id"])
        if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
            return
        await _cmd(text)
    elif "callback_query" in upd:
        cq      = upd["callback_query"]
        data    = cq.get("data","")
        cqid    = cq["id"]
        chat_id = str(cq["message"]["chat"]["id"])
        if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
            tg_answer_callback(cqid, "Unauthorized")
            return
        tg_answer_callback(cqid)
        await _cmd(data)


async def _cmd(cmd: str):
    cmd = cmd.lower().strip()

    # ── Main menu / help ──
    if cmd in ("/start","/help","cmd_back"):
        await tg_async(
            f"🤖 *SMC ELITE EA*\n\n"
            f"Pair: `{state.pair_display}`\n"
            f"Risk: `{state.risk_pct*100:.0f}%`  "
            f"Mode: `{'💎 SMALL ACC' if state.small_acc_mode else 'STD'}`\n"
            f"Balance: `{state.account_balance:.2f} {state.account_currency}`\n\n"
            f"Select an option:",
            reply_markup=kb_main()
        )

    # ── Balance ──
    elif cmd in ("/balance","cmd_balance"):
        if state.ws:
            try: await get_account_info()
            except Exception: pass
        wr = f"{state.wins/(state.wins+state.losses)*100:.1f}%" \
            if (state.wins+state.losses)>0 else "N/A"
        await tg_async(
            f"💰 *Balance*\n"
            f"`{state.account_balance:.2f} {state.account_currency}`\n\n"
            f"Trades: `{state.trade_count}`  "
            f"W/L: `{state.wins}/{state.losses}`  WR: `{wr}`\n"
            f"Total P&L: `{state.total_pnl:+.2f} {state.account_currency}`",
            reply_markup=kb_main()
        )

    # ── Chart ──
    elif cmd in ("/chart","cmd_chart"):
        m15_count = len(state.m15_candles)
        path = generate_chart(state.m15_candles, "M15", chart_type="live")
        if path:
            pd_e = "🟢" if state.premium_discount=="DISCOUNT" \
                else "🔴" if state.premium_discount=="PREMIUM" else "⚪"
            await tg_async(
                f"📊 *{state.pair_display}  M15*\n"
                f"Price: `{state.current_price:.5f}`  "
                f"Bias: `{state.trend_bias}`\n"
                f"Zone: {pd_e}`{state.premium_discount}`  "
                f"OB Score: `{state.ob_score}/100`\n"
                f"IDM swept: {'✅' if state.active_idm and state.active_idm.get('swept') else '⏳'}\n"
                f"Trap swept: {'✅' if state.active_trap and state.active_trap.get('swept') else '⏳'}\n"
                f"Bars loaded: `H1:{len(state.h1_candles)} M15:{m15_count} M5:{len(state.m5_candles)}`",
                photo_path=path, reply_markup=kb_main()
            )
        elif m15_count == 0:
            await tg_async(
                f"⚠️ *No candle data received yet.*\n\n"
                f"H1: `{len(state.h1_candles)}` bars\n"
                f"M15: `{m15_count}` bars\n"
                f"M5: `{len(state.m5_candles)}` bars\n"
                f"Price: `{state.current_price}`\n\n"
                f"WS connected: `{'Yes' if state.ws else 'No'}`\n"
                f"Subscribed: `{state.subscribed_pair or 'None'}`\n\n"
                f"Please wait 10–20 seconds after startup, then try again.",
                reply_markup=kb_main()
            )
        else:
            await tg_async(
                f"⚠️ Only `{m15_count}` M15 bars loaded (need 20+).\n"
                f"Wait a moment and try again.",
                reply_markup=kb_main()
            )

    # ── History ──
    elif cmd in ("/history","cmd_history"):
        if not state.trade_history:
            await tg_async("📋 No trade history yet.", reply_markup=kb_main())
            return
        path  = generate_history_chart()
        lines = [f"📋 *History ({state.pair_display}) — last 10*\n"]
        for t in state.trade_history[-10:]:
            icon = "✅" if t["win"] else "❌"
            sign = "+" if t["pnl"]>0 else ""
            lines.append(
                f"{icon} `#{t['num']}` {t['direction']} "
                f"`{t['entry']:.5f}`→`{t['exit']:.5f}` "
                f"`{sign}{t['pnl']:.2f}`  _{t['ts']}_"
            )
        wr = f"{state.wins/(state.wins+state.losses)*100:.1f}%" \
            if (state.wins+state.losses)>0 else "N/A"
        lines.append(
            f"\n*Total P&L:* `{state.total_pnl:+.2f} {state.account_currency}`  "
            f"WR: `{wr}`"
        )
        await tg_async("\n".join(lines), photo_path=path, reply_markup=kb_main())

    # ── Status ──
    elif cmd in ("/status","cmd_status"):
        mode = "🛑 PAUSED" if state.paused else "🟢 SCANNING"
        sig_info = ""
        if state.last_signal:
            s = state.last_signal
            sig_info = (
                f"\n\n*Last Signal*\n"
                f"`{s['direction']}` @ `{s['entry']}`\n"
                f"SL:`{s['sl']}` TP1:`{s['tp1']}`\n"
                f"Logic: `{s['logic'][:80]}`"
            )
        await tg_async(
            f"⚡ *Status*\n"
            f"Mode: {mode}\n"
            f"Pair: `{state.pair_display}`\n"
            f"Price: `{state.current_price:.5f}`\n"
            f"Bias: `{state.trend_bias}`  Zone: `{state.premium_discount}`\n"
            f"Open: `{len(state.open_contracts)}` contracts\n"
            f"Balance: `{state.account_balance:.2f} {state.account_currency}`\n"
            f"Risk: `{state.risk_pct*100:.0f}%`  "
            f"Mode: `{'💎SMALL' if state.small_acc_mode else 'STD'}`"
            + sig_info,
            reply_markup=kb_main()
        )

    # ── Settings menu ──
    elif cmd in ("/settings","cmd_settings"):
        await tg_async(
            f"⚙️ *Settings*\n\n"
            f"Current Pair: `{state.pair_display}`\n"
            f"Risk: `{state.risk_pct*100:.0f}%`\n"
            f"TP: `{state.tp1_r}R / {state.tp2_r}R / {state.tp3_r}R`\n"
            f"Small Acc: `{'ON 💎' if state.small_acc_mode else 'OFF'}`",
            reply_markup=kb_settings()
        )

    # ── Pair menu ──
    elif cmd == "cmd_pair_menu":
        await tg_async("💱 *Select Trading Pair:*", reply_markup=kb_pair_menu())

    # ── Pair selection ──
    elif cmd.startswith("cmd_pair_"):
        key = cmd.replace("cmd_pair_","").upper()
        if key in PAIR_REGISTRY:
            old_key = state.pair_key
            state.pair_key = key
            state.small_acc_mode = False
            # Re-subscribe if WS is live
            if state.ws and state.subscribed_pair != state.pair_symbol:
                try:
                    await subscribe_pair(state.pair_symbol)
                    await tg_async(
                        f"💱 Pair changed to `{state.pair_display}`\n"
                        f"Re-subscribed to live data ✅",
                        reply_markup=kb_main()
                    )
                except Exception as e:
                    state.pair_key = old_key
                    await tg_async(f"❌ Pair change failed: {e}", reply_markup=kb_main())
            else:
                await tg_async(
                    f"💱 Pair set to `{state.pair_display}` (takes effect on next connect)",
                    reply_markup=kb_main()
                )

    # ── Small Account Mode ──
    elif cmd == "cmd_small_acc":
        if state.small_acc_mode:
            # Toggle off
            state.small_acc_mode = False
            state.risk_pct = 0.01
            state.tp1_r, state.tp2_r, state.tp3_r = 2.0, 4.0, 6.0
            await tg_async("💎 Small Acc Mode *OFF*\nReset to 1% risk, standard TPs.",
                           reply_markup=kb_settings())
        else:
            # Toggle on — GBP/USD recommended but gold allowed with tight risk
            state.small_acc_mode = True
            if state.pair_key == "XAUUSD":
                # Gold in small acc: 2% risk, tighter TP
                state.risk_pct = 0.02
                state.tp1_r, state.tp2_r, state.tp3_r = 1.5, 3.0, 5.0
                note = "XAU/USD at 2% risk (tight TP for gold)"
            else:
                state.pair_key = "GBPUSD"
                state.risk_pct = 0.05
                state.tp1_r, state.tp2_r, state.tp3_r = 1.5, 3.0, 4.5
                note = "GBP/USD at 5% risk"
            if state.ws and state.subscribed_pair != state.pair_symbol:
                try:
                    await subscribe_pair(state.pair_symbol)
                except Exception:
                    pass
            await tg_async(
                f"💎 *Small Acc Mode ON*\n{note}\n"
                f"TPs: `{state.tp1_r}R / {state.tp2_r}R / {state.tp3_r}R`",
                reply_markup=kb_settings()
            )

    # ── Risk buttons ──
    elif cmd == "cmd_risk_1":
        state.risk_pct = 0.01; state.small_acc_mode = False
        await tg_async("✅ Risk set to *1%*", reply_markup=kb_settings())

    elif cmd == "cmd_risk_3":
        state.risk_pct = 0.03; state.small_acc_mode = False
        await tg_async("✅ Risk set to *3%*", reply_markup=kb_settings())

    elif cmd == "cmd_risk_5":
        state.risk_pct = 0.05; state.small_acc_mode = False
        await tg_async("✅ Risk set to *5%*", reply_markup=kb_settings())

    # ── Emergency stop ──
    elif cmd in ("/close_all","cmd_stop"):
        state.paused = True
        n = await close_all()
        await tg_async(
            f"🛑 *Emergency Stop*\n"
            f"Closed `{n}` contract(s). Bot *PAUSED*.\n"
            f"Press ▶️ Resume to restart.",
            reply_markup=kb_main()
        )

    # ── Resume ──
    elif cmd in ("/resume","cmd_resume"):
        state.paused = False
        await tg_async("▶️ Bot *RESUMED* — scanning for setups.", reply_markup=kb_main())


# ══════════════════════════════════════════════════════
# WS ENGINE
# ══════════════════════════════════════════════════════
async def ws_reader_loop(ws):
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
        await get_account_info()
        log.info(f"Balance: {state.account_balance} {state.account_currency}")
        await subscribe_pair(state.pair_symbol)
        await tg_async(
            f"🤖 *SMC ELITE EA Online*\n"
            f"Pair: `{state.pair_display}`\n"
            f"Balance: `{state.account_balance:.2f} {state.account_currency}`\n"
            f"Risk: `{state.risk_pct*100:.0f}%`  "
            f"Mode: `{'💎 SMALL ACC' if state.small_acc_mode else 'STD'}`\n"
            f"TPs: `{state.tp1_r}R / {state.tp2_r}R / {state.tp3_r}R`",
            reply_markup=kb_main()
        )

    task = asyncio.ensure_future(setup())
    try:
        await ws_reader_loop(ws)
    finally:
        task.cancel()
        try: await task
        except (asyncio.CancelledError, Exception): pass


async def ws_connect_loop():
    delay = 5
    while state.running:
        try:
            log.info(f"Connecting WS: {DERIV_WS_BASE}")
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
            log.error(f"WS timeout: {e}. Retry {delay}s")
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
    wr = f"{state.wins/(state.wins+state.losses)*100:.1f}" \
        if (state.wins+state.losses)>0 else "0"
    return web.json_response({
        "status":     "running" if state.running else "stopped",
        "paused":     state.paused,
        "pair":       state.pair_key,
        "price":      state.current_price,
        "trend":      state.trend_bias,
        "zone":       state.premium_discount,
        "ob_score":   state.ob_score,
        "balance":    state.account_balance,
        "risk_pct":   state.risk_pct,
        "small_acc":  state.small_acc_mode,
        "trades":     state.trade_count,
        "wins":       state.wins,
        "losses":     state.losses,
        "winrate":    wr,
        "total_pnl":  state.total_pnl,
        "open":       len(state.open_contracts),
        "history":    len(state.trade_history),
        "h1_bars":    len(state.h1_candles),
        "m15_bars":   len(state.m15_candles),
    })


async def start_health_server():
    app = web.Application()
    app.router.add_get("/",       health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"Health server :{PORT}")


# ══════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════
async def main():
    log.info("╔══════════════════════════════════════╗")
    log.info("║   SMC ELITE EA  ·  Multi-Pair Forex  ║")
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
