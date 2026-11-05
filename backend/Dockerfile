# 后端镜像（M3.8）：Python 3.12（D27）、系统 tzdata、非 root；main.py serve 以 Uvicorn 单 worker 运行网站与调度器（D25）。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 bfradar \
    && useradd --system --uid 10001 --gid bfradar --no-create-home --home-dir /app bfradar

WORKDIR /app

# 依赖单独一层，只改代码时不重装；requirements.txt 由 uv export 生成，带哈希
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pyproject.toml main.py ./
COPY app ./app
# 数据库与备份都在 data/ 下，由宿主机目录挂载（Docker Desktop 不用管属主；Linux 宿主机上要把该目录的属主改成 10001）
RUN mkdir data && chown bfradar:bfradar data

USER bfradar
EXPOSE 8000
# 监听地址写死：前端按 app:8000 取数据与转发，不受 .env 里本机开发用的 HOST/PORT 影响
CMD ["python", "main.py", "serve", "--host", "0.0.0.0", "--port", "8000"]
