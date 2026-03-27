# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  XAU/USD SMC Trading Bot — Dockerfile (FIXED VERSION)                  ║
# ║  Strategy: Install Wine to run the Windows MT5 terminal on Linux        ║
# ╚══════════════════════════════════════════════════════════════════════════╝

FROM ubuntu:22.04

# Prevent interactive prompts during apt installs
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# ── 1. የተስተካከለ የቅደም ተከተል ክፍል (Architecture Setup) ──────────────────────
# [span_1](start_span)wine32:i386 እንዲገኝ መጀመሪያ አርክቴክቸሩ መታወቅ አለበት[span_1](end_span)
RUN dpkg --add-architecture i386 && apt-get update

# ── 2. System packages ────────────────────────────────────────────────────────
RUN apt-get install -y --no-install-recommends \
    wget curl gnupg software-properties-common ca-certificates \
    python3.11 python3.11-venv python3-pip \
    wine64 wine32:i386 winetricks \
    xvfb x11-utils fonts-liberation fonts-wine \
    net-tools procps \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── 3. Python symlink ─────────────────────────────────────────────────────────
RUN ln -sf /usr/bin/python3.11 /usr/bin/python3 \
    && ln -sf /usr/bin/python3.11 /usr/bin/python

# ── 4. Wine environment ───────────────────────────────────────────────────────
ENV WINEPREFIX=/root/.wine
ENV WINEARCH=win64
ENV DISPLAY=:99

WORKDIR /app

# ── 5. Requirements (MetaTrader5 በሊኑክስ ስለማይገኝ እዚህ አይጫንም) ──────────────
COPY requirements.txt .
# [span_2](start_span)ማሳሰቢያ፡ MetaTrader5ን ከ requirements.txt ውስጥ ማውጣትህን እርግጠኛ ሁን[span_2](end_span)
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── 6. Download MT5 Installer ─────────────────────────────────────────────────
RUN wget -q "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" \
         -O /tmp/mt5setup.exe

# ── 7. Initialize Wine & Install MT5 ──────────────────────────────────────────
# [span_3](start_span)ለ Railway Build ጊዜ እንዲቆጥብ winetricks ቀንሰናል[span_3](end_span)
RUN Xvfb :99 -screen 0 1024x768x16 & \
    sleep 5 && \
    winecfg /v win10 && \
    wineboot --init && \
    # MT5ን በዝምታ መጫን
    wine /tmp/mt5setup.exe /auto && \
    sleep 15 && \
    echo "Wine & MT5 Setup Complete"

# ── 8. Copy application code ──────────────────────────────────────────────────
COPY main.py .
COPY entrypoint.sh .
[span_4](start_span)RUN chmod +x entrypoint.sh[span_4](end_span)

# ── 9. Health check ───────────────────────────────────────────────────────────
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    [span_5](start_span)CMD pgrep -f "main.py" > /dev/null || exit 1[span_5](end_span)

# ── 10. Environment variables ─────────────────────────────────────────────────
ENV MT5_LOGIN=""
ENV MT5_PASSWORD=""
ENV MT5_SERVER=""
ENV GEMINI_API_KEY=""
ENV TELEGRAM_BOT_TOKEN=""

EXPOSE 8080
ENTRYPOINT ["./entrypoint.sh"]
