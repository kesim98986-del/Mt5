#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║   XAU/USD GOLD TRADING BOT — MT5 + Gemini AI + Telegram                   ║
║   Architecture: SMC (Smart Money Concepts) + AI Confirmation               ║
║   Deployment:   Linux/Docker via Wine (MT5 Windows binary)                 ║
║   Risk Model:   1% per trade | SL/TP1(1:2)/TP2(1:4) | Break-Even mgmt    ║
╚══════════════════════════════════════════════════════════════════════════════╝

FLOW OVERVIEW
─────────────
  Every 5 minutes:
  1. Connect/verify MT5 session
  2. Fetch M15 + H1 candles for XAUUSD
  3. Run SMC Engine → detect BOS/CHOCH + Order Blocks
  4. If valid setup found → ask Gemini AI for confirmation
  5. Check spread guard (≤ 30 pts), session, risk filters
  6. Execute trade with dynamic lot, SL, TP1, TP2
  7. Monitor open positions → move SL to BE after TP1
  8. Send Telegram alerts at every significant event
"""

# ─────────────────────────────────────────────────────────────────────────────
# STANDARD LIBRARY
# ─────────────────────────────────────────────────────────────────────────────
import os
import sys
import time
import logging
import signal
import traceback
from datetime import datetime, timezone, timedelta
from typing    import Optional, Dict, List, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# THIRD-PARTY (see requirements.txt)
# ─────────────────────────────────────────────────────────────────────────────
import pandas            as pd
import numpy             as np
import requests                          # Telegram HTTP calls
import google.generativeai as genai      # Gemini AI free API

# MetaTrader5 runs under Wine on Linux (see Dockerfile)
import MetaTrader5 as mt5

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING — structured, timestamped, file + stdout
# ─────────────────────────────────────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-14s | %(message)s"
logging.basicConfig(
    level    = logging.INFO,
    format   = LOG_FORMAT,
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("XAUUSD_BOT")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — all secrets via environment variables
# ─────────────────────────────────────────────────────────────────────────────
class Config:
    """
    Load everything from environment variables so no secrets live in code.
    Set these in your Docker / Koyeb / Render environment:

      MT5_LOGIN          — Integer account number
      MT5_PASSWORD       — Account password (string)
      MT5_SERVER         — Broker server name  e.g. "ICMarkets-Demo"
      GEMINI_API_KEY     — Free key from aistudio.google.com
      TELEGRAM_BOT_TOKEN — From @BotFather on Telegram
      TELEGRAM_CHAT_ID   — Your chat/group ID (use @userinfobot to find it)
    """
    # MT5 credentials
    MT5_LOGIN    : int = int(os.environ.get("MT5_LOGIN",    "0"))
    MT5_PASSWORD : str = os.environ.get("MT5_PASSWORD",    "")
    MT5_SERVER   : str = os.environ.get("MT5_SERVER",      "")

    # AI
    GEMINI_API_KEY : str = os.environ.get("GEMINI_API_KEY", "")

    # Telegram
    TELEGRAM_BOT_TOKEN : str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID   : str = os.environ.get("TELEGRAM_CHAT_ID",   "")

    # Symbol
    SYMBOL : str = "XAUUSD"
    MAGIC  : int = 202501          # Unique identifier for this bot's orders

    # Risk
    RISK_PCT        : float = 0.01   # 1% of balance per trade
    MAX_SPREAD_PTS  : int   = 30     # Skip trade if spread > 30 points
    DAILY_DD_LIMIT  : float = 0.03   # Halt at 3% daily drawdown

    # SMC parameters
    OB_LOOKBACK     : int   = 30     # Candles to scan for Order Blocks
    SWING_LEFT      : int   = 3      # Left bars for swing detection
    SWING_RIGHT     : int   = 3      # Right bars for swing detection

    # Risk-Reward
    TP1_RR : float = 2.0
    TP2_RR : float = 4.0

    # Timing
    SCAN_INTERVAL_SEC : int = 300    # 5 minutes between scans

    # Trading sessions (UTC hours) — London + New York only
    SESSION_WINDOWS : List[Tuple[int,int]] = [(8, 17), (13, 21)]

    # Gemini model (free tier)
    GEMINI_MODEL : str = "gemini-1.5-flash"

cfg = Config()

# ═════════════════════════════════════════════════════════════════════════════
# 1. TELEGRAM NOTIFIER
# ═════════════════════════════════════════════════════════════════════════════
class Telegram:
    """
    Sends formatted messages to a Telegram bot.
    Uses the sendMessage Bot API endpoint — no libraries needed, just requests.
    All failures are caught silently so a Telegram outage never crashes the bot.
    """

    BASE = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: str, chat_id: str):
        self.token   = token
        self.chat_id = chat_id
        self._url    = self.BASE.format(token=token)
        self._ok     = bool(token and chat_id)

    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a plain or HTML-formatted message."""
        if not self._ok:
            log.debug("Telegram not configured — message skipped")
            return False
        try:
            resp = requests.post(
                self._url,
                json    = {"chat_id": self.chat_id, "text": text, "parse_mode": parse_mode},
                timeout = 10,
            )
            return resp.status_code == 200
        except Exception as e:
            log.warning(f"Telegram send failed: {e}")
            return False

    # ── Formatted message helpers ──────────────────────────────────────────
    def alert_setup(self, direction: str, reason: str, price: float, rsi: float):
        emoji = "🟢" if direction == "buy" else "🔴"
        self.send(
            f"{emoji} <b>SETUP DETECTED — XAUUSD</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Direction : <b>{direction.upper()}</b>\n"
            f"Price     : <b>{price:.2f}</b>\n"
            f"RSI       : {rsi:.1f}\n"
            f"Reason    : {reason}\n"
            f"⏳ Awaiting Gemini AI confirmation…"
        )

    def alert_trade_open(self, direction: str, lot: float, entry: float,
                         sl: float, tp1: float, tp2: float, ticket: int):
        emoji = "🚀" if direction == "buy" else "💥"
        self.send(
            f"{emoji} <b>TRADE OPENED — XAUUSD</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Ticket    : #{ticket}\n"
            f"Direction : <b>{direction.upper()}</b>\n"
            f"Lot Size  : {lot}\n"
            f"Entry     : {entry:.2f}\n"
            f"Stop Loss : {sl:.2f}\n"
            f"TP1 (1:2) : {tp1:.2f}\n"
            f"TP2 (1:4) : {tp2:.2f}"
        )

    def alert_tp1_be(self, ticket: int, be_price: float):
        self.send(
            f"🎯 <b>TP1 HIT — SL → Break-Even</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Ticket   : #{ticket}\n"
            f"BE Price : {be_price:.2f}\n"
            f"50% position closed. Trailing TP2…"
        )

    def alert_closed(self, ticket: int, result: str, pnl: float):
        emoji = "✅" if pnl >= 0 else "❌"
        self.send(
            f"{emoji} <b>POSITION CLOSED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Ticket : #{ticket}\n"
            f"Result : {result}\n"
            f"P&L    : {'+'if pnl>=0 else ''}{pnl:.2f} USD"
        )

    def alert_gemini_skip(self, direction: str, gemini_response: str):
        self.send(
            f"🤖 <b>GEMINI SKIP — {direction.upper()}</b>\n"
            f"AI returned: <i>{gemini_response}</i>\n"
            f"Trade signal filtered out."
        )

    def alert_error(self, error: str):
        self.send(f"⚠️ <b>BOT ERROR</b>\n<code>{error[:500]}</code>")

    def alert_daily_limit(self, dd_pct: float):
        self.send(
            f"🛑 <b>DAILY DRAWDOWN LIMIT HIT</b>\n"
            f"Drawdown : {dd_pct:.2f}%\n"
            f"Bot will halt trading for today."
        )

    def alert_startup(self, balance: float):
        self.send(
            f"🤖 <b>XAU/USD BOT STARTED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Balance  : ${balance:,.2f}\n"
            f"Risk     : 1% = ${balance*0.01:,.2f}/trade\n"
            f"Scanning : Every 5 minutes\n"
            f"Sessions : London + New York\n"
            f"AI       : Gemini {cfg.GEMINI_MODEL}"
        )


