# XAU/USD SMC Trading Bot — Complete Deployment Guide

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    Linux Container (Docker)                     │
│                                                                 │
│  ┌──────────┐    ┌─────────────────┐    ┌───────────────────┐  │
│  │  Xvfb    │    │  MT5 Terminal   │    │   Python Bot      │  │
│  │ Display  │───▶│  (Wine/Windows) │◀───│   main.py         │  │
│  │  :99     │    │  terminal64.exe │    │                   │  │
│  └──────────┘    └─────────────────┘    └────────┬──────────┘  │
│                                                  │             │
└──────────────────────────────────────────────────┼─────────────┘
                                                   │
                        ┌──────────────────────────┼───────────┐
                        │          External APIs   │           │
                        │                          │           │
                        │  ┌────────────┐  ┌───────▼────────┐  │
                        │  │ Gemini AI  │  │ Telegram Bot   │  │
                        │  │ Free Tier  │  │ Alerts         │  │
                        │  └────────────┘  └────────────────┘  │
                        └───────────────────────────────────────┘
```

## File Structure

```
xauusd-bot/
├── main.py              ← Trading bot (all logic)
├── Dockerfile           ← Wine + MT5 + Python setup
├── entrypoint.sh        ← Startup sequence script
├── docker-compose.yml   ← Local development
├── requirements.txt     ← Python dependencies
├── .env.example         ← Secrets template (copy → .env)
└── .gitignore           ← Prevent secrets being committed
```

## Step 1 — Get Your Free API Keys

### Gemini AI (Free, No Credit Card)
1. Go to https://aistudio.google.com/app/apikey
2. Sign in with Google account
3. Click **"Create API Key"**
4. Copy the key → it starts with `AIzaSy...`
5. Free tier: 15 requests/min, 1,500/day — more than enough

### Telegram Bot
1. Open Telegram → search `@BotFather`
2. Send `/newbot` → follow prompts
3. Copy the token (format: `123456789:ABCdef...`)
4. Find your Chat ID: message `@userinfobot`

### MT5 Demo Account (Free)
1. Download MT5 from your broker (ICMarkets, Pepperstone, etc.)
2. Open a free demo account
3. Note: Login number, Password, Server name

## Step 2 — Local Testing

```bash
# Clone / create project folder
mkdir xauusd-bot && cd xauusd-bot

# Copy all files here, then:
cp .env.example .env
nano .env   # Fill in your credentials

# Test locally with Docker
docker build -t xauusd-bot .
docker run --env-file .env xauusd-bot

# Or with docker-compose
docker-compose up --build
```

## Step 3 — Deploy to Koyeb (Free Tier)

Koyeb offers a free "Nano" instance (512MB RAM, shared CPU) — enough for this bot.

### Option A — Deploy from GitHub (Recommended)
1. Push your code to a **private** GitHub repo
2. Go to https://app.koyeb.com → **Create Service**
3. Select **"GitHub"** → choose your repo
4. Set **Build type** = "Dockerfile"
5. Add environment variables:
   ```
   MT5_LOGIN          = 12345678
   MT5_PASSWORD       = YourPassword
   MT5_SERVER         = ICMarkets-Demo
   GEMINI_API_KEY     = AIzaSy...
   TELEGRAM_BOT_TOKEN = 123456:ABC...
   TELEGRAM_CHAT_ID   = 987654321
   ```
6. Set **Port** = 8080 (health check endpoint)
7. Click **Deploy**

### Option B — Deploy to Render (Free Tier)
1. Push to GitHub (private repo)
2. Go to https://render.com → **New Web Service**
3. Connect GitHub repo
4. Set **Environment** = Docker
5. Add the same environment variables
6. Set **Health Check Path** = `/`
7. Deploy

### Option C — Docker on Any VPS

```bash
# On your Linux VPS (Ubuntu 22.04 recommended):
git clone <your-private-repo>
cd xauusd-bot
cp .env.example .env && nano .env

# Build and run
docker build -t xauusd-bot .
docker run -d \
  --name xauusd-bot \
  --restart unless-stopped \
  --env-file .env \
  -p 8080:8080 \
  -v $(pwd)/logs:/app/logs \
  xauusd-bot

# View logs
docker logs -f xauusd-bot
```

## Step 4 — Monitor Your Bot

### Telegram Alerts You'll Receive
| Event | Message |
|-------|---------|
| Bot starts | Balance, risk amount, sessions |
| Setup found | Direction, price, RSI, reason |
| Gemini skips trade | Direction + AI response |
| Trade opens | Ticket, lot, entry, SL, TP1, TP2 |
| TP1 hit + BE set | Ticket, break-even price |
| Position closed | P&L result |
| Bot error | Error message (capped 500 chars) |
| Daily DD limit | Drawdown % — halted for day |

### Log File
The bot writes `bot.log` in `/app/` inside the container.
Mount it with `-v $(pwd)/logs:/app/logs` to persist on host.

## Risk Parameters (Edit in main.py Config class)

| Parameter | Default | Description |
|-----------|---------|-------------|
| RISK_PCT | 1% | Percent of balance risked per trade |
| MAX_SPREAD_PTS | 30 | Skip if spread exceeds this |
| DAILY_DD_LIMIT | 3% | Halt trading at 3% daily drawdown |
| TP1_RR | 2.0 | Take Profit 1 at 1:2 risk-reward |
| TP2_RR | 4.0 | Take Profit 2 at 1:4 risk-reward |
| SCAN_INTERVAL_SEC | 300 | Scan every 5 minutes |
| OB_LOOKBACK | 30 | Candles to look back for Order Blocks |
| SESSION_WINDOWS | [(8,17),(13,21)] | UTC hours: London + NY |

## Critical Broker Notes

**IP Whitelist**: Some brokers block logins from cloud IPs (Koyeb/Render have shared IPs).
- Solution 1: Use a broker that doesn't restrict IP (e.g., Pepperstone Demo)
- Solution 2: Deploy on a VPS with a dedicated IP and whitelist it with your broker
- Solution 3: Use a residential proxy (add proxy settings to MT5 terminal config)

**Wine + MT5 Compatibility**: Tested with MT5 Build 4000+.
If MT5 fails to install, check the Docker build logs for the `wine mt5setup.exe` step.

**Memory**: Bot uses ~300–500MB RAM (Wine + MT5 + Python). Koyeb free = 512MB — borderline.
If OOM: reduce `OB_LOOKBACK` to 15 and candle counts in `_cycle()`.

## .gitignore (Add This)

```
.env
*.log
__pycache__/
*.pyc
logs/
```

## Disclaimer

This bot is for educational purposes. Forex/Gold trading carries significant risk.
Always test on a demo account before using real money.
Past performance does not guarantee future results.
