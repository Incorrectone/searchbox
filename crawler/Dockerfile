FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    MODEL_DIR=/models/minilm

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY antiseo_crawler ./antiseo_crawler

# Non-root by default; run.sh overrides with your own uid so files in the
# model folder stay owned by you.
USER 10001
ENTRYPOINT ["python", "-m", "antiseo_crawler"]
CMD ["crawl"]
