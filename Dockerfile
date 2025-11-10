FROM python:3.11-slim

# Install common tools
RUN apt-get update && apt-get install -y \
    git curl sqlite3 build-essential procps \
    # for browser automation
    xvfb libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libxkbcommon0 libxext6 libxshmfence1 libxss1 libxtst6 \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libgbm1 libasound2 libatspi2.0-0 libpango-1.0-0 libpangocairo-1.0-0 \
    libgtk-3-0 libcairo2 libgdk-pixbuf-2.0-0 libxrender1 \
    fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Install multiple runtime managers
RUN pip install uv

# Create bencher user with matching UID
ARG USER_ID=1000
RUN useradd -m -u ${USER_ID} bencher
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs
RUN npm install -g @anthropic-ai/claude-code
RUN npx --yes playwright@latest install --with-deps

WORKDIR /home/bencher/workspace

# Install benchmark runner dependencies
COPY pyproject.toml .
COPY src/ src/
COPY data/ data/
COPY README.md .
RUN uv sync

RUN mkdir stories

# Change ownership of workspace to bencher user
RUN chown -R bencher:bencher /home/bencher && chmod -R 755 /home/bencher

# Switch to bencher user
USER bencher

# Entrypoint expects REPO_CONFIG env var
ENTRYPOINT ["uv", "run" ,"productengineerbench"]