# ═════════════════════════════════════════════════════════════════════════════
# 2. MT5 CONNECTION MANAGER
# ═════════════════════════════════════════════════════════════════════════════
class MT5Manager:
    """
    Wraps MetaTrader5 Python API.
    On Linux: MT5 runs inside Wine (see Dockerfile).
    The mt5.initialize() call launches the Wine-hosted MT5 terminal.
    """

    def __init__(self):
        self._connected = False

    def connect(self) -> bool:
        """Initialize MT5 and log in with credentials from environment."""
        if self._connected:
            # Verify connection is still alive
            if mt5.account_info() is not None:
                return True
            self._connected = False

        log.info("Connecting to MT5…")
        if not mt5.initialize():
            log.error(f"mt5.initialize() failed: {mt5.last_error()}")
            return False

        if not mt5.login(cfg.MT5_LOGIN, cfg.MT5_PASSWORD, cfg.MT5_SERVER):
            log.error(f"mt5.login() failed: {mt5.last_error()}")
            mt5.shutdown()
            return False

        acc = mt5.account_info()
        log.info(f"✅ MT5 connected | Account:{acc.login} | Balance:${acc.balance:,.2f} | Server:{acc.server}")
        self._connected = True
        return True

    def disconnect(self):
        mt5.shutdown()
        self._connected = False
        log.info("MT5 disconnected")

    def get_account(self) -> Optional[mt5.AccountInfo]:
        return mt5.account_info()

    def get_balance(self) -> float:
        acc = self.get_account()
        return acc.balance if acc else 0.0

    def get_equity(self) -> float:
        acc = self.get_account()
        return acc.equity if acc else 0.0

    def get_candles(self, symbol: str, timeframe: int, count: int) -> Optional[pd.DataFrame]:
        """Fetch OHLCV data, return as clean DataFrame with UTC datetime index."""
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
        if rates is None or len(rates) == 0:
            log.warning(f"No rates for {symbol} tf={timeframe}: {mt5.last_error()}")
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("time", inplace=True)
        return df

    def get_tick(self, symbol: str) -> Optional[mt5.Tick]:
        return mt5.symbol_info_tick(symbol)

    def get_spread_points(self, symbol: str) -> int:
        """Return current spread in points (broker convention)."""
        tick = self.get_tick(symbol)
        info = mt5.symbol_info(symbol)
        if tick is None or info is None:
            return 9999
        spread_price  = tick.ask - tick.bid
        point         = info.point
        return int(round(spread_price / point)) if point > 0 else 9999

    def get_positions(self) -> List[mt5.TradePosition]:
        """All open positions belonging to this bot (matched by MAGIC)."""
        positions = mt5.positions_get(symbol=cfg.SYMBOL)
        if positions is None:
            return []
        return [p for p in positions if p.magic == cfg.MAGIC]

    def get_history_deals(self, from_dt: datetime) -> List[mt5.TradeDeal]:
        """Fetch closed deals since from_dt (for daily P&L tracking)."""
        deals = mt5.history_deals_get(from_dt, datetime.now(timezone.utc))
        if deals is None:
            return []
        return [d for d in deals if d.magic == cfg.MAGIC]


