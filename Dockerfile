# ============================================================
# 多智能体诊断系统 - 容器镜像
# 对应 JD「云原生部署」：将服务标准化打包，一次构建处处运行
# ============================================================
FROM python:3.11-slim

# 工作目录
WORKDIR /app

# 系统依赖（faiss / numpy 等科学计算库的编译/运行所需）
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# ── 先装依赖（利用 Docker 层缓存：requirements 不变就不重装）──
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
        -i https://pypi.tuna.tsinghua.edu.cn/simple

# ── 再复制代码（代码改动不会触发上面依赖层重装）──
COPY . .

# 构建模拟工厂数据库（factory.db），让镜像可独立运行
RUN python data/build_database.py

# API Key 通过运行时环境变量注入，绝不写进镜像（安全最佳实践）
# 运行时用 -e DASHSCOPE_API_KEY=xxx 或 docker-compose 的 environment 传入

EXPOSE 8000

# 启动 REST API 服务，监听所有网卡（容器内必须 0.0.0.0）
CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8000"]
