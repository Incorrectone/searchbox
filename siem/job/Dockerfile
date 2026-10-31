FROM python:3.13-slim-bookworm
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
RUN useradd --system --no-create-home siem
USER siem
ENV PYTHONUNBUFFERED=1
CMD ["python", "main.py"]