# ═════════════════════════════════════════════════════════════════════════════
# 3. SMC ENGINE — Market Structure + Order Blocks
# ═════════════════════════════════════════════════════════════════════════════
class SMCEngine:
    """
    Implements core Smart Money Concepts analysis:
      • Swing highs/lows detection
      • BOS (Break of Structure) — continuation
      • CHOCH (Change of Character) — reversal
      • Order Block identification (last opposite candle before impulse)
      • RSI momentum filter
    """

    # ─── RSI ──────────────────────────────────────────────────────────────────
    @staticmethod
    def rsi(close: pd.Series, period: int = 14) -> pd.Series:
        delta    = close.diff()
        gain     = delta.clip(lower=0)
        loss     = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        rs       = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    # ─── Swing Detection ──────────────────────────────────────────────────────
    def swing_points(self, df: pd.DataFrame,
                     left: int = 3, right: int = 3
                     ) -> Tuple[pd.Series, pd.Series]:
        """
        Returns (swing_highs, swing_lows) as boolean Series.
        A pivot high: highest high in [i-left … i+right] window.
        A pivot low : lowest  low  in [i-left … i+right] window.
        """
        sh = pd.Series(False, index=df.index)
        sl = pd.Series(False, index=df.index)
        n  = len(df)
        for i in range(left, n - right):
            win_h = df["high"].iloc[i - left : i + right + 1]
            win_l = df["low"].iloc[i - left : i + right + 1]
            if df["high"].iloc[i] == win_h.max():
                sh.iloc[i] = True
            if df["low"].iloc[i] == win_l.min():
                sl.iloc[i] = True
        return sh, sl

    # ─── Market Structure ─────────────────────────────────────────────────────
    def market_structure(self, df: pd.DataFrame) -> Dict:
        """
        Identifies current trend and BOS/CHOCH events.
        
        Returns:
          structure : "bullish" | "bearish" | "ranging"
          bos       : bool — break of structure (same direction)
          choch     : bool — change of character (opposite direction)
          last_sh   : float — most recent swing high price
          last_sl   : float — most recent swing low price
          prev_sh   : float
          prev_sl   : float
        """
        sh_mask, sl_mask = self.swing_points(df, cfg.SWING_LEFT, cfg.SWING_RIGHT)

        sh_prices = df["high"][sh_mask].values
        sl_prices = df["low"][sl_mask].values
        close     = df["close"].iloc[-1]
        prev_c    = df["close"].iloc[-2]

        result = dict(structure="ranging", bos=False, choch=False,
                      last_sh=0.0, last_sl=0.0, prev_sh=0.0, prev_sl=0.0)

        if len(sh_prices) < 2 or len(sl_prices) < 2:
            return result

        result.update({
            "last_sh" : sh_prices[-1],
            "prev_sh" : sh_prices[-2],
            "last_sl" : sl_prices[-1],
            "prev_sl" : sl_prices[-2],
        })

        lsh, psh = sh_prices[-1], sh_prices[-2]
        lsl, psl = sl_prices[-1], sl_prices[-2]

        # Determine trend from swing structure
        if lsh > psh and lsl > psl:
            result["structure"] = "bullish"
        elif lsh < psh and lsl < psl:
            result["structure"] = "bearish"

        # BOS: close breaks last swing in trend direction → continuation
        # CHOCH: close breaks last swing against trend → reversal signal
        if close > lsh > prev_c:
            result["bos"]   = (lsh > psh)    # bullish BOS if higher highs
            result["choch"] = (lsh <= psh)   # CHOCH if broke below prior HH

        if close < lsl < prev_c:
            result["bos"]   = (lsl < psl)    # bearish BOS if lower lows
            result["choch"] = (lsl >= psl)   # CHOCH if broke above prior LL

        return result

    # ─── Order Block Detection ────────────────────────────────────────────────
    def find_order_blocks(self, df: pd.DataFrame) -> List[Dict]:
        """
        Bullish OB : Last BEARISH candle immediately before a strong bullish impulse.
        Bearish OB : Last BULLISH candle immediately before a strong bearish impulse.
        Only returns un-mitigated OBs (price has not traded back through them).

        Each OB dict:
          type     : "bullish" | "bearish"
          top      : float  — upper boundary of OB zone
          bottom   : float  — lower boundary of OB zone
          index    : int    — candle index in df
        """
        obs   = []
        n     = len(df)
        chunk = df.tail(cfg.OB_LOOKBACK).reset_index(drop=True)

        for i in range(1, len(chunk) - 1):
            c = chunk.iloc[i]
            n_c = chunk.iloc[i + 1]

            # ── Bullish OB ───────────────────────────────────────────────────
            is_bearish_candle  = c["close"] < c["open"]
            is_bullish_impulse = (n_c["close"] > n_c["open"] and
                                  n_c["close"] > c["high"])
            if is_bearish_candle and is_bullish_impulse:
                ob = {"type"  : "bullish",
                      "top"   : c["open"],   # body top of bearish candle
                      "bottom": c["close"],  # body bottom
                      "index" : i}
                # Mitigation check: any future candle's low dips into OB bottom
                future_lows = chunk["low"].iloc[i+1:]
                if future_lows.min() > ob["bottom"]:  # price never revisited
                    obs.append(ob)

            # ── Bearish OB ───────────────────────────────────────────────────
            is_bullish_candle  = c["close"] > c["open"]
            is_bearish_impulse = (n_c["close"] < n_c["open"] and
                                  n_c["close"] < c["low"])
            if is_bullish_candle and is_bearish_impulse:
                ob = {"type"  : "bearish",
                      "top"   : c["close"],  # body top of bullish candle
                      "bottom": c["open"],   # body bottom
                      "index" : i}
                future_highs = chunk["high"].iloc[i+1:]
                if future_highs.max() < ob["top"]:
                    obs.append(ob)

        return obs

    # ─── Full Signal Generation ───────────────────────────────────────────────
    def analyse(self, df_m15: pd.DataFrame, df_h1: pd.DataFrame
                ) -> Optional[Dict]:
        """
        Combines H1 structure + M15 OBs + RSI filter into a trade signal.
        Returns signal dict or None.

        Signal fields:
          direction : "buy" | "sell"
          entry     : float  — current mid price
          sl        : float  — stop-loss price
          reason    : str    — human-readable explanation
          rsi       : float  — current RSI value
          ob        : dict   — triggering order block
          h1_struct : dict   — H1 market structure snapshot
        """
        # H1 structure (big-picture bias)
        h1 = self.market_structure(df_h1)
        if h1["structure"] == "ranging":
            log.debug("H1 ranging — no trade")
            return None

        # M15 OBs (precision entry)
        obs = self.find_order_blocks(df_m15)
        if not obs:
            log.debug("No valid OBs on M15")
            return None

        # RSI on M15
        rsi_series  = self.rsi(df_m15["close"])
        current_rsi = rsi_series.iloc[-1]
        current_px  = df_m15["close"].iloc[-1]

        # ── BUY: bullish H1 + un-mitigated bullish OB near price ──────────
        if h1["structure"] == "bullish":
            bull_obs = [o for o in obs if o["type"] == "bullish"
                        and o["bottom"] < current_px <= o["top"] * 1.003]
            if bull_obs and current_rsi < 70:          # Not overbought
                ob = max(bull_obs, key=lambda x: x["top"])
                return {
                    "direction": "buy",
                    "entry"    : current_px,
                    "sl"       : ob["bottom"] - 0.50,  # just below OB
                    "reason"   : (f"H1 Bullish | {'BOS' if h1['bos'] else 'CHOCH'} | "
                                  f"Bullish OB @ {ob['bottom']:.2f}-{ob['top']:.2f}"),
                    "rsi"      : current_rsi,
                    "ob"       : ob,
                    "h1_struct": h1,
                }

        # ── SELL: bearish H1 + un-mitigated bearish OB near price ─────────
        if h1["structure"] == "bearish":
            bear_obs = [o for o in obs if o["type"] == "bearish"
                        and o["bottom"] * 0.997 <= current_px < o["top"]]
            if bear_obs and current_rsi > 30:          # Not oversold
                ob = min(bear_obs, key=lambda x: x["bottom"])
                return {
                    "direction": "sell",
                    "entry"    : current_px,
                    "sl"       : ob["top"] + 0.50,
                    "reason"   : (f"H1 Bearish | {'BOS' if h1['bos'] else 'CHOCH'} | "
                                  f"Bearish OB @ {ob['bottom']:.2f}-{ob['top']:.2f}"),
                    "rsi"      : current_rsi,
                    "ob"       : ob,
                    "h1_struct": h1,
                }

        return None


