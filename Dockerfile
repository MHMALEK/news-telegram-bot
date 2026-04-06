FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY news_telegram_bot/ ./news_telegram_bot/

RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1

CMD ["news-bot"]
