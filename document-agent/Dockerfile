# Document Agent - OCR, compose, diagrams, forms
FROM semos-agentura-base:latest

# System tools for document processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    pandoc \
    texlive-xetex \
    texlive-fonts-recommended \
    libreoffice-calc \
    libreoffice-writer \
    libreoffice-impress \
    && rm -rf /var/lib/apt/lists/*

# Node.js tools (marp, mermaid-cli, draw.io-export)
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

COPY document-agent/tools/package.json /app/document-agent/tools/
RUN cd /app/document-agent/tools && npm install --omit=dev \
    && npx playwright install --with-deps chromium

WORKDIR /app/document-agent

# Chromium needs --no-sandbox when running as root in Docker
RUN echo '{"args":["--no-sandbox","--disable-setuid-sandbox"]}' > /app/puppeteer.json
ENV PUPPETEER_CONFIG=/app/puppeteer.json

EXPOSE 8002

CMD ["uv", "run", "uvicorn", "document_agent.service:app", \
     "--host", "0.0.0.0", "--port", "8002"]
