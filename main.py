"""
XAU/USD Smart Money Concepts EA — Deriv WebSocket API
Production-Grade | SMC + HFT | Railway-Compatible
Upgrades: Telegram conflict fix, real-time chart loop, trade history screenshots
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
import numpy as np
import pandas as pd
import requests
import websockets
from aiohttp import web

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DERIV_APP_ID       = os.getenv("DERIV_APP_ID", "1089")
DERIV_API_TOKEN    = os.getenv("DERIV_API_TOKEN", "")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
PORT               = int(os.getenv("PORT", "8080"))
SYMBOL             = os.getenv("SYMBOL", "frxXAUUSD")
RISK_PCT           = float(os.getenv("RISK_PCT", "0.01"))
TP1_R              = float(os.getenv("TP1_R", "2"))
TP2_R              = float(os.getenv("TP2_R", "4"))
TP3_R              = float(os.getenv("TP3_R", "6"))
SWING_LOOKBACK     = int(os.getenv("SWING_LOOKBACK", "5"))
OB_LOOKBACK        = int(os.getenv("OB_LOOKBACK", "20"))
FVG_THRESHOLD      = float(os.getenv("FVG_THRESHOLD", "0.05"))
CHART_INTERVAL     = int(os.getenv("CHART_INTERVAL", "300"))   # auto-chart every 5 min
DERIV_WS_URL       = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("SMC-EA")

# ─────────────────────────────────────────────
# GLOBAL STATE
# ─────────────────────────────────────────────
class BotState:
    def __init__(self):
        self.running          = True
        self.paused           = False
        self.account_balance  = 0.0
        self.account_currency = "USD"
        self.open_contracts   = {}
        self.h1_candles       = deque(maxlen=200)
        self.m15_candles      = deque(maxlen=200)
        self.m5_candles       = deque(maxlen=200)
        self.current_price    = 0.0
        self.trend_bias       = "NEUTRAL"
        self.last_signal      = None
        self.ws               : Optional[websockets.WebSocketClientProtocol] = None
        self.req_id           = 1
        self.pending_requests = {}
        self.active_ob        = None
        self.active_fvg       = None
        self.active_trap      = None
        self.trade_count      = 0
        self.wins             = 0
        self.losses           = 0
        self.total_pnl        = 0.0
        # Trade history: list of dicts
        self.trade_history    = []   # max 50 entries

state = BotState()

# ─────────────────────────────────────────────
# INLINE KEYBOARD
# ─────────────────────────────────────────────
INLINE_KB = {
    "inline_keyboard": [
        [
            {"text": "💰 Balance",      "callback_data": "cmd_balance"},
            {"text": "📊 Chart",         "callback_data": "cmd_chart"},
        ],
        [
            {"text": "⚡ Status",        "callback_data": "cmd_status"},
            {"text": "📋 History",       "callback_data": "cmd_history"},
        ],
        [
            {"text": "🛑 Emergency Stop","callback_data": "cmd_stop"},
            {"text": "▶️ Resume",        "callback_data": "cmd_resume"},
        ],
    ]
}

# ─────────────────────────────────────────────
# TELEGRAM HELPERS
# ─────────────────────────────────────────────
def tg_send(text: str, photo_path: str = None, reply_markup=None):
    """Sync Telegram sender — always run via run_in_executor."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    markup = reply_markup if reply_markup is not None else INLINE_KB
    try:
        if photo_path:
            with open(photo_path, "rb") as f:
                r = requests.post(
                    f"{base}/sendPhoto",
                    data={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "caption": text[:1024],
                        "reply_markup": json.dumps(markup),
                        "parse_mode": "Markdown",
                    },
                    files={"photo": f},
                    timeout=20,
                )
        else:
            r = requests.post(
                f"{base}/sendMessage",
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": text,
                    "reply_markup": markup,
                    "parse_mode": "Markdown",
                },
                timeout=10,
            )
        if r.status_code not in (200, 201):
            log.warning(f"Telegram HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"tg_send failed: {type(e).__name__}: {e}")


def tg_answer_callback(callback_query_id: str, text: str = ""):
    if not TELEGRAM_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=5,
        )
    except Exception:
        pass


