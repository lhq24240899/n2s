# 计量检测智能问数系统 —— 部署镜像（百度 AI Studio highcode / 任意容器平台）
# 入口为 Streamlit Web UI（Streamlit.app.py）。
FROM python:3.11-slim

WORKDIR /app

# 系统依赖：psycopg 用 [binary] 自带轮子一般够用；保险起见装编译/SSL 基础库。
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用层缓存：依赖变动远少于代码变动）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

# 再拷源码（排除 .env / .git / 缓存，见 .dockerignore）
COPY . .

# Streamlit 服务端配置：绑定 0.0.0.0，headless，关闭使用统计上报
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_ENABLE_CORS=false

# 端口：优先用平台注入的 $PORT，否则 8081
EXPOSE 8081
CMD streamlit run Streamlit.app.py --server.address=0.0.0.0 --server.port=${PORT:-8081} --server.headless=true
