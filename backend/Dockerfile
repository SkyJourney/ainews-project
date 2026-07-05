FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# M4 Fallback A：Playwright 无头浏览器渲染需要浏览器二进制 + 系统依赖（fonts/libs），
# --with-deps 一并装好，避免运行时报 "missing shared library" 之类的错误。
RUN playwright install --with-deps chromium

COPY . .

# M7 起定时触发改用 Temporal 原生 Schedule（worker 启动时自 ensure），不再需要
# celery-worker/celery-beat 服务，镜像只跑这一个入口。
CMD ["python", "-m", "worker.worker"]
