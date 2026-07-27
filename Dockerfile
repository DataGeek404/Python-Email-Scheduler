FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EMAIL_LOG_FILE=-

WORKDIR /app

RUN useradd --create-home --uid 10001 scheduler

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

COPY --chown=scheduler:scheduler . .
USER scheduler

HEALTHCHECK --interval=2m --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "daily_email_report.py", "--healthcheck"]

CMD ["python", "daily_email_report.py"]
