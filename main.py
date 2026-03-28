"""
XAU/USD Smart Money Concepts EA — Deriv WebSocket API
Production-Grade | SMC + HFT | Railway-Compatible
"""

import asyncio
import json
import logging
import os
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from io import BytesIO
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
# CONFIG — all from environment variables
# ─────────────────────────────────────────────
DERIV_APP_ID       = os.getenv("DERIV_APP_ID", "1089")
DERIV_API_TOKEN    = os.getenv("DERIV_API_TOKEN", "")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
PORT               = int(os.getenv("PORT", "8080"))
SYMBOL             = os.getenv("SYMBOL", "frxXAUUSD")
RISK_PCT           = float(os.getenv("RISK_PCT", "0.01"))          # 1%
TP1_R              = float(os.getenv("TP1_R", "2"))
TP2_R              = float(os.getenv("TP2_R", "4"))
TP3_R              = float(os.getenv("TP3_R", "6"))
SWING_LOOKBACK     = int(os.getenv("SWING_LOOKBACK", "5"))
DERIV_WS_URL       = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"
OB_LOOKBACK        = int(os.getenv("OB_LOOKBACK", "20"))
FVG_THRESHOLD      = float(os.getenv("FVG_THRESHOLD", "0.05"))     # % gap
TRADE_DURATION     = int(os.getenv("TRADE_DURATION", "3600"))      # seconds

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
        self.open_contracts   = {}          # contract_id → info dict
        self.h1_candles       = deque(maxlen=200)
        self.m15_candles      = deque(maxlen=200)
        self.m5_candles       = deque(maxlen=200)
        self.current_price    = 0.0
        self.trend_bias       = "NEUTRAL"   # BULLISH / BEARISH / NEUTRAL
        self.last_signal      = None        # dict with signal details
        self.ws               : Optional[websockets.WebSocketClientProtocol] = None
        self.req_id           = 1
        self.pending_requests = {}          # req_id → asyncio.Future
        self.active_ob        = None        # last identified OB
        self.active_fvg       = None
        self.active_trap      = None
        self.trade_count      = 0
        self.wins             = 0
        self.losses           = 0

state = BotState()


# ─────────────────────────────────────────────
# TELEGRAM HELPERS
# ─────────────────────────────────────────────
INLINE_KB = {
    "inline_keyboard": [
        [
            {"text": "💰 Check Balance",  "callback_data": "cmd_balance"},
            {"text": "📊 Get Chart",       "callback_data": "cmd_chart"},
        ],
        [
            {"text": "⚡ Bot Status",       "callback_data": "cmd_status"},
            {"text": "🛑 Emergency Stop",   "callback_data": "cmd_stop"},
        ],
    ]
}


def tg_send(text: str, photo_path: str = None, reply_markup=None):
    """Fire-and-forget Telegram message (sync, runs in executor)."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    try:
        if photo_path:
            with open(photo_path, "rb") as f:
                r = requests.post(
                    f"{base}/sendPhoto",
                    data={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "caption": text[:1024],
                        "reply_markup": json.dumps(reply_markup or INLINE_KB),
                        "parse_mode": "Markdown",
                    },
                    files={"photo": f},
                    timeout=15,
                )
        else:
            r = requests.post(
                f"{base}/sendMessage",
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": text,
                    "reply_markup": reply_markup or INLINE_KB,
                    "parse_mode": "Markdown",
                },
                timeout=10,
            )
        if r.status_code != 200:
            log.warning(f"Telegram error: {r.text}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


def tg_answer_callback(callback_query_id: str, text: str = ""):
    """Answer a callback query to remove the spinner on the button."""
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
    """Non-blocking async wrapper for tg_send — safe to call from async code."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, lambda: tg_send(text, photo_path, reply_markup)
    )


