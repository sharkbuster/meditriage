# 用官方 slim 镜像，体积小、启动快
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖，利用镜像层缓存：改代码不会重装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "psycopg[binary]"

COPY app ./app
COPY scripts ./scripts
COPY tests ./tests
COPY pytest.ini README.md ./

RUN mkdir -p data

EXPOSE 8000

# 默认用 SQLite 起，docker compose 里会覆盖成 PostgreSQL
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
