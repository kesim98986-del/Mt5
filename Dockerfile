# 1. Python base image
FROM python:3.10-slim

# 2. Set the working directory in the container
WORKDIR /app

# 3. Install system dependencies for Matplotlib and image rendering
RUN apt-get update && apt-get install -y \
    build-essential \
    libpng-dev \
    libfreetype6-dev \
    pkg-config \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# 4. Copy requirements and install python libraries
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copy the rest of the bot's source code
COPY . .

# 6. Command to run the bot
CMD ["python", "main.py"]
