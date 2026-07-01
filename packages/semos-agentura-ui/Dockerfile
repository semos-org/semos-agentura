# Agentura UI - chat client + filesystem agent (in-process)
FROM semos-agentura-base:latest

WORKDIR /app

# Config template (mount config.yml at runtime to override)
COPY packages/semos-agentura-ui/config.example.yml /app/packages/semos-agentura-ui/config.example.yml

EXPOSE 5006

CMD ["uv", "run", "python", "-m", "semos.agentura.ui"]
