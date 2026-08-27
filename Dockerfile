FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_DATA_DIR=/data \
    TZ=Europe/Berlin

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app

RUN python -m compileall -q /app/app \
    && python -c "from app.bmf_tax import calculate_bmf_2026; r=calculate_bmf_2026(monthly_tax_gross=5000.00,tax_class=1,kv_additional_rate=2.5,childless_care_surcharge=True); assert r['wage_tax'] == 785.83, r" \
    && mkdir -p /data/documents

EXPOSE 8788

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8788/health >/dev/null || exit 1

CMD ["python", "-m", "app.server"]
