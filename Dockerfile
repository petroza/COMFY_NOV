# ComfyLocal v kontejneru.
# Appka jen mluví na ComfyUI po síti — sama žádnou GPU nepotřebuje.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COMFYLOCAL_HOST=0.0.0.0 \
    COMFYLOCAL_PORT=8770 \
    COMFYLOCAL_OPEN_BROWSER=0

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY comfylocal/ ./comfylocal/
COPY web/ ./web/
COPY workflows/ ./workflows/
COPY config.example.json ./

# Uploady, výstupy a databáze patří do volume, ať přežijí redeploy.
VOLUME ["/app/data"]
EXPOSE 8770

# Adresu ComfyUI dej přes COMFY_URL, nebo přimountuj vlastní /app/config.json.
CMD ["python", "-m", "comfylocal"]
