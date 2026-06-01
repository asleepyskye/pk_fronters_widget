FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/app/data/pk_fronters_widget.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

CMD ["python", "bot.py"]
