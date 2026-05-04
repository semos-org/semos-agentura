# Agentura UI - chat client + filesystem-agent (in-process)
FROM semos-agentura-base:latest

WORKDIR /app

# Config and static files not in src/
COPY agentura-ui/config.yml /app/agentura-ui/

EXPOSE 5006

CMD ["uv", "run", "python", "-m", "agentura_ui"]