# ═════════════════════════════════════════════════════════════════════════════
# 4. GEMINI AI CONFIRMATION FILTER
# ═════════════════════════════════════════════════════════════════════════════
class GeminiFilter:
    """
    Calls Gemini 1.5-Flash (free tier) to validate a trade signal.
    Sends: market structure summary + proposed direction.
    Expects: exactly one of Bullish / Bearish / Neutral / High Risk.
    Trades are executed only if response matches proposed direction.
    """

    def __init__(self, api_key: str):
        if api_key:
            genai.configure(api_key=api_key)
            self._model = genai.GenerativeModel(cfg.GEMINI_MODEL)
            self._ok    = True
        else:
            log.warning("No GEMINI_API_KEY — AI filter disabled, trading on SMC alone")
            self._ok = False

    def confirm(self, signal: Dict, df_h4_summary: str) -> str:
        """
        Returns: "Bullish" | "Bearish" | "Neutral" | "High Risk"
        If API unavailable, returns proposed direction (pass-through).
        """
        if not self._ok:
            # Without API key, auto-confirm (SMC-only mode)
            return "Bullish" if signal["direction"] == "buy" else "Bearish"

        prompt = f"""You are a professional XAU/USD (Gold) market analyst.
Analyze this trade setup and return EXACTLY ONE WORD from the list below.

PROPOSED TRADE: {signal['direction'].upper()}

MARKET CONTEXT:
- H1 Trend       : {signal['h1_struct']['structure'].upper()}
- Structure Event : {'BOS' if signal['h1_struct']['bos'] else 'CHOCH' if signal['h1_struct']['choch'] else 'None'}
- Last Swing High : {signal['h1_struct']['last_sh']:.2f}
- Last Swing Low  : {signal['h1_struct']['last_sl']:.2f}
- Current Price   : {signal['entry']:.2f}
- RSI (14)        : {signal['rsi']:.1f}
- Order Block     : {signal['ob']['type'].upper()} zone {signal['ob']['bottom']:.2f}–{signal['ob']['top']:.2f}
- Stop Loss       : {signal['sl']:.2f}

H4 SUMMARY:
{df_h4_summary}

INSTRUCTIONS:
- Respond with EXACTLY one word, no punctuation, no explanation.
- Choose from: Bullish, Bearish, Neutral, High Risk
- Bullish   = conditions support a BUY trade
- Bearish   = conditions support a SELL trade
- Neutral   = mixed or insufficient signals
- High Risk = news event risk or unclear market

Your one-word response:"""

        try:
            response = self._model.generate_content(
                prompt,
                generation_config=genai.GenerationConfig(
                    temperature      = 0.05,
                    max_output_tokens= 8,
                )
            )
            raw = response.text.strip()
            for label in ["Bullish", "Bearish", "Neutral", "High Risk"]:
                if label.lower() in raw.lower():
                    log.info(f"🤖 Gemini: {label}")
                    return label
            log.warning(f"Gemini unexpected: '{raw}' → treating as Neutral")
            return "Neutral"
        except Exception as e:
            log.error(f"Gemini API error: {e}")
            return "Neutral"

    @staticmethod
    def h4_summary(df_h4: Optional[pd.DataFrame]) -> str:
        """Build a plain-text H4 summary to send with the Gemini prompt."""
        if df_h4 is None or len(df_h4) < 20:
            return "H4 data unavailable"
        close  = df_h4["close"]
        rsi_v  = SMCEngine.rsi(close).iloc[-1]
        sma20  = close.rolling(20).mean().iloc[-1]
        curr   = close.iloc[-1]
        chg    = (curr - close.iloc[-20]) / close.iloc[-20] * 100
        trend  = "BULLISH" if curr > sma20 else "BEARISH"
        return (
            f"H4 Price:{curr:.2f} | SMA20:{sma20:.2f} | RSI:{rsi_v:.1f} | "
            f"20-bar change:{chg:+.2f}% | Trend:{trend}"
        )


