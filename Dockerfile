# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  XAU/USD SMC Trading Bot — Dockerfile                                   ║
# ║  Strategy: Install Wine to run the Windows MT5 terminal on Linux        ║
# ║  Base    : Ubuntu 22.04 (stable Wine support)                           ║
# ║  Target  : Koyeb / Render / Any Linux container host                    ║
# ╚══════════════════════════════════════════════════════════════════════════╝
#
# HOW THE LINUX+MT5 TRICK WORKS
# ──────────────────────────────
# MetaTrader5's Python library communicates with a running MT5 terminal.
# That terminal is a Windows .exe binary.
# Wine lets Linux run Windows executables natively (no VM, no emulation).
# Steps:
#   1. Install Wine + Winetricks (Windows compatibility layer)
#   2. Download MT5 terminal installer via wget
#   3. Run the installer silently under Wine → installs to ~/.wine/drive_c/
#   4. The MetaTrader5 Python package then talks to that Wine-hosted terminal
#      via a named pipe / socket — exactly as on Windows.
#
# NOTE: Some brokers block MT5 logins from cloud IPs.
#       If so, use a residential proxy or a VPS with a dedicated IP.

# ── Base image ────────────────────────────────────────────────────────────────
FROM ubuntu:22.04

# Prevent interactive prompts during apt installs
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# ── 1. የተስተካከለ የቅደም ተከተል ክፍል (Architecture Setup) ──────────────────────
# wine32:i386 እንዲገኝ መጀመሪያ አርክቴክቸሩ መታወቅ አለበት
RUN dpkg --add-architecture i386 && apt-get update

# ── System packages ────────────────────────────────────────────────────────────
RUN apt-get install -y --no-install-recommends \
    # Core tools
    wget curl gnupg software-properties-common ca-certificates \
    # Python
    python3.11 python3.11-venv python3-pip \
    # Wine prerequisites
    wine64 wine32:i386 winetricks \
    # Display server (required for Wine/MT5 GUI, even headless)
    xvfb x11-utils \
    # Fonts (MT5 rendering)
    fonts-liberation fonts-wine \
    # Network & process tools
    net-tools procps \
    # Cleanup
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Python symlink ─────────────────────────────────────────────────────────────
RUN ln -sf /usr/bin/python3.11 /usr/bin/python3 \
    && ln -sf /usr/bin/python3.11 /usr/bin/python

# ── Wine environment ───────────────────────────────────────────────────────────
ENV WINEPREFIX=/root/.wine
ENV WINEARCH=win64
ENV DISPLAY=:99
# Wine ድምፅና ግራፊክስ እንዳይፈልግ ያደርጋሉ (ከንቱ የ RPC ስህተቶችን ለመከላከል)
ENV WINEDEBUG=-all
ENV WINEPATH=/usr/lib/wine

# ── Create working directory ───────────────────────────────────────────────────
WORKDIR /app

# ── Copy requirements first (layer caching) ───────────────────────────────────
COPY requirements.txt .

# ── Install Python dependencies ────────────────────────────────────────────────
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── Download MT5 Windows terminal installer ────────────────────────────────────
# MetaQuotes official MT5 installer — runs under Wine
RUN wget -q "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" \
         -O /tmp/mt5setup.exe

# ── Initialize Wine prefix ────────────────────────────────────────────────────
RUN Xvfb :99 -screen 0 1024x768x16 & \
    sleep 3 && \
    winecfg /v win10 && \
    wineboot --init && \
    # Install required Windows runtime components
    winetricks -q vcrun2019 && \
    # winetricks -q dotnet48 && # ለ Railway Build ጊዜ እንዲቆጥብdotnet ቀንሰናል
    echo "Wine initialized"

# ── Install MT5 silently under Wine ───────────────────────────────────────────
# /auto flag: silent install, /portable: self-contained directory install
RUN Xvfb :99 -screen 0 1024x768x16 & \
    sleep 3 && \
    wine /tmp/mt5setup.exe /auto && \
    sleep 10 && \
    echo "MT5 installed"

# ── Copy application code ──────────────────────────────────────────────────────
COPY main.py .
COPY entrypoint.sh .

RUN chmod +x entrypoint.sh

# ── Health check ───────────────────────────────────────────────────────────────
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD pgrep -f "main.py" > /dev/null || exit 1

# ── Environment variable placeholders (override at runtime) ───────────────────
ENV MT5_LOGIN=""
ENV MT5_PASSWORD=""
ENV MT5_SERVER=""
ENV GEMINI_API_KEY=""
ENV TELEGRAM_BOT_TOKEN=""
ENV TELEGRAM_CHAT_ID=""

# ── Expose port for optional health endpoint ───────────────────────────────────
EXPOSE 8080

# ── Entrypoint ─────────────────────────────────────────────────────────────────
ENTRYPOINT ["./entrypoint.sh"]
