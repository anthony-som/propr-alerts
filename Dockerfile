FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ALERTS_DB=/data/alerts.db

WORKDIR /srv
RUN adduser --system --group --home /srv alerts

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY propr_alerts ./propr_alerts

RUN mkdir -p /data && chown -R alerts:alerts /data /srv
USER alerts

CMD ["python", "-m", "propr_alerts.bot"]
