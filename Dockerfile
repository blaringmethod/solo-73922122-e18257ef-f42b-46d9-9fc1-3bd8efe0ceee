FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    COST_ALLOCATION_DB=/data/cost_allocations.sqlite3

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷应用代码
COPY app ./app

# 成本分摊数据库放在卷上：容器重建后已发布的版本仍在
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 静态血缘分析 + 部门成本分摊（SQLite 仅用于成本分摊版本化存储）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
