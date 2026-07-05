FROM python:3.12-slim

# ffmpeg（mp3変換に必須）
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py index.html ./
COPY lists ./lists

# 多くのホストは $PORT を渡す（未指定なら 8765）
ENV PORT=8765
EXPOSE 8765

CMD ["python", "app.py"]