async def tg_send_async(text: str, photo_path: str = None, reply_markup=None):
    """Non-blocking async wrapper — safe anywhere in async code."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: tg_send(text, photo_path, reply_markup))


# ─────────────────────────────────────────────
# CHART GENERATION  (enhanced with history panel)
# ─────────────────────────────────────────────
def generate_chart(candles: deque, timeframe: str = "M15",
                   entry_price: float = None, exit_price: float = None,
                   direction: str = None, pnl: float = None,
                   chart_type: str = "live") -> Optional[str]:
    """
    chart_type: 'live' | 'entry' | 'exit'
    Returns path to saved PNG or None.
    """
    if len(candles) < 20:
        return None

    df = pd.DataFrame(list(candles)[-80:])
    df.columns = ["time", "open", "high", "low", "close"]
    df.reset_index(drop=True, inplace=True)

    fig, ax = plt.subplots(figsize=(16, 8), facecolor="#0d1117")
    ax.set_facecolor("#0d1117")

    # ── Candlesticks ──
    for i, row in df.iterrows():
        color = "#00e676" if row["close"] >= row["open"] else "#ff1744"
        body_lo = min(row["open"], row["close"])
        body_hi = max(row["open"], row["close"])
        ax.plot([i, i], [row["low"], row["high"]], color=color, linewidth=0.8, alpha=0.9)
        ax.add_patch(mpatches.FancyBboxPatch(
            (i - 0.3, body_lo), 0.6, max(body_hi - body_lo, 0.0001),
            boxstyle="square,pad=0", facecolor=color, edgecolor=color, alpha=0.85
        ))

    # ── Order Block ──
    if state.active_ob:
        ob = state.active_ob
        ob_color = "#00bcd4" if ob["type"] == "BULL" else "#ff9800"
        idx_s = max(0, len(df) - 30)
        ax.axhspan(ob["low"], ob["high"], xmin=idx_s / len(df),
                   alpha=0.18, color=ob_color, label=f"{ob['type']} OB")
        ax.axhline(ob["high"], color=ob_color, linestyle="--", linewidth=0.7, alpha=0.7)
        ax.axhline(ob["low"],  color=ob_color, linestyle="--", linewidth=0.7, alpha=0.7)
        ax.text(1, ob["high"], f"  {ob['type']} OB", color=ob_color,
                fontsize=7, va="bottom", fontfamily="monospace")

    # ── FVG ──
    if state.active_fvg:
        fvg = state.active_fvg
        ax.axhspan(fvg["low"], fvg["high"], alpha=0.13, color="#e040fb",
                   label="FVG Imbalance")
        ax.text(1, fvg["high"], "  FVG", color="#e040fb",
                fontsize=7, va="bottom", fontfamily="monospace")

    # ── Liquidity Trap ──
    if state.active_trap:
        trap = state.active_trap
        ax.axhline(trap["level"], color="#ffeb3b", linestyle=":",
                   linewidth=1.3, alpha=0.9, label="Liq. Trap")
        ax.text(1, trap["level"], f"  TRAP {trap['side']}", color="#ffeb3b",
                fontsize=7, va="bottom", fontfamily="monospace")

    # ── Active Signal Lines ──
    if state.last_signal:
        sig = state.last_signal
        e_color = "#00e676" if sig["direction"] == "BUY" else "#ff1744"
        ax.axhline(sig["entry"], color=e_color, linewidth=1.5,
                   linestyle="-", label=f"Entry {sig['direction']}")
        ax.axhline(sig["sl"],    color="#f44336", linewidth=1.0,
                   linestyle="--", label="SL")
        for tp_k, tp_c in [("tp1","#69f0ae"),("tp2","#40c4ff"),("tp3","#b388ff")]:
            if tp_k in sig:
                ax.axhline(sig[tp_k], color=tp_c, linewidth=0.8,
                           linestyle="-.", label=tp_k.upper())

    # ── Entry/Exit overlay for trade screenshots ──
    if entry_price:
        e_col = "#00e676" if direction == "BUY" else "#ff1744"
        ax.axhline(entry_price, color=e_col, linewidth=2.0,
                   linestyle="-", alpha=0.9, label=f"ENTRY {direction}")
        ax.annotate(f" ▶ ENTRY {entry_price:.2f}",
                    xy=(len(df)-1, entry_price), color=e_col,
                    fontsize=8, fontfamily="monospace",
                    xytext=(len(df)-10, entry_price))

    if exit_price:
        ex_col = "#00e676" if (pnl and pnl > 0) else "#ff1744"
        ax.axhline(exit_price, color=ex_col, linewidth=2.0,
                   linestyle="--", alpha=0.9, label=f"EXIT")
        pnl_str = f"+{pnl:.2f}" if pnl and pnl > 0 else f"{pnl:.2f}"
        ax.annotate(f" ◀ EXIT {exit_price:.2f}  P&L: {pnl_str}",
                    xy=(len(df)-1, exit_price), color=ex_col,
                    fontsize=8, fontfamily="monospace",
                    xytext=(len(df)-18, exit_price))

    # ── Swing markers ──
    swh, swl = detect_swing_points(df)
    for idx in swh:
        ax.plot(idx, df.iloc[idx]["high"] * 1.0002, "^",
                color="#00e676", markersize=4, alpha=0.6)
    for idx in swl:
        ax.plot(idx, df.iloc[idx]["low"] * 0.9998, "v",
                color="#ff1744", markersize=4, alpha=0.6)

    # ── Title & labels ──
    trend_clr = {"BULLISH":"#00e676","BEARISH":"#ff1744","NEUTRAL":"#90a4ae"}
    t_color = trend_clr.get(state.trend_bias, "#90a4ae")

    type_labels = {"live":"📡 LIVE","entry":"🎯 ENTRY","exit":"🏁 CLOSED"}
    type_label  = type_labels.get(chart_type, "")

    ax.set_title(
        f"{type_label}  XAU/USD · {timeframe} · Trend: {state.trend_bias} · "
        f"Price: {state.current_price:.2f}",
        color=t_color, fontsize=11, fontfamily="monospace", pad=10
    )
    ax.set_ylabel("Price (USD)", color="#90a4ae", fontsize=9)
    ax.tick_params(colors="#90a4ae", labelsize=8)
    for spine in ax.spines.values():
        spine.set_edgecolor("#1e2a38")
    ax.set_xlim(-1, len(df))
    ax.legend(loc="upper left", fontsize=7, facecolor="#161b22",
              edgecolor="#30363d", labelcolor="#cdd9e5")
    ax.grid(axis="y", color="#1e2a38", linewidth=0.5, alpha=0.5)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fig.text(0.99, 0.01, f"SMC-EA · {ts}", color="#444d56",
             fontsize=7, ha="right", fontfamily="monospace")

    path = f"/tmp/chart_{chart_type}.png"
    plt.tight_layout()
    plt.savefig(path, dpi=130, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def generate_history_chart() -> Optional[str]:
    """Generate a bar chart of recent trade P&L history."""
    history = state.trade_history[-20:]
    if not history:
        return None

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9),
                                    facecolor="#0d1117",
                                    gridspec_kw={"height_ratios": [2, 1]})
    for ax in (ax1, ax2):
        ax.set_facecolor("#0d1117")
        ax.tick_params(colors="#90a4ae", labelsize=8)
        for spine in ax.spines.values():
            spine.set_edgecolor("#1e2a38")

    # ── P&L bars ──
    labels = [f"#{t['num']}" for t in history]
    pnls   = [t["pnl"] for t in history]
    colors = ["#00e676" if p > 0 else "#ff1744" for p in pnls]
    bars = ax1.bar(labels, pnls, color=colors, alpha=0.85, edgecolor="#1e2a38")
    ax1.axhline(0, color="#444d56", linewidth=0.8)
    for bar, val in zip(bars, pnls):
        sign = "+" if val > 0 else ""
        ax1.text(bar.get_x() + bar.get_width()/2,
                 bar.get_height() + (0.05 if val >= 0 else -0.15),
                 f"{sign}{val:.2f}", ha="center", va="bottom",
                 color="#cdd9e5", fontsize=7, fontfamily="monospace")
    ax1.set_title(
        f"📋 Trade History  ·  {state.wins}W / {state.losses}L  ·  "
        f"Total P&L: {state.total_pnl:+.2f} {state.account_currency}  ·  "
        f"Balance: {state.account_balance:.2f}",
        color="#cdd9e5", fontsize=10, fontfamily="monospace", pad=8
    )
    ax1.set_ylabel("P&L (USD)", color="#90a4ae", fontsize=9)
    ax1.grid(axis="y", color="#1e2a38", linewidth=0.5, alpha=0.5)

    # ── Cumulative P&L line ──
    cumulative = np.cumsum(pnls)
    cum_color  = "#00e676" if cumulative[-1] >= 0 else "#ff1744"
    ax2.plot(labels, cumulative, color=cum_color, linewidth=1.8,
             marker="o", markersize=4)
    ax2.fill_between(labels, cumulative, alpha=0.15, color=cum_color)
    ax2.axhline(0, color="#444d56", linewidth=0.8)
    ax2.set_ylabel("Cumulative P&L", color="#90a4ae", fontsize=8)
    ax2.grid(axis="y", color="#1e2a38", linewidth=0.5, alpha=0.5)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fig.text(0.99, 0.01, f"SMC-EA History · {ts}", color="#444d56",
             fontsize=7, ha="right", fontfamily="monospace")

    path = "/tmp/chart_history.png"
    plt.tight_layout()
    plt.savefig(path, dpi=130, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


# ─────────────────────────────────────────────
# SMC ANALYSIS ENGINE
# ─────────────────────────────────────────────
def detect_swing_points(df: pd.DataFrame, n=None):
    n = n or SWING_LOOKBACK
    highs, lows = [], []
    for i in range(n, len(df) - n):
        if df["high"].iloc[i] == df["high"].iloc[i-n:i+n+1].max():
            highs.append(i)
        if df["low"].iloc[i] == df["low"].iloc[i-n:i+n+1].min():
            lows.append(i)
    return highs, lows


def detect_bos_choch(df: pd.DataFrame):
    highs, lows = detect_swing_points(df)
    if len(highs) < 2 or len(lows) < 2:
        return None
    last_sh, prev_sh = highs[-1], highs[-2]
    last_sl, prev_sl = lows[-1],  lows[-2]
    last_close = df["close"].iloc[-1]

    if last_close > df["high"].iloc[last_sh] and last_sh > prev_sh:
        return {"type":"BOS","direction":"BULLISH","level":df["high"].iloc[last_sh]}
    if last_close < df["low"].iloc[last_sl] and last_sl > prev_sl:
        return {"type":"BOS","direction":"BEARISH","level":df["low"].iloc[last_sl]}
    if (df["high"].iloc[last_sh] < df["high"].iloc[prev_sh] and
            last_close > df["high"].iloc[last_sh]):
        return {"type":"CHoCH","direction":"BULLISH","level":df["high"].iloc[last_sh]}
    if (df["low"].iloc[last_sl] > df["low"].iloc[prev_sl] and
            last_close < df["low"].iloc[last_sl]):
        return {"type":"CHoCH","direction":"BEARISH","level":df["low"].iloc[last_sl]}
    return None


def identify_order_block(df: pd.DataFrame, direction: str):
    lookback = min(OB_LOOKBACK, len(df) - 2)
    recent = df.iloc[-lookback:]
    if direction == "BULLISH":
        for i in range(len(recent)-3, 0, -1):
            c, nc = recent.iloc[i], recent.iloc[i+1]
            if c["close"] < c["open"] and nc["close"] > c["high"]:
                return {"type":"BULL","high":c["high"],"low":c["low"],"index":i}
    elif direction == "BEARISH":
        for i in range(len(recent)-3, 0, -1):
            c, nc = recent.iloc[i], recent.iloc[i+1]
            if c["close"] > c["open"] and nc["close"] < c["low"]:
                return {"type":"BEAR","high":c["high"],"low":c["low"],"index":i}
    return None


def identify_fvg(df: pd.DataFrame, ob: dict):
    if ob is None:
        return None
    lookback = min(OB_LOOKBACK, len(df)-3)
    recent = df.iloc[-lookback:]
    if ob["type"] == "BULL":
        for i in range(len(recent)-3, 0, -1):
            c1, c3 = recent.iloc[i], recent.iloc[i+2]
            gap_pct = (c3["low"] - c1["high"]) / c1["high"] * 100
            if c1["high"] < c3["low"] and gap_pct >= FVG_THRESHOLD:
                return {"type":"BULL","high":c3["low"],"low":c1["high"],"gap_pct":gap_pct}
    elif ob["type"] == "BEAR":
        for i in range(len(recent)-3, 0, -1):
            c1, c3 = recent.iloc[i], recent.iloc[i+2]
            gap_pct = (c1["low"] - c3["high"]) / c1["low"] * 100
            if c1["low"] > c3["high"] and gap_pct >= FVG_THRESHOLD:
                return {"type":"BEAR","high":c1["low"],"low":c3["high"],"gap_pct":gap_pct}
    return None


def detect_liquidity_trap(df: pd.DataFrame):
    if len(df) < 10:
        return None
    recent = df.iloc[-20:]
    tol = 0.0005
    highs = recent["high"].values
    for i in range(len(highs)-2, 0, -1):
        for j in range(i-1, max(i-6, 0), -1):
            if abs(highs[i]-highs[j])/highs[j] < tol:
                level = (highs[i]+highs[j])/2
                last = df.iloc[-1]
                if last["high"] > level and last["close"] < level:
                    return {"side":"SELL","level":level,"swept":True,"type":"EQL_HIGHS"}
    lows = recent["low"].values
    for i in range(len(lows)-2, 0, -1):
        for j in range(i-1, max(i-6, 0), -1):
            if abs(lows[i]-lows[j])/lows[j] < tol:
                level = (lows[i]+lows[j])/2
                last = df.iloc[-1]
                if last["low"] < level and last["close"] > level:
                    return {"side":"BUY","level":level,"swept":True,"type":"EQL_LOWS"}
    return None


def analyze_h1_trend():
    if len(state.h1_candles) < 30:
        return "NEUTRAL"
    df = pd.DataFrame(list(state.h1_candles))
    df.columns = ["time","open","high","low","close"]
    result = detect_bos_choch(df)
    if result:
        state.trend_bias = result["direction"]
    else:
        closes = df["close"].values[-20:]
        state.trend_bias = "BULLISH" if np.polyfit(np.arange(len(closes)), closes, 1)[0] > 0 else "BEARISH"
    return state.trend_bias


def compute_signal(timeframe="M15"):
    candles = state.m15_candles if timeframe == "M15" else state.m5_candles
    if len(candles) < 30:
        return None
    df = pd.DataFrame(list(candles))
    df.columns = ["time","open","high","low","close"]

    bias = analyze_h1_trend()
    if bias == "NEUTRAL":
        return None

    struct = detect_bos_choch(df)
    if struct is None or struct["direction"] != bias:
        return None

    ob = identify_order_block(df, bias)
    if ob is None:
        return None
    state.active_ob = ob

    fvg = identify_fvg(df, ob)
    if fvg is None:
        return None
    state.active_fvg = fvg

    trap = detect_liquidity_trap(df)
    state.active_trap = trap
    if trap is None or trap["side"] != ("BUY" if bias == "BULLISH" else "SELL"):
        return None

    if bias == "BULLISH":
        entry = ob["high"]
        sl    = ob["low"] * 0.9995
    else:
        entry = ob["low"]
        sl    = ob["high"] * 1.0005

    risk = abs(entry - sl)
    mult = 1 if bias == "BULLISH" else -1
    tp1  = entry + risk * TP1_R * mult
    tp2  = entry + risk * TP2_R * mult
    tp3  = entry + risk * TP3_R * mult
    units = round(state.account_balance * RISK_PCT / risk, 2) if risk > 0 and state.account_balance > 0 else 1.0

    signal = {
        "direction": "BUY" if bias == "BULLISH" else "SELL",
        "entry": round(entry, 3), "sl": round(sl, 3),
        "tp1": round(tp1, 3), "tp2": round(tp2, 3), "tp3": round(tp3, 3),
        "risk_r": round(risk, 4), "units": units,
        "struct": struct["type"], "ob": ob, "fvg": fvg, "trap": trap,
        "tf": timeframe, "bias": bias,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    state.last_signal = signal
    return signal


# ─────────────────────────────────────────────
# TRADE MANAGEMENT (BE + Trailing Stop)
# ─────────────────────────────────────────────
def check_trade_management():
    for cid, info in list(state.open_contracts.items()):
        sig = info.get("signal")
        if sig is None:
            continue
        price     = state.current_price
        direction = info["direction"]
        entry     = sig["entry"]
        tp1       = sig["tp1"]

        if not info["be_moved"]:
            triggered = (direction == "BUY" and price >= tp1) or \
                        (direction == "SELL" and price <= tp1)
            if triggered:
                info["be_moved"] = True
                sig["sl"] = entry
                log.info(f"[{cid}] BreakEven → SL={entry}")
                asyncio.ensure_future(tg_send_async(
                    f"✅ *BreakEven* `{cid}`\nSL moved to entry `{entry}`"
                ))

        # Trailing stop via last swing
        if len(state.m15_candles) >= 10:
            df = pd.DataFrame(list(state.m15_candles)[-30:])
            df.columns = ["time","open","high","low","close"]
            swh, swl = detect_swing_points(df, n=3)
            if direction == "BUY" and swl:
                t_sl = df["low"].iloc[swl[-1]] * 0.9998
                if t_sl > sig["sl"]:
                    sig["sl"] = t_sl
                    log.info(f"[{cid}] Trailing SL → {t_sl:.3f}")
            elif direction == "SELL" and swh:
                t_sl = df["high"].iloc[swh[-1]] * 1.0002
                if t_sl < sig["sl"]:
                    sig["sl"] = t_sl
                    log.info(f"[{cid}] Trailing SL → {t_sl:.3f}")


# ─────────────────────────────────────────────
# DERIV WEBSOCKET ENGINE
# ─────────────────────────────────────────────
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
        raise asyncio.TimeoutError(f"Timeout: keys={list(payload.keys())}")


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


async def subscribe_candles(symbol: str, granularity: int):
    await send_request({
        "ticks_history": symbol, "end": "latest",
        "count": 200, "granularity": granularity,
        "style": "candles", "subscribe": 1,
    })


async def open_contract(direction: str, amount: float):
    if state.paused:
        return None
    contract_type = "MULTUP" if direction == "BUY" else "MULTDOWN"
    try:
        resp = await send_request({
            "buy": 1, "price": round(amount, 2),
            "parameters": {
                "contract_type": contract_type, "symbol": SYMBOL,
                "amount": round(amount, 2), "currency": state.account_currency,
                "multiplier": 10, "basis": "stake", "stop_out": 1,
            },
        })
        if "error" in resp:
            log.error(f"Buy error: {resp['error']['message']}")
            return None
        contract_id = resp["buy"]["contract_id"]
        state.open_contracts[contract_id] = {
            "direction": direction, "entry": state.current_price,
            "amount": amount, "signal": state.last_signal,
            "be_moved": False, "opened_at": time.time(),
        }
        state.trade_count += 1
        log.info(f"✅ Contract opened: {contract_id} [{direction}] ${amount:.2f}")
        return contract_id
    except Exception as e:
        log.error(f"open_contract: {e}")
        return None


async def close_contract(contract_id: str):
    try:
        resp = await send_request({"sell": contract_id, "price": 0})
        if "error" in resp:
            log.error(f"Close error: {resp['error']['message']}")
            return False
        state.open_contracts.pop(contract_id, None)
        log.info(f"🔴 Closed: {contract_id}")
        return True
    except Exception as e:
        log.error(f"close_contract: {e}")
        return False


async def close_all_contracts():
    ids = list(state.open_contracts.keys())
    for cid in ids:
        await close_contract(cid)
    return len(ids)


# ─────────────────────────────────────────────
# WEBSOCKET MESSAGE HANDLER
# ─────────────────────────────────────────────
def parse_candle(msg: dict):
    if "candles" in msg:
        return [(c["epoch"], float(c["open"]), float(c["high"]),
                 float(c["low"]), float(c["close"])) for c in msg["candles"]]
    if "ohlc" in msg:
        c = msg["ohlc"]
        return [(int(c["epoch"]), float(c["open"]), float(c["high"]),
                 float(c["low"]), float(c["close"]))]
    return None


async def handle_message(msg: dict):
    req_id = msg.get("req_id")
    if req_id and req_id in state.pending_requests:
        fut = state.pending_requests.pop(req_id)
        if not fut.done():
            fut.set_result(msg)
        return

    mtype = msg.get("msg_type", "")

    if mtype == "ohlc":
        gran = msg["ohlc"].get("granularity", 0)
        candle = parse_candle(msg)
        if candle:
            if gran == 3600:
                state.h1_candles.extend(candle)
            elif gran == 900:
                state.m15_candles.extend(candle)
                state.current_price = candle[-1][4]
            elif gran == 300:
                state.m5_candles.extend(candle)
                state.current_price = candle[-1][4]

    elif mtype == "candles":
        gran = msg.get("echo_req", {}).get("granularity", 0)
        candles = parse_candle(msg)
        if candles:
            if gran == 3600:
                state.h1_candles.extend(candles)
            elif gran == 900:
                state.m15_candles.extend(candles)
            elif gran == 300:
                state.m5_candles.extend(candles)

    elif mtype == "tick":
        state.current_price = float(msg["tick"]["quote"])

    elif mtype == "proposal_open_contract":
        poc = msg.get("proposal_open_contract", {})
        cid = str(poc.get("contract_id", ""))
        if cid in state.open_contracts:
            profit = float(poc.get("profit", 0))
            status = poc.get("status", "")
            exit_spot = float(poc.get("exit_tick", state.current_price) or state.current_price)

            if status in ("sold", "expired"):
                info = state.open_contracts.pop(cid, {})
                if profit > 0:
                    state.wins += 1
                else:
                    state.losses += 1
                state.total_pnl += profit

                # Record history
                trade_num = state.trade_count
                history_entry = {
                    "num":       trade_num,
                    "id":        cid,
                    "direction": info.get("direction","?"),
                    "entry":     info.get("entry", 0),
                    "exit":      exit_spot,
                    "pnl":       round(profit, 2),
                    "win":       profit > 0,
                    "ts":        datetime.now(timezone.utc).strftime("%m/%d %H:%M"),
                }
                state.trade_history.append(history_entry)
                if len(state.trade_history) > 50:
                    state.trade_history.pop(0)

                # Generate exit chart screenshot
                exit_chart = generate_chart(
                    state.m15_candles, "M15",
                    entry_price=info.get("entry"),
                    exit_price=exit_spot,
                    direction=info.get("direction"),
                    pnl=profit,
                    chart_type="exit"
                )
                result_emoji = "✅ WIN" if profit > 0 else "❌ LOSS"
                pnl_str = f"+{profit:.2f}" if profit > 0 else f"{profit:.2f}"
                msg_text = (
                    f"{result_emoji}  Trade `#{trade_num}` Closed\n\n"
                    f"Contract: `{cid}`\n"
                    f"Direction: `{info.get('direction','?')}`\n"
                    f"Entry: `{info.get('entry', 0):.2f}`\n"
                    f"Exit:  `{exit_spot:.2f}`\n"
                    f"P&L:   `{pnl_str} {state.account_currency}`\n\n"
                    f"Record: `{state.wins}W / {state.losses}L`\n"
                    f"Total P&L: `{state.total_pnl:+.2f} {state.account_currency}`\n"
                    f"Balance: `{state.account_balance:.2f} {state.account_currency}`"
                )
                asyncio.ensure_future(
                    tg_send_async(msg_text, photo_path=exit_chart)
                )
                log.info(f"Trade #{trade_num} closed. PnL={pnl_str}")

    elif mtype == "balance":
        state.account_balance  = msg["balance"]["balance"]
        state.account_currency = msg["balance"]["currency"]

    elif "error" in msg:
        log.warning(f"WS API error: {msg['error'].get('message','?')}")


# ─────────────────────────────────────────────
# REAL-TIME CHART LOOP
# ─────────────────────────────────────────────
async def chart_broadcast_loop():
    """Send a live chart screenshot to Telegram every CHART_INTERVAL seconds."""
    await asyncio.sleep(30)  # Wait for initial data
    while state.running:
        try:
            if state.current_price > 0 and len(state.m15_candles) >= 20:
                path = generate_chart(state.m15_candles, "M15", chart_type="live")
                if path:
                    bias_e = "📈" if state.trend_bias=="BULLISH" else "📉" if state.trend_bias=="BEARISH" else "➡️"
                    open_n = len(state.open_contracts)
                    caption = (
                        f"{bias_e} *XAU/USD Real-Time · M15*\n"
                        f"Price: `{state.current_price:.2f}`  Bias: `{state.trend_bias}`\n"
                        f"OB: {'✅' if state.active_ob else '—'}  "
                        f"FVG: {'✅' if state.active_fvg else '—'}  "
                        f"Trap: {'✅' if state.active_trap else '—'}\n"
                        f"Open Trades: `{open_n}`  "
                        f"Balance: `{state.account_balance:.2f} {state.account_currency}`"
                    )
                    await tg_send_async(caption, photo_path=path)
                    log.info("📊 Auto chart sent to Telegram")
        except Exception as e:
            log.error(f"Chart broadcast error: {e}")
        await asyncio.sleep(CHART_INTERVAL)


# ─────────────────────────────────────────────
# TELEGRAM POLLING — Conflict-safe
# ─────────────────────────────────────────────
async def telegram_poll_loop():
    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN not set — Telegram disabled.")
        return

    # ── Delete webhook + drop pending to clear any conflict ──
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    loop = asyncio.get_event_loop()
    try:
        def clear_webhook():
            requests.post(f"{base}/deleteWebhook",
                          json={"drop_pending_updates": True}, timeout=10)
        await loop.run_in_executor(None, clear_webhook)
        log.info("Telegram webhook cleared (conflict prevention)")
    except Exception as e:
        log.warning(f"Webhook clear failed: {e}")

    await asyncio.sleep(2)  # Give Telegram time to release any other poller

    offset = 0
    CONSECUTIVE_ERRORS = 0

    while state.running:
        try:
            def do_poll():
                return requests.get(
                    f"{base}/getUpdates",
                    params={"offset": offset, "timeout": 20, "allowed_updates": ["message","callback_query"]},
                    timeout=25,
                )
            r = await loop.run_in_executor(None, do_poll)
            data = r.json()

            if not data.get("ok"):
                desc = data.get("description", str(data))
                log.error(f"Telegram poll error: {desc}")
                # If still conflict, wait longer then retry
                if "Conflict" in desc:
                    log.warning("Conflict detected — waiting 30s before retry...")
                    await asyncio.sleep(30)
                    # Try clearing webhook again
                    await loop.run_in_executor(None, clear_webhook)
                    await asyncio.sleep(5)
                else:
                    await asyncio.sleep(10)
                CONSECUTIVE_ERRORS += 1
                continue

            CONSECUTIVE_ERRORS = 0
            updates = data.get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                await handle_telegram_update(update)

        except Exception as e:
            CONSECUTIVE_ERRORS += 1
            log.error(f"Telegram poll exception: {type(e).__name__}: {e}")
            wait = min(5 * CONSECUTIVE_ERRORS, 60)
            await asyncio.sleep(wait)
            continue

        await asyncio.sleep(0.5)


async def handle_telegram_update(update: dict):
    if "message" in update:
        text    = update["message"].get("text", "").strip()
        chat_id = str(update["message"]["chat"]["id"])
        if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
            return
        await process_command(text)
    elif "callback_query" in update:
        cq      = update["callback_query"]
        data    = cq.get("data", "")
        cqid    = cq["id"]
        chat_id = str(cq["message"]["chat"]["id"])
        if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
            tg_answer_callback(cqid, "Unauthorized")
            return
        tg_answer_callback(cqid)
        await process_command(data)


async def process_command(cmd: str):
    cmd = cmd.lower().strip()

    if cmd in ("/start", "/help"):
        await tg_send_async(
            "🤖 *SMC-EA XAU/USD Bot*\n\n"
            "Smart Money Concepts Expert Advisor\n"
            f"Symbol: `{SYMBOL}` | Risk: `{RISK_PCT*100:.0f}%`\n"
            f"Auto-chart every `{CHART_INTERVAL//60}` min\n\n"
            "Use the buttons below:"
        )

    elif cmd in ("/balance", "cmd_balance"):
        if state.ws is not None:
            try:
                await get_account_info()
            except Exception:
                pass
        await tg_send_async(
            f"💰 *Account Balance*\n"
            f"`{state.account_balance:.2f} {state.account_currency}`\n"
            f"Trades: `{state.trade_count}`  W/L: `{state.wins}/{state.losses}`\n"
            f"Total P&L: `{state.total_pnl:+.2f} {state.account_currency}`"
        )

    elif cmd in ("/chart", "cmd_chart"):
        path = generate_chart(state.m15_candles, "M15", chart_type="live")
        if path:
            bias_e = "📈" if state.trend_bias=="BULLISH" else "📉" if state.trend_bias=="BEARISH" else "➡️"
            await tg_send_async(
                f"{bias_e} *XAU/USD M15 Live Chart*\n"
                f"Bias: `{state.trend_bias}`  Price: `{state.current_price:.2f}`\n"
                f"OB: {'✅' if state.active_ob else '—'}  "
                f"FVG: {'✅' if state.active_fvg else '—'}  "
                f"Trap: {'✅' if state.active_trap else '—'}",
                photo_path=path
            )
        else:
            await tg_send_async("⚠️ Not enough data yet. Please wait...")

    elif cmd in ("/history", "cmd_history"):
        if not state.trade_history:
            await tg_send_async("📋 No trade history yet.")
            return
        path = generate_history_chart()
        # Build text summary
        lines = ["📋 *Trade History (last 10)*\n"]
        for t in state.trade_history[-10:]:
            icon = "✅" if t["win"] else "❌"
            sign = "+" if t["pnl"] > 0 else ""
            lines.append(
                f"{icon} `#{t['num']}` {t['direction']} "
                f"@ `{t['entry']:.2f}` → `{t['exit']:.2f}`  "
                f"`{sign}{t['pnl']:.2f}`  _{t['ts']}_"
            )
        lines.append(
            f"\n*Total P&L:* `{state.total_pnl:+.2f} {state.account_currency}`\n"
            f"*W/L:* `{state.wins}/{state.losses}`"
        )
        await tg_send_async("\n".join(lines), photo_path=path)

    elif cmd in ("/status", "cmd_status"):
        mode = "🛑 PAUSED" if state.paused else "🟢 SCANNING"
        open_c = len(state.open_contracts)
        sig_info = ""
        if state.last_signal:
            s = state.last_signal
            sig_info = (
                f"\n\n*Last Signal*\n"
                f"`{s['direction']}` Entry:`{s['entry']}` SL:`{s['sl']}`\n"
                f"Struct:`{s['struct']}` TF:`{s['tf']}`"
            )
        await tg_send_async(
            f"⚡ *Bot Status*\n"
            f"Mode: {mode}\n"
            f"Price: `{state.current_price:.2f}`\n"
            f"Trend: `{state.trend_bias}`\n"
            f"Open: `{open_c}` contract(s)\n"
            f"Balance: `{state.account_balance:.2f} {state.account_currency}`\n"
            f"W/L: `{state.wins}/{state.losses}`  P&L: `{state.total_pnl:+.2f}`"
            + sig_info
        )

    elif cmd in ("/close_all", "cmd_stop"):
        state.paused = True
        n = await close_all_contracts()
        await tg_send_async(
            f"🛑 *Emergency Stop*\n"
            f"Closed `{n}` contract(s). Bot *PAUSED*.\n"
            f"Press ▶️ Resume or send /resume to restart."
        )

    elif cmd in ("/resume", "cmd_resume"):
        state.paused = False
        await tg_send_async("▶️ Bot *RESUMED* — scanning for setups.")


# ─────────────────────────────────────────────
# MAIN TRADING LOOP
# ─────────────────────────────────────────────
async def trading_loop():
    await asyncio.sleep(20)
    last_signal_ts = 0
    SIGNAL_COOLDOWN = 300

    while state.running:
        try:
            if state.paused or state.current_price == 0:
                await asyncio.sleep(30)
                continue

            check_trade_management()

            if time.time() - last_signal_ts < SIGNAL_COOLDOWN:
                await asyncio.sleep(30)
                continue

            signal = compute_signal("M15")
            if signal:
                m5_sig = compute_signal("M5")
                if m5_sig and m5_sig["direction"] == signal["direction"]:
                    log.info(
                        f"🎯 SIGNAL {signal['direction']} "
                        f"Entry:{signal['entry']} SL:{signal['sl']} TP1:{signal['tp1']}"
                    )
                    # Entry chart screenshot
                    entry_chart = generate_chart(
                        state.m15_candles, "M15",
                        entry_price=signal["entry"],
                        direction=signal["direction"],
                        chart_type="entry"
                    )
                    tg_msg = (
                        f"🎯 *NEW SIGNAL — XAU/USD*\n\n"
                        f"Direction: `{signal['direction']}`\n"
                        f"Entry:    `{signal['entry']}`\n"
                        f"SL:       `{signal['sl']}`\n"
                        f"TP1 (2R): `{signal['tp1']}`\n"
                        f"TP2 (4R): `{signal['tp2']}`\n"
                        f"TP3 (6R): `{signal['tp3']}`\n\n"
                        f"H1 Bias:  `{signal['bias']}`\n"
                        f"Struct:   `{signal['struct']}`\n"
                        f"OB: `{signal['ob']['type']}` "
                        f"`{signal['ob']['high']:.2f}–{signal['ob']['low']:.2f}`\n"
                        f"FVG Gap:  `{signal['fvg']['gap_pct']:.3f}%`\n"
                        f"Trap:     `{signal['trap']['type']}` ✅\n"
                        f"Stake:    `{signal['units']:.2f} {state.account_currency}`"
                    )
                    await tg_send_async(tg_msg, photo_path=entry_chart)

                    # Execute
                    if state.account_balance > 0:
                        stake = max(1.0, round(state.account_balance * RISK_PCT, 2))
                        cid = await open_contract(signal["direction"], stake)
                        if cid:
                            last_signal_ts = time.time()
                            await send_request({
                                "proposal_open_contract": 1,
                                "contract_id": cid, "subscribe": 1
                            })

        except Exception as e:
            log.error(f"Trading loop: {e}\n{traceback.format_exc()}")

        await asyncio.sleep(30)


# ─────────────────────────────────────────────
# WEBSOCKET ENGINE
# ─────────────────────────────────────────────
async def ws_reader_loop(ws):
    async for raw in ws:
        try:
            await handle_message(json.loads(raw))
        except Exception as e:
            log.error(f"Handler error: {type(e).__name__}: {e}")


async def ws_setup_and_run(ws):
    state.ws = ws

    async def setup():
        await asyncio.sleep(0.1)
        log.info("Running authorize...")
        await authorize()
        log.info("Authorize OK. Getting balance...")
        await get_account_info()
        log.info("Subscribing candles...")
        await subscribe_candles(SYMBOL, 3600)
        await subscribe_candles(SYMBOL, 900)
        await subscribe_candles(SYMBOL, 300)
        log.info(f"✅ Subscribed | Balance: {state.account_balance} {state.account_currency}")
        await tg_send_async(
            f"🤖 *SMC-EA Online*\n"
            f"Symbol: `{SYMBOL}`\n"
            f"Balance: `{state.account_balance:.2f} {state.account_currency}`\n"
            f"Risk: `{RISK_PCT*100:.0f}%`  TPs: `{TP1_R}R/{TP2_R}R/{TP3_R}R`\n"
            f"Auto-chart: every `{CHART_INTERVAL//60}` min"
        )

    setup_task = asyncio.ensure_future(setup())
    try:
        await ws_reader_loop(ws)
    finally:
        setup_task.cancel()
        try:
            await setup_task
        except (asyncio.CancelledError, Exception):
            pass


async def ws_connect_loop():
    retry_delay = 5
    while state.running:
        try:
            log.info(f"Connecting: {DERIV_WS_URL}")
            async with websockets.connect(
                DERIV_WS_URL,
                ping_interval=25, ping_timeout=10,
                close_timeout=10, open_timeout=15,
            ) as ws:
                retry_delay = 5
                await ws_setup_and_run(ws)
        except websockets.ConnectionClosed as e:
            log.warning(f"WS closed: {e}. Retry in {retry_delay}s")
        except asyncio.TimeoutError as e:
            log.error(f"WS timeout: {e}. Retry in {retry_delay}s")
        except Exception as e:
            log.error(f"WS error: {type(e).__name__}: {e}. Retry in {retry_delay}s")
            log.error(traceback.format_exc())
        finally:
            state.ws = None
            for fut in state.pending_requests.values():
                if not fut.done():
                    fut.cancel()
            state.pending_requests.clear()
        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 60)


# ─────────────────────────────────────────────
# HEALTH SERVER
# ─────────────────────────────────────────────
async def health_handler(request):
    return web.json_response({
        "status":   "running" if state.running else "stopped",
        "paused":   state.paused,
        "balance":  state.account_balance,
        "currency": state.account_currency,
        "price":    state.current_price,
        "trend":    state.trend_bias,
        "trades":   state.trade_count,
        "wins":     state.wins,
        "losses":   state.losses,
        "total_pnl":state.total_pnl,
        "open":     len(state.open_contracts),
        "history":  len(state.trade_history),
        "h1_bars":  len(state.h1_candles),
        "m15_bars": len(state.m15_candles),
    })


async def start_health_server():
    app = web.Application()
    app.router.add_get("/",       health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"Health server on :{PORT}")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
async def main():
    log.info("=" * 55)
    log.info("  SMC-EA  |  XAU/USD  |  Deriv WebSocket  ")
    log.info("=" * 55)
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
