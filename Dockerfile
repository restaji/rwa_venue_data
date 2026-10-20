FROM python:3.11-slim AS base

RUN groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chown -R app:app /app
USER app

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "exec gunicorn -k eventlet -w 1 --bind 0.0.0.0:${PORT:-8000} app:app"]
