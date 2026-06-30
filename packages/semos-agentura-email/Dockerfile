# Email Agent - IMAP/SMTP backend (no COM on Linux)
FROM semos-agentura-base:latest

WORKDIR /app/email-agent

EXPOSE 8001

CMD ["uv", "run", "uvicorn", "email_agent.service:app", \
     "--host", "0.0.0.0", "--port", "8001"]
