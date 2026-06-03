# 部署指南

本系统支持三种部署方式，从开发到生产逐级演进。

---

## 方式一：本地直接运行（开发调试）

```bash
pip install -r requirements.txt
# 设置 API Key
export DASHSCOPE_API_KEY=你的key        # Windows: $env:DASHSCOPE_API_KEY="你的key"
# 构建数据库（首次）
cd data && python build_database.py && cd ..
# 启动 API 服务
uvicorn api_server:app --reload --port 8000
```
浏览器打开 http://localhost:8000/docs 交互测试。

---

## 方式二：Docker 容器化（标准化交付）

**前置**：安装 Docker Desktop

```bash
# 1. 在 Multi_Agent/ 目录创建 .env 文件
echo "DASHSCOPE_API_KEY=你的key" > .env

# 2. 构建并启动
docker compose up --build

# 3. 访问
# http://localhost:8000/docs
```

**单独构建/运行（不用 compose）：**
```bash
docker build -t multi-agent-diagnosis:2.0 .
docker run -p 8000:8000 -e DASHSCOPE_API_KEY=你的key multi-agent-diagnosis:2.0
```

**容器化要点（面试讲点）：**
- 分层构建：依赖层和代码层分离，代码改动不触发依赖重装
- API Key 运行时注入，不写进镜像（安全）
- 数据卷持久化：记忆库 memory.db 容器重启不丢失
- 健康检查：编排平台据此判断服务就绪，支撑高可用

---

## 方式三：Kubernetes 部署（云原生 / 高可用）

**前置**：minikube（本地单节点 K8s，笔记本可跑）+ kubectl

```bash
# 1. 启动本地集群
minikube start

# 2. 让 minikube 用本地镜像
minikube image load multi-agent-diagnosis:2.0

# 3. 创建 API Key 密钥（不明文写进 manifest）
kubectl create secret generic dashscope-secret \
    --from-literal=api-key=你的key

# 4. 部署
kubectl apply -f k8s/

# 5. 访问
minikube service diagnosis-api-service
```

**K8s 要点（面试讲点）：**
- Deployment 管理 Pod 副本，挂掉自动重建（自愈）
- replicas 可水平扩展（横向扩容）
- Secret 管理密钥，与代码/镜像解耦
- liveness/readiness 探针实现健康检查与流量控制
- Service 提供稳定的访问入口和负载均衡

> ⚠️ **已知限制（诚实说明）**：当前每个 Pod 用各自的本地 SQLite——`factory.db`
> 启动时自动重建（只读基线数据，各副本一致），但 `memory.db` 与新建工单是 **Pod 本地状态**，
> 多副本/HPA 扩缩容时不共享、会分叉。因此本 K8s 方案演示的是"无状态服务"的高可用编排；
> 真正多副本一致需把记忆/工单等有状态数据外置到共享数据库（PostgreSQL），让 Pod 无状态化。
> 详见 [从原型到生产_演进设计.md](docs/从原型到生产_演进设计.md) 可靠性层。

---

## 演进路径总结

```
本地运行     →   Docker容器化   →   K8s编排
（开发）          （标准交付）        （生产/高可用）
单进程            单容器             多副本+自愈+扩展
```