# ═════════════════════════════════════════════════════════════════════════════
# 5. RISK & FILTER MANAGER
# ═════════════════════════════════════════════════════════════════════════════
class RiskManager:
    """
    Dynamic lot size calculation + all pre-trade guards:
      • Spread guard (≤ 30 pts)
      • Session filter (London / NY only)
      • Daily drawdown cap (3%)
    """

    def __init__(self, mt5_mgr: MT5Manager):
        self._mt5          = mt5_mgr
        self._daily_start  = 0.0
        self._daily_halted = False
        self._reset_date   = datetime.now(timezone.utc).date()

    def _refresh_daily(self):
        today = datetime.now(timezone.utc).date()
        if today != self._reset_date:
            self._daily_start  = self._mt5.get_balance()
            self._daily_halted = False
            self._reset_date   = today
            log.info(f"📅 Daily reset — start balance: ${self._daily_start:,.2f}")

    def lot_size(self, symbol: str, sl_price: float, entry_price: float) -> float:
        """
        Lot = (Balance × Risk%) / (SL_distance × pip_value_per_lot)
        For XAUUSD: 1 standard lot = 100 troy oz
        Pip value ≈ $1 per 0.01 move per lot (most brokers)
        """
        pip_size          = 0.01   # 1 pip for gold
        pip_value_per_lot = 1.00   # USD per pip per standard lot

        balance    = self._mt5.get_balance()
        risk_usd   = balance * cfg.RISK_PCT
        sl_pips    = abs(entry_price - sl_price) / pip_size

        if sl_pips <= 0:
            return 0.01

        raw_lot = risk_usd / (sl_pips * pip_value_per_lot)
        # Round to 2 decimal places, clamp 0.01–10.0
        lot = max(0.01, min(10.0, round(raw_lot, 2)))
        log.info(f"💰 Balance:${balance:,.2f} | Risk:${risk_usd:.2f} | "
                 f"SL:{sl_pips:.1f}pips | Lot:{lot}")
        return lot

    def spread_ok(self, symbol: str, mt5_mgr: MT5Manager) -> bool:
        spread = mt5_mgr.get_spread_points(symbol)
        if spread > cfg.MAX_SPREAD_PTS:
            log.warning(f"⚠ Spread {spread} pts > limit {cfg.MAX_SPREAD_PTS} pts — skipping")
            return False
        return True

    def in_session(self) -> bool:
        """True if current UTC hour is within a defined trading window."""
        hour = datetime.now(timezone.utc).hour
        for (start, end) in cfg.SESSION_WINDOWS:
            if start <= hour < end:
                return True
        return False

    def daily_ok(self) -> bool:
        """Check 3% daily drawdown cap. Returns False if halted."""
        self._refresh_daily()
        if self._daily_halted:
            return False
        if self._daily_start <= 0:
            self._daily_start = self._mt5.get_balance()
            return True
        equity = self._mt5.get_equity()
        dd     = (self._daily_start - equity) / self._daily_start
        if dd >= cfg.DAILY_DD_LIMIT:
            self._daily_halted = True
            log.warning(f"🛑 Daily drawdown {dd*100:.1f}% ≥ {cfg.DAILY_DD_LIMIT*100:.0f}% — halted")
            return False
        return True

    def all_clear(self, mt5_mgr: MT5Manager) -> Tuple[bool, str]:
        """Master pre-trade check. Returns (ok, reason)."""
        if not self.daily_ok():
            return False, "Daily drawdown limit reached"
        if not self.in_session():
            return False, "Outside London/NY session"
        if not self.spread_ok(cfg.SYMBOL, mt5_mgr):
            return False, "Spread too wide"
        return True, "OK"


