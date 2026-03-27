# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  XAU/USD SMC Trading Bot — Dockerfile (FIXED)                          ║
# ║  Target  : Railway / Koyeb / Render                                     ║
# ╚══════════════════════════════════════════════════════════════════════════╝

FROM ubuntu:22.04

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# ── 1. Setup Architecture & Update ──────────────────────────────────────────
# ይህ መስመር wine32 እንዲጫን የግድ መቅደም አለበት
RUN dpkg --add-architecture i386 && apt-get update

# ── 2. Install System Packages ───────────────────────────────────────────────
RUN apt-get install -y --no-install-recommends \
    wget curl gnupg software-properties-common ca-certificates \
    python3.11 python3.11-venv python3-pip \
    wine64 wine32:i386 winetricks \
    xvfb x11-utils fonts-liberation fonts-wine \
    net-tools procps \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── 3. Python Setup ──────────────────────────────────────────────────────────
RUN ln -sf /usr/bin/python3.11 /usr/bin/python3 \
    && ln -sf /usr/bin/python3.11 /usr/bin/python

ENV WINEPREFIX=/root/.wine
ENV WINEARCH=win64
ENV DISPLAY=:99

WORKDIR /app

# ── 4. Dependencies ──────────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── 5. MT5 Setup ─────────────────────────────────────────────────────────────
RUN wget -q "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" \
         -O /tmp/mt5setup.exe

# Initialize Wine and Install MT5
RUN Xvfb :99 -screen 0 1024x768x16 & \
    sleep 5 && \
    winecfg /v win10 && \
    wineboot --init && \
    winetricks -q vcrun2019 && \
    wine /tmp/mt5setup.exe /auto && \
    sleep 15

# ── 6. App Files ─────────────────────────────────────────────────────────────
COPY main.py .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# ── 7. Runtime Config ────────────────────────────────────────────────────────
ENV MT5_LOGIN=""
ENV MT5_PASSWORD=""
ENV MT5_SERVER=""
ENV GEMINI_API_KEY=""
ENV TELEGRAM_BOT_TOKEN=""

EXPOSE 8080
ENTRYPOINT ["./entrypoint.sh"]
