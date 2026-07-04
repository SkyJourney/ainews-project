FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# M4 Fallback A：Playwright 无头浏览器渲染需要浏览器二进制 + 系统依赖（fonts/libs），
# --with-deps 一并装好，避免运行时报 "missing shared library" 之类的错误。
RUN playwright install --with-deps chromium

COPY . .

# 默认跑 Temporal worker；celery-worker/celery-beat 服务在 docker-compose 里
# 用 command: 覆盖成 celery 命令，共用同一个镜像，不必分开维护两个 Dockerfile。
CMD ["python", "-m", "worker.worker"]