# ─────────────────────────────────────────────
# CHART GENERATION
# ─────────────────────────────────────────────
def generate_chart(candles: deque, timeframe="M15") -> str:
    """Generate and save chart.png with OBs, FVGs, traps, and entry lines."""
    if len(candles) < 20:
        return None

    df = pd.DataFrame(list(candles)[-80:])
    df.columns = ["time", "open", "high", "low", "close"]
    df["time"] = pd.to_datetime(df["time"], unit="s")

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
        color = "#00bcd4" if ob["type"] == "BULL" else "#ff9800"
        idx_start = max(0, len(df) - 30)
        ax.axhspan(ob["low"], ob["high"], xmin=idx_start / len(df),
                   alpha=0.18, color=color, label=f"{ob['type']} OB")
        ax.axhline(ob["high"], color=color, linestyle="--", linewidth=0.7, alpha=0.6)
        ax.axhline(ob["low"],  color=color, linestyle="--", linewidth=0.7, alpha=0.6)
        ax.text(2, ob["high"], f" {ob['type']} OB", color=color, fontsize=7,
                va="bottom", fontfamily="monospace")

    # ── FVG ──
    if state.active_fvg:
        fvg = state.active_fvg
        ax.axhspan(fvg["low"], fvg["high"], alpha=0.12, color="#e040fb",
                   label="FVG / Imbalance")
        ax.text(2, fvg["high"], " FVG", color="#e040fb", fontsize=7,
                va="bottom", fontfamily="monospace")

    # ── Trap ──
    if state.active_trap:
        trap = state.active_trap
        ax.axhline(trap["level"], color="#ffeb3b", linestyle=":", linewidth=1.2,
                   alpha=0.9, label="Liquidity Trap")
        ax.text(2, trap["level"], f" TRAP {trap['side']}", color="#ffeb3b",
                fontsize=7, va="bottom", fontfamily="monospace")

    # ── Signal ──
    if state.last_signal:
        sig = state.last_signal
        entry_color = "#00e676" if sig["direction"] == "BUY" else "#ff1744"
        ax.axhline(sig["entry"], color=entry_color, linewidth=1.4,
                   linestyle="-", label=f"Entry ({sig['direction']})")
        ax.axhline(sig["sl"],    color="#f44336", linewidth=0.9,
                   linestyle="--", label="Stop Loss")
        for tp_key, tp_color in [("tp1", "#69f0ae"), ("tp2", "#40c4ff"), ("tp3", "#b388ff")]:
            if tp_key in sig:
                ax.axhline(sig[tp_key], color=tp_color, linewidth=0.8,
                           linestyle="-.", label=tp_key.upper())

    # ── Swing H/L ──
    highs, lows = detect_swing_points(df)
    for idx in highs:
        ax.plot(idx, df.iloc[idx]["high"] * 1.0002, "^", color="#00e676",
                markersize=5, alpha=0.7)
    for idx in lows:
        ax.plot(idx, df.iloc[idx]["low"] * 0.9998, "v", color="#ff1744",
                markersize=5, alpha=0.7)

    # ── Trend label ──
    trend_clr = {"BULLISH": "#00e676", "BEARISH": "#ff1744", "NEUTRAL": "#90a4ae"}
    bias_color = trend_clr.get(state.trend_bias, "#90a4ae")
    ax.set_title(
        f"XAU/USD  ·  {timeframe}  ·  Trend: {state.trend_bias}  ·  "
        f"Price: {state.current_price:.2f}",
        color=bias_color, fontsize=11, fontfamily="monospace", pad=10
    )

    # ── Style ──
    ax.tick_params(colors="#90a4ae", labelsize=8)
    for spine in ax.spines.values():
        spine.set_edgecolor("#1e2a38")
    ax.set_xlim(-1, len(df))
    ax.set_ylabel("Price (USD)", color="#90a4ae", fontsize=9)
    ax.legend(loc="upper left", fontsize=7, facecolor="#161b22",
              edgecolor="#30363d", labelcolor="#cdd9e5")
    ax.grid(axis="y", color="#1e2a38", linewidth=0.5, alpha=0.5)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    fig.text(0.99, 0.01, f"SMC-EA  ·  {ts}", color="#444d56",
             fontsize=7, ha="right", fontfamily="monospace")

    path = "/tmp/chart.png"
    plt.tight_layout()
    plt.savefig(path, dpi=130, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


# ─────────────────────────────────────────────
# SMC ANALYSIS ENGINE
# ─────────────────────────────────────────────
def detect_swing_points(df: pd.DataFrame, n=None):
    """Return indices of swing highs and swing lows."""
    n = n or SWING_LOOKBACK
    highs, lows = [], []
    for i in range(n, len(df) - n):
        window_h = df["high"].iloc[i - n:i + n + 1]
        window_l = df["low"].iloc[i - n:i + n + 1]
        if df["high"].iloc[i] == window_h.max():
            highs.append(i)
        if df["low"].iloc[i] == window_l.min():
            lows.append(i)
    return highs, lows


def detect_bos_choch(df: pd.DataFrame):
    """
    Detect BOS (Break of Structure) and CHoCH (Change of Character).
    Returns: dict with 'type' ('BOS'/'CHoCH'), 'direction', 'level', 'index'
    """
    highs, lows = detect_swing_points(df)
    if len(highs) < 2 or len(lows) < 2:
        return None

    last_sh = highs[-1]
    prev_sh = highs[-2]
    last_sl = lows[-1]
    prev_sl = lows[-2]

    last_close = df["close"].iloc[-1]
    last_open  = df["open"].iloc[-1]

    result = None

    # Bullish BOS: price breaks above last swing high
    if last_close > df["high"].iloc[last_sh] and last_sh > prev_sh:
        result = {"type": "BOS", "direction": "BULLISH",
                  "level": df["high"].iloc[last_sh], "index": len(df) - 1}

    # Bearish BOS: price breaks below last swing low
    elif last_close < df["low"].iloc[last_sl] and last_sl > prev_sl:
        result = {"type": "BOS", "direction": "BEARISH",
                  "level": df["low"].iloc[last_sl], "index": len(df) - 1}

    # CHoCH: structure flip
    elif (df["high"].iloc[last_sh] < df["high"].iloc[prev_sh] and
          last_close > df["high"].iloc[last_sh]):
        result = {"type": "CHoCH", "direction": "BULLISH",
                  "level": df["high"].iloc[last_sh], "index": len(df) - 1}

    elif (df["low"].iloc[last_sl] > df["low"].iloc[prev_sl] and
          last_close < df["low"].iloc[last_sl]):
        result = {"type": "CHoCH", "direction": "BEARISH",
                  "level": df["low"].iloc[last_sl], "index": len(df) - 1}

    return result


def identify_order_block(df: pd.DataFrame, direction: str):
    """
    Find the last valid Order Block before a BOS.
    Bull OB: last bearish candle before bullish impulse.
    Bear OB: last bullish candle before bearish impulse.
    """
    lookback = min(OB_LOOKBACK, len(df) - 2)
    recent = df.iloc[-lookback:]

    if direction == "BULLISH":
        # Find last down candle followed by strong up move
        for i in range(len(recent) - 3, 0, -1):
            candle = recent.iloc[i]
            next_c = recent.iloc[i + 1]
            if candle["close"] < candle["open"]:     # bearish candle
                if next_c["close"] > candle["high"]:  # next breaks up
                    return {
                        "type": "BULL",
                        "high": candle["high"],
                        "low":  candle["low"],
                        "index": i,
                    }

    elif direction == "BEARISH":
        for i in range(len(recent) - 3, 0, -1):
            candle = recent.iloc[i]
            next_c = recent.iloc[i + 1]
            if candle["close"] > candle["open"]:     # bullish candle
                if next_c["close"] < candle["low"]:  # next breaks down
                    return {
                        "type": "BEAR",
                        "high": candle["high"],
                        "low":  candle["low"],
                        "index": i,
                    }
    return None


def identify_fvg(df: pd.DataFrame, ob: dict):
    """
    Fair Value Gap: 3-candle imbalance.
    Bull FVG: candle[i].high < candle[i+2].low → gap between them.
    Bear FVG: candle[i].low  > candle[i+2].high → gap between them.
    """
    if ob is None:
        return None

    lookback = min(OB_LOOKBACK, len(df) - 3)
    recent = df.iloc[-lookback:]

    if ob["type"] == "BULL":
        for i in range(len(recent) - 3, 0, -1):
            c1, c3 = recent.iloc[i], recent.iloc[i + 2]
            gap_pct = (c3["low"] - c1["high"]) / c1["high"] * 100
            if c1["high"] < c3["low"] and gap_pct >= FVG_THRESHOLD:
                return {"type": "BULL", "high": c3["low"],
                        "low": c1["high"], "gap_pct": gap_pct}

    elif ob["type"] == "BEAR":
        for i in range(len(recent) - 3, 0, -1):
            c1, c3 = recent.iloc[i], recent.iloc[i + 2]
            gap_pct = (c1["low"] - c3["high"]) / c1["low"] * 100
            if c1["low"] > c3["high"] and gap_pct >= FVG_THRESHOLD:
                return {"type": "BEAR", "high": c1["low"],
                        "low": c3["high"], "gap_pct": gap_pct}

    return None


def detect_liquidity_trap(df: pd.DataFrame):
    """
    Equal Highs/Lows and Inducement detection.
    Equal levels (within 0.05%) → likely liquidity pool.
    Trap confirmed when price sweeps above/below and closes back inside.
    """
    if len(df) < 10:
        return None

    recent = df.iloc[-20:]
    tolerance = 0.0005   # 0.05%

    # Equal Highs
    highs = recent["high"].values
    for i in range(len(highs) - 2, 0, -1):
        for j in range(i - 1, max(i - 6, 0), -1):
            if abs(highs[i] - highs[j]) / highs[j] < tolerance:
                level = (highs[i] + highs[j]) / 2
                # Sweep: current price crossed above then closed below
                last = df.iloc[-1]
                if last["high"] > level and last["close"] < level:
                    return {"side": "SELL", "level": level,
                            "swept": True, "type": "EQL_HIGHS"}

    # Equal Lows
    lows = recent["low"].values
    for i in range(len(lows) - 2, 0, -1):
        for j in range(i - 1, max(i - 6, 0), -1):
            if abs(lows[i] - lows[j]) / lows[j] < tolerance:
                level = (lows[i] + lows[j]) / 2
                last = df.iloc[-1]
                if last["low"] < level and last["close"] > level:
                    return {"side": "BUY", "level": level,
                            "swept": True, "type": "EQL_LOWS"}

    return None


def analyze_h1_trend():
    """Determine H1 bias: BULLISH, BEARISH, or NEUTRAL."""
    if len(state.h1_candles) < 30:
        return "NEUTRAL"
    df = pd.DataFrame(list(state.h1_candles))
    df.columns = ["time", "open", "high", "low", "close"]
    result = detect_bos_choch(df)
    if result:
        state.trend_bias = result["direction"]
    else:
        # Fallback: simple HH/HL or LH/LL count
        closes = df["close"].values[-20:]
        if np.polyfit(np.arange(len(closes)), closes, 1)[0] > 0:
            state.trend_bias = "BULLISH"
        else:
            state.trend_bias = "BEARISH"
    return state.trend_bias


def compute_signal(timeframe="M15"):
    """Full SMC pipeline. Returns signal dict or None."""
    candles = state.m15_candles if timeframe == "M15" else state.m5_candles
    if len(candles) < 30:
        return None

    df = pd.DataFrame(list(candles))
    df.columns = ["time", "open", "high", "low", "close"]

    # 1. H1 trend bias
    bias = analyze_h1_trend()
    if bias == "NEUTRAL":
        return None

    # 2. BOS / CHoCH on execution TF
    struct = detect_bos_choch(df)
    if struct is None:
        return None

    # Must align with H1 bias
    if struct["direction"] != bias:
        return None

    # 3. Order Block
    ob = identify_order_block(df, bias)
    if ob is None:
        return None
    state.active_ob = ob

    # 4. FVG from OB
    fvg = identify_fvg(df, ob)
    if fvg is None:
        return None
    state.active_fvg = fvg

    # 5. Liquidity trap (sweep before entry)
    trap = detect_liquidity_trap(df)
    state.active_trap = trap
    # Require a confirmed trap sweep to avoid retail entry
    if trap is None:
        return None
    if trap["side"] != ("BUY" if bias == "BULLISH" else "SELL"):
        return None

    # 6. Entry, SL, TPs
    current = state.current_price
    if bias == "BULLISH":
        entry = ob["high"]          # enter at top of bull OB
        sl    = ob["low"] * 0.9995  # just below OB
    else:
        entry = ob["low"]           # enter at bottom of bear OB
        sl    = ob["high"] * 1.0005 # just above OB

    risk  = abs(entry - sl)
    tp1   = entry + (risk * TP1_R * (1 if bias == "BULLISH" else -1))
    tp2   = entry + (risk * TP2_R * (1 if bias == "BULLISH" else -1))
    tp3   = entry + (risk * TP3_R * (1 if bias == "BULLISH" else -1))

    # Size based on 1% account risk
    if state.account_balance > 0 and risk > 0:
        risk_amount = state.account_balance * RISK_PCT
        units = round(risk_amount / risk, 2)
    else:
        units = 1.0

    signal = {
        "direction": "BUY" if bias == "BULLISH" else "SELL",
        "entry":  round(entry, 3),
        "sl":     round(sl, 3),
        "tp1":    round(tp1, 3),
        "tp2":    round(tp2, 3),
        "tp3":    round(tp3, 3),
        "risk_r": round(risk, 4),
        "units":  units,
        "struct": struct["type"],
        "ob":     ob,
        "fvg":    fvg,
        "trap":   trap,
        "tf":     timeframe,
        "bias":   bias,
        "ts":     datetime.now(timezone.utc).isoformat(),
    }
    state.last_signal = signal
    return signal


# ─────────────────────────────────────────────
# DERIV WEBSOCKET ENGINE
# ─────────────────────────────────────────────
async def send_request(payload: dict) -> dict:
    """Send a request to Deriv WS and await the response."""
    if state.ws is None:
        raise RuntimeError("WebSocket not connected")
    req_id = state.req_id
    state.req_id += 1
    payload["req_id"] = req_id
    future = asyncio.get_event_loop().create_future()
    state.pending_requests[req_id] = future
    await state.ws.send(json.dumps(payload))
    return await asyncio.wait_for(future, timeout=15)


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


async def subscribe_candles(symbol: str, granularity: int, style="candles"):
    """Subscribe to OHLC candle stream."""
    await send_request({
        "ticks_history": symbol,
        "end": "latest",
        "count": 200,
        "granularity": granularity,
        "style": style,
        "subscribe": 1,
    })


async def open_contract(direction: str, amount: float, duration: int = None):
    """Open MULTUP or MULTDOWN contract on Deriv."""
    if state.paused:
        log.info("Bot paused — skipping contract open.")
        return None
    contract_type = "MULTUP" if direction == "BUY" else "MULTDOWN"
    payload = {
        "buy": 1,
        "price": round(amount, 2),
        "parameters": {
            "contract_type": contract_type,
            "symbol": SYMBOL,
            "amount": round(amount, 2),
            "currency": state.account_currency,
            "multiplier": 10,
            "basis": "stake",
            "stop_out": 1,
        },
    }
    try:
        resp = await send_request(payload)
        if "error" in resp:
            log.error(f"Buy error: {resp['error']['message']}")
            return None
        contract_id = resp["buy"]["contract_id"]
        state.open_contracts[contract_id] = {
            "direction": direction,
            "entry": state.current_price,
            "amount": amount,
            "signal": state.last_signal,
            "be_moved": False,
            "opened_at": time.time(),
        }
        state.trade_count += 1
        log.info(f"✅ Contract opened: {contract_id} [{direction}] ${amount:.2f}")
        return contract_id
    except Exception as e:
        log.error(f"open_contract exception: {e}")
        return None


async def close_contract(contract_id: str):
    """Sell/close an open contract."""
    try:
        resp = await send_request({"sell": contract_id, "price": 0})
        if "error" in resp:
            log.error(f"Close error: {resp['error']['message']}")
            return False
        state.open_contracts.pop(contract_id, None)
        log.info(f"🔴 Contract closed: {contract_id}")
        return True
    except Exception as e:
        log.error(f"close_contract exception: {e}")
        return False


async def close_all_contracts():
    ids = list(state.open_contracts.keys())
    for cid in ids:
        await close_contract(cid)
    return len(ids)


async def subscribe_contract_updates():
    """Subscribe to proposal_open_contract for live P&L and trailing stop."""
    for cid in list(state.open_contracts.keys()):
        await send_request({
            "proposal_open_contract": 1,
            "contract_id": cid,
            "subscribe": 1,
        })


# ─────────────────────────────────────────────
# TRADE MANAGEMENT (BE + Trailing Stop)
# ─────────────────────────────────────────────
def check_trade_management():
    """Check each open trade for TP1 BE trigger and trailing stop."""
    for cid, info in list(state.open_contracts.items()):
        sig = info.get("signal")
        if sig is None:
            continue
        price = state.current_price
        direction = info["direction"]
        entry  = sig["entry"]
        risk_r = sig["risk_r"]
        tp1    = sig["tp1"]
        sl     = sig["sl"]

        # Break-Even: move SL to entry once TP1 is reached
        if not info["be_moved"]:
            if direction == "BUY"  and price >= tp1:
                info["be_moved"] = True
                sig["sl"] = entry
                log.info(f"[{cid}] BreakEven triggered — SL moved to {entry}")
                asyncio.get_event_loop().call_soon_threadsafe(
                    lambda: asyncio.ensure_future(
                        tg_send_async(f"✅ *BreakEven* triggered for `{cid}`\nSL moved to entry: `{entry}`")
                    )
                )

            elif direction == "SELL" and price <= tp1:
                info["be_moved"] = True
                sig["sl"] = entry
                log.info(f"[{cid}] BreakEven triggered — SL moved to {entry}")
                asyncio.get_event_loop().call_soon_threadsafe(
                    lambda: asyncio.ensure_future(
                        tg_send_async(f"✅ *BreakEven* triggered for `{cid}`\nSL moved to entry: `{entry}`")
                    )
                )

        # Dynamic Trailing Stop using last swing
        if len(state.m15_candles) >= 10:
            df = pd.DataFrame(list(state.m15_candles)[-30:])
            df.columns = ["time", "open", "high", "low", "close"]
            swh, swl = detect_swing_points(df, n=3)
            if direction == "BUY" and swl:
                trailing_sl = df["low"].iloc[swl[-1]] * 0.9998
                if trailing_sl > sig["sl"]:
                    sig["sl"] = trailing_sl
                    log.info(f"[{cid}] Trailing SL → {trailing_sl:.3f}")

            elif direction == "SELL" and swh:
                trailing_sl = df["high"].iloc[swh[-1]] * 1.0002
                if trailing_sl < sig["sl"]:
                    sig["sl"] = trailing_sl
                    log.info(f"[{cid}] Trailing SL → {trailing_sl:.3f}")


# ─────────────────────────────────────────────
# WEBSOCKET MESSAGE HANDLER
# ─────────────────────────────────────────────
def parse_candle(msg: dict) -> Optional[tuple]:
    """Extract OHLCV tuple from WS candle message."""
    if "candles" in msg:
        return [
            (c["epoch"], c["open"], c["high"], c["low"], c["close"])
            for c in msg["candles"]
        ]
    if "ohlc" in msg:
        c = msg["ohlc"]
        return [(c["epoch"], float(c["open"]), float(c["high"]),
                 float(c["low"]), float(c["close"]))]
    return None


async def handle_message(msg: dict):
    """Dispatch incoming WebSocket messages."""
    req_id = msg.get("req_id")
    if req_id and req_id in state.pending_requests:
        future = state.pending_requests.pop(req_id)
        if not future.done():
            future.set_result(msg)
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
                state.current_price = float(msg["ohlc"]["close"])
            elif gran == 300:
                state.m5_candles.extend(candle)
                state.current_price = float(msg["ohlc"]["close"])

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
            profit = poc.get("profit", 0)
            status = poc.get("status", "")
            if status in ("sold", "expired"):
                if profit > 0:
                    state.wins += 1
                else:
                    state.losses += 1
                state.open_contracts.pop(cid, None)
                asyncio.ensure_future(tg_send_async(
                    f"{'✅ WIN' if profit > 0 else '❌ LOSS'}  Contract `{cid}`\n"
                    f"P&L: `{profit:.2f}` {state.account_currency}\n"
                    f"Record: {state.wins}W / {state.losses}L",
                ))

    elif mtype == "balance":
        state.account_balance  = msg["balance"]["balance"]
        state.account_currency = msg["balance"]["currency"]

    elif "error" in msg:
        log.warning(f"WS error msg: {msg['error']}")


# ─────────────────────────────────────────────
# TELEGRAM POLLING LOOP
# ─────────────────────────────────────────────
async def telegram_poll_loop():
    """Long-poll Telegram for commands and callback queries (non-blocking)."""
    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN not set — Telegram polling disabled.")
        return
    offset = 0
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    loop = asyncio.get_event_loop()

    while state.running:
        try:
            def do_poll():
                return requests.get(
                    f"{base}/getUpdates",
                    params={"offset": offset, "timeout": 20},
                    timeout=25,
                )
            r = await loop.run_in_executor(None, do_poll)
            data = r.json()
            if not data.get("ok"):
                log.error(f"Telegram getUpdates error: {data.get('description', data)}")
                await asyncio.sleep(10)
                continue
            updates = data.get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                await handle_telegram_update(update)
        except Exception as e:
            log.error(f"Telegram poll error: {type(e).__name__}: {e}")
            await asyncio.sleep(5)
        await asyncio.sleep(1)


async def handle_telegram_update(update: dict):
    """Process a Telegram update (message or callback query)."""
    # Text commands
    if "message" in update:
        text = update["message"].get("text", "").strip()
        chat_id = str(update["message"]["chat"]["id"])
        if chat_id != TELEGRAM_CHAT_ID and TELEGRAM_CHAT_ID:
            return
        await process_command(text)

    # Inline keyboard callbacks
    elif "callback_query" in update:
        cq = update["callback_query"]
        data = cq.get("data", "")
        cqid = cq["id"]
        chat_id = str(cq["message"]["chat"]["id"])
        if chat_id != TELEGRAM_CHAT_ID and TELEGRAM_CHAT_ID:
            tg_answer_callback(cqid, "Unauthorized")
            return
        tg_answer_callback(cqid)
        await process_command(data)


async def process_command(cmd: str):
    """Handle a bot command (text or callback)."""
    cmd = cmd.lower().strip()

    if cmd in ("/start", "/help"):
        await tg_send_async(
            "🤖 *SMC-EA XAU/USD Bot*\n\n"
            "Smart Money Concepts Expert Advisor\n"
            f"Symbol: `{SYMBOL}` | Risk: `{RISK_PCT*100:.0f}%`\n\n"
            "Use the buttons below to interact:",
        )

    elif cmd in ("/balance", "cmd_balance"):
        await get_account_info()
        await tg_send_async(
            f"💰 *Account Balance*\n"
            f"`{state.account_balance:.2f} {state.account_currency}`\n"
            f"Trades: {state.trade_count} | W/L: {state.wins}/{state.losses}",
        )

    elif cmd in ("/chart", "cmd_chart"):
        path = generate_chart(state.m15_candles, "M15")
        if path:
            bias_emoji = "📈" if state.trend_bias == "BULLISH" else "📉" if state.trend_bias == "BEARISH" else "➡️"
            caption = (
                f"{bias_emoji} *XAU/USD M15 Chart*\n"
                f"Bias: `{state.trend_bias}`  |  Price: `{state.current_price:.2f}`\n"
                f"OB: {'✅' if state.active_ob else '❌'}  "
                f"FVG: {'✅' if state.active_fvg else '❌'}  "
                f"Trap: {'✅' if state.active_trap else '❌'}"
            )
            await tg_send_async(caption, photo_path=path)
        else:
            await tg_send_async("⚠️ Not enough data to generate chart yet.")

    elif cmd in ("/status", "cmd_status"):
        open_c = len(state.open_contracts)
        mode = "🛑 PAUSED" if state.paused else "🟢 SCANNING"
        signal_info = ""
        if state.last_signal:
            s = state.last_signal
            signal_info = (
                f"\n\n*Last Signal*\n"
                f"Dir: `{s['direction']}` | Entry: `{s['entry']}`\n"
                f"SL: `{s['sl']}` | TP1: `{s['tp1']}`\n"
                f"Struct: `{s['struct']}` | TF: `{s['tf']}`"
            )
        await tg_send_async(
            f"⚡ *Bot Status*\n"
            f"Mode: {mode}\n"
            f"Price: `{state.current_price:.2f}`\n"
            f"Trend: `{state.trend_bias}`\n"
            f"Open Contracts: `{open_c}`\n"
            f"Balance: `{state.account_balance:.2f} {state.account_currency}`"
            + signal_info,
        )

    elif cmd in ("/close_all", "cmd_stop"):
        state.paused = True
        n = await close_all_contracts()
        await tg_send_async(
            f"🛑 *Emergency Stop Activated*\n"
            f"Closed `{n}` contract(s).\n"
            f"Bot is now *PAUSED*.\n"
            f"Send /resume to restart scanning.",
        )

    elif cmd == "/resume":
        state.paused = False
        await tg_send_async("✅ Bot *RESUMED* — scanning for setups.")


# ─────────────────────────────────────────────
# MAIN TRADING LOOP
# ─────────────────────────────────────────────
async def trading_loop():
    """Core scanning loop — runs every 30 seconds."""
    await asyncio.sleep(15)  # Let candles populate
    last_signal_ts = 0
    SIGNAL_COOLDOWN = 300    # seconds between signals

    while state.running:
        try:
            if state.paused or state.current_price == 0:
                await asyncio.sleep(30)
                continue

            check_trade_management()

            # Don't spam signals
            if time.time() - last_signal_ts < SIGNAL_COOLDOWN:
                await asyncio.sleep(30)
                continue

            # M15 primary signal
            signal = compute_signal("M15")

            # Confirm on M5 if M15 gives a signal
            if signal:
                m5_signal = compute_signal("M5")
                if m5_signal and m5_signal["direction"] == signal["direction"]:
                    log.info(
                        f"🎯 SIGNAL: {signal['direction']} | "
                        f"Entry: {signal['entry']} | SL: {signal['sl']} | "
                        f"TP1: {signal['tp1']} | Struct: {signal['struct']}"
                    )
                    # Generate chart
                    chart_path = generate_chart(state.m15_candles, "M15")

                    # Telegram notification
                    tg_msg = (
                        f"🎯 *NEW SIGNAL — XAU/USD*\n\n"
                        f"Direction: `{signal['direction']}`\n"
                        f"Entry:  `{signal['entry']}`\n"
                        f"SL:     `{signal['sl']}`\n"
                        f"TP1 (2R): `{signal['tp1']}`\n"
                        f"TP2 (4R): `{signal['tp2']}`\n"
                        f"TP3 (6R): `{signal['tp3']}`\n\n"
                        f"H1 Bias: `{signal['bias']}`\n"
                        f"Structure: `{signal['struct']}`\n"
                        f"OB: `{signal['ob']['type']}` @ `{signal['ob']['high']:.2f}–{signal['ob']['low']:.2f}`\n"
                        f"FVG Gap: `{signal['fvg']['gap_pct']:.3f}%`\n"
                        f"Trap: `{signal['trap']['type']}` swept ✅\n"
                        f"Stake: `{signal['units']:.2f} {state.account_currency}`"
                    )
                    if chart_path:
                        await tg_send_async(tg_msg, photo_path=chart_path)
                    else:
                        await tg_send_async(tg_msg)

                    # Execute trade
                    if state.account_balance > 0:
                        stake = max(1.0, round(state.account_balance * RISK_PCT, 2))
                        await open_contract(signal["direction"], stake)
                        last_signal_ts = time.time()

        except Exception as e:
            log.error(f"Trading loop error: {e}\n{traceback.format_exc()}")

        await asyncio.sleep(30)


# ─────────────────────────────────────────────
# WEBSOCKET CONNECTION MANAGER
# ─────────────────────────────────────────────
async def ws_connect_loop():
    """Maintain persistent WebSocket connection with auto-reconnect."""
    retry_delay = 5
    while state.running:
        try:
            log.info(f"Connecting to Deriv WebSocket: {DERIV_WS_URL}")
            async with websockets.connect(
                DERIV_WS_URL,
                ping_interval=25,
                ping_timeout=10,
                close_timeout=10,
            ) as ws:
                state.ws = ws
                retry_delay = 5  # reset on successful connect

                # Auth & subscriptions
                await authorize()
                await get_account_info()

                # Subscribe to candles: H1, M15, M5
                await subscribe_candles(SYMBOL, 3600)   # H1
                await subscribe_candles(SYMBOL, 900)    # M15
                await subscribe_candles(SYMBOL, 300)    # M5

                log.info(f"✅ Subscribed to {SYMBOL} | Balance: {state.account_balance} {state.account_currency}")
                await tg_send_async(
                    f"🤖 *SMC-EA Online*\n"
                    f"Symbol: `{SYMBOL}`\n"
                    f"Balance: `{state.account_balance:.2f} {state.account_currency}`\n"
                    f"Risk: `{RISK_PCT*100:.0f}%` per trade\n"
                    f"TPs: `{TP1_R}R / {TP2_R}R / {TP3_R}R`",
                )

                async for raw in ws:
                    msg = json.loads(raw)
                    await handle_message(msg)

        except websockets.ConnectionClosed as e:
            log.warning(f"WS closed: {type(e).__name__}: {e}. Reconnecting in {retry_delay}s…")
        except Exception as e:
            log.error(f"WS error: {type(e).__name__}: {e}. Reconnecting in {retry_delay}s…")
            log.error(traceback.format_exc())
        finally:
            state.ws = None

        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 60)  # exponential backoff


# ─────────────────────────────────────────────
# HEALTH SERVER (Railway)
# ─────────────────────────────────────────────
async def health_handler(request):
    data = {
        "status":   "running" if state.running else "stopped",
        "paused":   state.paused,
        "balance":  state.account_balance,
        "currency": state.account_currency,
        "price":    state.current_price,
        "trend":    state.trend_bias,
        "trades":   state.trade_count,
        "wins":     state.wins,
        "losses":   state.losses,
        "open":     len(state.open_contracts),
        "h1_bars":  len(state.h1_candles),
        "m15_bars": len(state.m15_candles),
    }
    return web.json_response(data)


async def start_health_server():
    app = web.Application()
    app.router.add_get("/",       health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"Health server on port {PORT}")


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
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down…")
        state.running = False
