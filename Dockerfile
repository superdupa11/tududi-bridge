FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/
COPY prompts/ /app/prompts/

ENV PYTHONUNBUFFERED=1 \
    BRIDGE_CONFIG=/config/config.yml \
    BRIDGE_PROMPTS=/app/prompts \
    BRIDGE_DB=/data/queue.db

CMD ["python", "worker.py"]
