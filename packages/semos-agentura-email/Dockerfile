# Email Agent - IMAP/SMTP backend (no COM on Linux)
FROM semos-agentura-base:latest

WORKDIR /app/packages/semos-agentura-email

EXPOSE 8001

CMD ["uv", "run", "uvicorn", "semos.agentura.email.service:app", \
     "--host", "0.0.0.0", "--port", "8001"]
