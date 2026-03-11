FROM python:3.10-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    ffmpeg \
    aria2 \
    wget \
    bash \
    ca-certificates \
    software-properties-common \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip
RUN pip install --upgrade pip

# Install python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Create required folders
RUN mkdir -p /app/cookies
RUN mkdir -p /app/downloads

# Expose port
EXPOSE 5000

CMD bash -c "flask run -h 0.0.0.0 -p 5000 & python3 -m devgagan"
