# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  XAU/USD SMC Trading Bot — Dockerfile                                   ║
# ║  Strategy: Install Wine to run the Windows MT5 terminal on Linux        ║
# ╚══════════════════════════════════════════════════════════════════════════╝

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# ── 1. Setup Architecture & Update ──────────────────────────────────────────
RUN dpkg --add-architecture i386 && apt-get update

# ── 2. Install Packages ─────────────────────────────────────────────────────
RUN apt-get install -y --no-install-recommends \
    wget curl ca-certificates \
    python3.11 python3.11-venv python3-pip \
    wine64 wine32:i386 xvfb x11-utils \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── 3. Wine environment ───────────────────────────────────────────────────────
ENV WINEPREFIX=/root/.wine
ENV WINEARCH=win64
ENV DISPLAY=:99
ENV WINEDEBUG=-all 

WORKDIR /app

# ── 4. Python Setup ──────────────────────────────────────────────────────────
RUN ln -sf /usr/bin/python3.11 /usr/bin/python3 && ln -sf /usr/bin/python3.11 /usr/bin/python

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ── 5. MT5 Installer (ዳውንሎድ ብቻ) ────────────────────────────────────────────
RUN wget -q "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" -O /tmp/mt5setup.exe

# ── 6. Silent Installation ────────────────────────────────────────────────────
# እዚህ ጋር sleep ሰከንዶችን ቀንሰናል Railway እንዳያቋርጠው
RUN Xvfb :99 -screen 0 1024x768x16 & \
    sleep 2 && \
    wineboot -u && \
    wine /tmp/mt5setup.exe /auto && \
    sleep 5 && \
    echo "MT5 setup finished"

# ── 7. Copy Code ──────────────────────────────────────────────────────────────
COPY main.py .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["./entrypoint.sh"]
