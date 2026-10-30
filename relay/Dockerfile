FROM python:3.13-slim-bookworm
WORKDIR /app
COPY main.py .
# Run as a non-root user; the relay needs no write access anywhere.
RUN useradd --system --no-create-home relay
USER relay
ENV PYTHONUNBUFFERED=1
CMD ["python", "main.py"]