# ═════════════════════════════════════════════════════════════════════════════
# 6. TRADE EXECUTOR
# ═════════════════════════════════════════════════════════════════════════════
class TradeExecutor:
    """
    Places and manages orders via mt5.order_send().
    Opens TWO half-size positions:
      Position 1 → TP at 1:2 RR (closed at TP1, SL → BE)
      Position 2 → TP at 1:4 RR (trailing, structural)
    """

    def __init__(self, mt5_mgr: MT5Manager, risk_mgr: RiskManager, tg: Telegram):
        self._mt5  = mt5_mgr
        self._risk = risk_mgr
        self._tg   = tg

    def open(self, signal: Dict) -> List[int]:
        """Open trade. Returns list of ticket numbers."""
        symbol    = cfg.SYMBOL
        direction = signal["direction"]
        sl        = signal["sl"]

        tick = self._mt5.get_tick(symbol)
        if tick is None:
            log.error("Cannot get tick for entry")
            return []

        entry = tick.ask if direction == "buy" else tick.bid
        lot   = self._risk.lot_size(symbol, sl, entry)
        if lot <= 0:
            return []

        # Compute TP levels from actual entry
        risk_dist = abs(entry - sl)
        tp1 = entry + risk_dist * cfg.TP1_RR if direction == "buy" \
              else entry - risk_dist * cfg.TP1_RR
        tp2 = entry + risk_dist * cfg.TP2_RR if direction == "buy" \
              else entry - risk_dist * cfg.TP2_RR

        half_lot   = max(0.01, round(lot / 2, 2))
        order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
        tickets    = []

        for tp, label in [(tp1, "TP1"), (tp2, "TP2")]:
            req = {
                "action"      : mt5.TRADE_ACTION_DEAL,
                "symbol"      : symbol,
                "volume"      : half_lot,
                "type"        : order_type,
                "price"       : entry,
                "sl"          : round(sl, 2),
                "tp"          : round(tp, 2),
                "magic"       : cfg.MAGIC,
                "comment"     : f"SMC_{label}",
                "type_filling": mt5.ORDER_FILLING_IOC,
                "deviation"   : 20,
            }
            result = mt5.order_send(req)
            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info(f"✅ {direction.upper()} {label} | Ticket:{result.order} | "
                         f"Lot:{half_lot} | Entry:{entry:.2f} | SL:{sl:.2f} | TP:{tp:.2f}")
                tickets.append(result.order)
            else:
                code = result.retcode if result else "N/A"
                log.error(f"Order failed [{label}]: retcode={code} | {mt5.last_error()}")

        if tickets:
            self._tg.alert_trade_open(direction, half_lot * 2, entry, sl, tp1, tp2, tickets[0])

        return tickets

    def move_sl_to_be(self, ticket: int, entry: float, direction: str):
        """After TP1 hit: move stop loss to break-even on the remaining position."""
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return
        # Small buffer to ensure the stop is valid
        be = round(entry + 0.10, 2) if direction == "buy" else round(entry - 0.10, 2)
        req = {
            "action" : mt5.TRADE_ACTION_SLTP,
            "ticket" : ticket,
            "sl"     : be,
        }
        result = mt5.order_send(req)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(f"🔄 SL → BE | Ticket:{ticket} | BE:{be:.2f}")
            self._tg.alert_tp1_be(ticket, be)
        else:
            log.warning(f"BE modify failed for ticket {ticket}: {mt5.last_error()}")


