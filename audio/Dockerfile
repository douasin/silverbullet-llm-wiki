FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# server.py 由 docker-compose.yml 以 volume 掛進 /app,改程式只要 restart;改依賴才重建 image

EXPOSE 8009
CMD ["python", "server.py"]
