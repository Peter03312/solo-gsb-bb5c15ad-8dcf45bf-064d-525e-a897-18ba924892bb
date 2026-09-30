FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV DB_PATH=/data/app.db \
    PORT=8000

VOLUME /data
EXPOSE 8000

# 启动即完成 SQLite 初始化（建库建表），随后对外服务
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