# ═════════════════════════════════════════════════════════════════════════════
# 7. POSITION MONITOR — TP1 / Break-Even Management
# ═════════════════════════════════════════════════════════════════════════════
class PositionMonitor:
    """
    Tracks open positions each cycle.
    - Detects when TP1 price level is reached
    - Moves SL to BE on the TP2 position
    - Sends Telegram alerts when positions close
    """

    def __init__(self, mt5_mgr: MT5Manager, executor: TradeExecutor, tg: Telegram):
        self._mt5       = mt5_mgr
        self._executor  = executor
        self._tg        = tg
        self._tp1_done  : set = set()   # tickets where BE has already been set
        self._known_pos : Dict[int, mt5.TradePosition] = {}

    def run(self):
        """Called every scan cycle to manage open positions."""
        current_positions = {p.ticket: p for p in self._mt5.get_positions()}

        # Detect closed positions (were known, now gone)
        for ticket, old_pos in list(self._known_pos.items()):
            if ticket not in current_positions:
                # Position closed — fetch deal history for P&L
                self._handle_close(ticket, old_pos)
                del self._known_pos[ticket]

        # Update known positions
        self._known_pos = current_positions

        # Manage open positions
        tick = self._mt5.get_tick(cfg.SYMBOL)
        if tick is None:
            return

        for ticket, pos in current_positions.items():
            if ticket in self._tp1_done:
                continue  # BE already set for this ticket

            entry     = pos.price_open
            direction = "buy" if pos.type == mt5.ORDER_TYPE_BUY else "sell"
            risk_dist = abs(entry - pos.sl)
            tp1_level = (entry + risk_dist * cfg.TP1_RR if direction == "buy"
                         else entry - risk_dist * cfg.TP1_RR)
            current   = tick.bid if direction == "buy" else tick.ask

            tp1_hit = (direction == "buy"  and current >= tp1_level) or \
                      (direction == "sell" and current <= tp1_level)

            if tp1_hit:
                log.info(f"🎯 TP1 hit on ticket {ticket} @ {current:.2f}")
                self._executor.move_sl_to_be(ticket, entry, direction)
                self._tp1_done.add(ticket)

    def _handle_close(self, ticket: int, pos: mt5.TradePosition):
        """Fetch deal P&L and send Telegram alert."""
        since = datetime.now(timezone.utc) - timedelta(hours=48)
        deals = self._mt5.get_history_deals(since)
        pnl   = sum(d.profit for d in deals if d.position_id == ticket)
        result_str = "TP HIT" if pnl > 0 else "SL HIT" if pnl < 0 else "CLOSED"
        log.info(f"📊 Position {ticket} closed | {result_str} | P&L: {pnl:+.2f}")
        self._tg.alert_closed(ticket, result_str, pnl)


