# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  XAU/USD SMC Trading Bot — Dockerfile (Final Fix)                      ║
# ╚══════════════════════════════════════════════════════════════════════════╝

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# ── 1. Setup Architecture & Install Everything ──────────────────────────────
RUN dpkg --add-architecture i386 && apt-get update && \
    apt-get install -y --no-install-recommends \
    wget curl ca-certificates \
    python3.11 python3.11-venv python3-pip \
    wine64 wine32:i386 xvfb x11-utils \
    fonts-liberation fonts-wine \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── 2. Python & Wine Setup ──────────────────────────────────────────────────
RUN ln -sf /usr/bin/python3.11 /usr/bin/python3 && ln -sf /usr/bin/python3.11 /usr/bin/python

ENV WINEPREFIX=/root/.wine
ENV WINEARCH=win64
ENV DISPLAY=:99
ENV WINEDEBUG=-all

WORKDIR /app

# ── 3. Install Python dependencies ──────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ── 4. Download & Install MT5 ───────────────────────────────────────────────
RUN wget -q "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" -O /tmp/mt5setup.exe

# Xvfb ን በመጠቀም በፀጥታ መጫን
RUN Xvfb :99 -screen 0 1024x768x16 & \
    sleep 5 && \
    wineboot -u && \
    wine /tmp/mt5setup.exe /auto && \
    sleep 15 && \
    echo "MT5 Installation finished"

# ── 5. Copy App Code ────────────────────────────────────────────────────────
COPY main.py .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["./entrypoint.sh"]
