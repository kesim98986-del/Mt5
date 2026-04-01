
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies required for matplotlib and other packages
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libpng-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Command to run the bot
CMD ["python", "main.py"]