# ═════════════════════════════════════════════════════════════════════════════
# 8. MAIN BOT ORCHESTRATOR
# ═════════════════════════════════════════════════════════════════════════════
class XAUBot:
    """
    Master controller — infinite 5-minute loop.
    Sequence per cycle:
      1. Ensure MT5 is connected
      2. Monitor existing positions (TP1/BE management)
      3. Check risk filters (session, spread, daily DD)
      4. Skip if position already open (no stacking)
      5. Run SMC analysis on M15 + H1
      6. Confirm with Gemini AI
      7. Execute trade
    """

    def __init__(self):
        self._mt5     = MT5Manager()
        self._smc     = SMCEngine()
        self._risk    = RiskManager(self._mt5)
        self._tg      = Telegram(cfg.TELEGRAM_BOT_TOKEN, cfg.TELEGRAM_CHAT_ID)
        self._gemini  = GeminiFilter(cfg.GEMINI_API_KEY)
        self._exec    = TradeExecutor(self._mt5, self._risk, self._tg)
        self._monitor = PositionMonitor(self._mt5, self._exec, self._tg)
        self._running = True

        # Graceful shutdown on SIGTERM (Docker stop) / SIGINT (Ctrl-C)
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT,  self._shutdown)

    def _shutdown(self, *_):
        log.info("Shutdown signal received")
        self._running = False

    def _cycle(self):
        """One complete scan-and-trade cycle."""

        # ── 1. MT5 connection ────────────────────────────────────────────────
        if not self._mt5.connect():
            log.error("MT5 connection failed — retrying next cycle")
            return

        # ── 2. Manage open positions ─────────────────────────────────────────
        self._monitor.run()

        # ── 3. Pre-trade filters ─────────────────────────────────────────────
        ok, reason = self._risk.all_clear(self._mt5)
        if not ok:
            log.info(f"⏸  Filters: {reason}")
            return

        # ── 4. Skip if position open (no stacking) ───────────────────────────
        if self._mt5.get_positions():
            log.info("📊 Position open — skipping new signal scan")
            return

        # ── 5. Fetch data ────────────────────────────────────────────────────
        df_m15 = self._mt5.get_candles(cfg.SYMBOL, mt5.TIMEFRAME_M15, 200)
        df_h1  = self._mt5.get_candles(cfg.SYMBOL, mt5.TIMEFRAME_H1,  100)
        df_h4  = self._mt5.get_candles(cfg.SYMBOL, mt5.TIMEFRAME_H4,  100)

        if df_m15 is None or df_h1 is None:
            log.warning("Missing price data — skipping cycle")
            return

        # ── 6. SMC Analysis ──────────────────────────────────────────────────
        signal = self._smc.analyse(df_m15, df_h1)
        if signal is None:
            log.info("🔍 No SMC setup found this cycle")
            return

        log.info(f"📡 Setup: {signal['direction'].upper()} | {signal['reason']}")
        self._tg.alert_setup(
            signal["direction"], signal["reason"],
            signal["entry"],    signal["rsi"]
        )

        # ── 7. Gemini AI Confirmation ────────────────────────────────────────
        h4_summary = GeminiFilter.h4_summary(df_h4)
        sentiment  = self._gemini.confirm(signal, h4_summary)

        # Match: Bullish → buy, Bearish → sell
        expected = "Bullish" if signal["direction"] == "buy" else "Bearish"
        if sentiment != expected:
            log.info(f"🤖 Gemini '{sentiment}' ≠ '{expected}' — trade skipped")
            self._tg.alert_gemini_skip(signal["direction"], sentiment)
            return

        log.info(f"🤖 Gemini confirmed: {sentiment} — executing trade")

        # ── 8. Execute ───────────────────────────────────────────────────────
        tickets = self._exec.open(signal)
        if not tickets:
            self._tg.alert_error("Trade execution failed — check MT5 logs")

    def run(self):
        """Infinite loop — runs forever until shutdown signal."""
        log.info("=" * 70)
        log.info("  XAU/USD SMC BOT — STARTING")
        log.info("=" * 70)

        # Initial connection
        for attempt in range(5):
            if self._mt5.connect():
                break
            log.warning(f"MT5 connect attempt {attempt+1}/5 failed — waiting 30s")
            time.sleep(30)
        else:
            log.critical("Cannot connect to MT5 — exiting")
            sys.exit(1)

        balance = self._mt5.get_balance()
        log.info(f"Balance: ${balance:,.2f} | Risk per trade: ${balance*cfg.RISK_PCT:,.2f}")
        self._tg.alert_startup(balance)

        while self._running:
            start = time.monotonic()
            try:
                self._cycle()
            except Exception as e:
                tb = traceback.format_exc()
                log.error(f"Cycle error:\n{tb}")
                self._tg.alert_error(str(e))

            # Sleep for remainder of 5-minute interval
            elapsed = time.monotonic() - start
            sleep_t = max(0, cfg.SCAN_INTERVAL_SEC - elapsed)
            log.info(f"⏱  Cycle complete in {elapsed:.1f}s — sleeping {sleep_t:.0f}s")
            time.sleep(sleep_t)

        log.info("Bot stopped cleanly")
        self._mt5.disconnect()


# ═════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    bot = XAUBot()
    bot.run()
