# ---- 构建阶段：安装 Node.js ----
FROM python:3.12-slim

# 安装 Node.js 22.x（PyExecJS 执行闲鱼签名脚本）
RUN apt-get update && apt-get install -y curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制依赖文件，利用 Docker 层缓存
COPY requirements.txt ./

RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY . .

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8090/health || exit 1

# 默认运行桥接服务（扫码登录 + WebSocket + 内部 API）
CMD ["python", "run_live.py"]

# --- 构建 & 运行 ---
# docker build -t xianyu-bridge .
# docker run -it --env-file .env -p 127.0.0.1:8090:8090 -v xianyu-data:/app/data xianyu-bridge
