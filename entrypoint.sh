#!/bin/bash
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  entrypoint.sh — Container startup sequence                            ║
# ║  Order: Xvfb (virtual display) → MT5 terminal → Python bot            ║
# ╚══════════════════════════════════════════════════════════════════════════╝

set -e

echo "======================================================"
echo "  XAU/USD SMC Bot — Starting container"
echo "  $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "======================================================"

# ── Validate required environment variables ───────────────────────────────────
REQUIRED_VARS="MT5_LOGIN MT5_PASSWORD MT5_SERVER"
for var in $REQUIRED_VARS; do
    if [ -z "${!var}" ]; then
        echo "ERROR: Environment variable '$var' is not set."
        echo "       Set it in your Koyeb/Render/Docker environment."
        exit 1
    fi
done
echo "✅ Environment variables validated"

# ── 1. Start virtual display (required for Wine/MT5 GUI rendering) ────────────
echo "🖥  Starting Xvfb virtual display on :99..."
Xvfb :99 -screen 0 1024x768x16 -ac +extension GLX +render -noreset &
XVFB_PID=$!
export DISPLAY=:99
sleep 3

if ! kill -0 $XVFB_PID 2>/dev/null; then
    echo "ERROR: Xvfb failed to start"
    exit 1
fi
echo "✅ Xvfb running (PID: $XVFB_PID)"

# ── 2. Find MT5 terminal executable ───────────────────────────────────────────
MT5_PATH="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe"

if [ ! -f "$MT5_PATH" ]; then
    # Fallback: search common Wine paths
    MT5_PATH=$(find "$WINEPREFIX" -name "terminal64.exe" 2>/dev/null | head -1)
fi

if [ -z "$MT5_PATH" ] || [ ! -f "$MT5_PATH" ]; then
    echo "WARNING: MT5 terminal not found at expected path."
    echo "         The MetaTrader5 Python library will attempt to locate it."
    echo "         If login fails, verify the Dockerfile MT5 installation step."
else
    echo "📍 MT5 found: $MT5_PATH"

    # ── 3. Launch MT5 terminal under Wine (background) ────────────────────────
    echo "🚀 Starting MT5 terminal under Wine..."
    wine "$MT5_PATH" \
        "/portable" \
        "/login:${MT5_LOGIN}" \
        "/password:${MT5_PASSWORD}" \
        "/server:${MT5_SERVER}" \
        &
    MT5_PID=$!
    echo "✅ MT5 launching (PID: $MT5_PID) — waiting 30s for initialization..."
    sleep 30

    if ! kill -0 $MT5_PID 2>/dev/null; then
        echo "WARNING: MT5 process exited early — may have already initialized."
    else
        echo "✅ MT5 terminal running"
    fi
fi

# ── 4. Optional: lightweight HTTP health endpoint ─────────────────────────────
# Koyeb/Render require a port response to mark the service healthy.
# This minimal Python HTTP server answers GET / with 200 OK.
python3 -c "
import http.server, threading, os

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'XAU/USD Bot Running')
    def log_message(self, *args): pass

port = int(os.environ.get('PORT', 8080))
server = http.server.HTTPServer(('0.0.0.0', port), Handler)
t = threading.Thread(target=server.serve_forever, daemon=True)
t.start()
print(f'Health endpoint listening on :{port}')
" &

# ── 5. Start the trading bot ───────────────────────────────────────────────────
echo "======================================================"
echo "  Starting XAU/USD Python trading bot..."
echo "======================================================"

# Trap signals for graceful shutdown
cleanup() {
    echo "Shutdown signal received — stopping bot..."
    kill $BOT_PID 2>/dev/null
    kill $MT5_PID 2>/dev/null
    kill $XVFB_PID 2>/dev/null
    exit 0
}
trap cleanup SIGTERM SIGINT

python3 main.py &
BOT_PID=$!
echo "✅ Bot running (PID: $BOT_PID)"

# Wait for bot to exit (it shouldn't in normal operation)
wait $BOT_PID
BOT_EXIT=$?

echo "Bot exited with code $BOT_EXIT"
echo "Cleaning up..."
kill $MT5_PID  2>/dev/null
kill $XVFB_PID 2>/dev/null
exit $BOT_EXIT
