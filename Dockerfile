# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  XAU/USD SMC Trading Bot — Dockerfile                                   ║
# ║  Strategy: Install Wine to run the Windows MT5 terminal on Linux        ║
# ║  Base    : Ubuntu 22.04 (stable Wine support)                           ║
# ╚══════════════════════════════════════════════════════════════════════════╝

FROM ubuntu:22.04

# Prevent interactive prompts during apt installs
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# ── የተስተካከለው የቅደም ተከተል ክፍል (Fixed Architecture Setup) ──────────────────
# በመጀመሪያ i386 አርክቴክቸር መጨመር አለበት፣ ከዚያ update መደረግ አለበት።
RUN dpkg --add-architecture i386 && apt-get update

# ── System packages ────────────────────────────────────────────────────────────
RUN apt-get install -y --no-install-recommends \
    wget curl gnupg software-properties-common ca-certificates \
    python3.11 python3.11-venv python3-pip \
    wine64 wine32:i386 winetricks \
    xvfb x11-utils fonts-liberation fonts-wine \
    net-tools procps \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Python symlink ─────────────────────────────────────────────────────────────
RUN ln -sf /usr/bin/python3.11 /usr/bin/python3 \
    && ln -sf /usr/bin/python3.11 /usr/bin/python

# ── Wine environment ───────────────────────────────────────────────────────────
ENV WINEPREFIX=/root/.wine
ENV WINEARCH=win64
ENV DISPLAY=:99

# ── Create working directory ───────────────────────────────────────────────────
WORKDIR /app

# ── Copy requirements first (layer caching) ───────────────────────────────────
COPY requirements.txt .

# ── Install Python dependencies ────────────────────────────────────────────────
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── Download MT5 Windows terminal installer ────────────────────────────────────
RUN wget -q "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" \
         -O /tmp/mt5setup.exe

# ── Initialize Wine prefix ────────────────────────────────────────────────────
RUN Xvfb :99 -screen 0 1024x768x16 & \
    sleep 3 && \
    winecfg /v win10 && \
    wineboot --init && \
    winetricks -q vcrun2019 && \
    winetricks -q dotnet48 && \
    echo "Wine initialized"

# ── Install MT5 silently under Wine ───────────────────────────────────────────
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

# ── Environment variable placeholders ──────────────────────────────────────────
ENV MT5_LOGIN=""
ENV MT5_PASSWORD=""
ENV MT5_SERVER=""
ENV GEMINI_API_KEY=""
ENV TELEGRAM_BOT_TOKEN=""
ENV TELEGRAM_CHAT_ID=""

EXPOSE 8080

# ── Entrypoint ─────────────────────────────────────────────────────────────────
ENTRYPOINT ["./entrypoint.sh"]
