# 制造企业多智能体业务协同系统

> 面向半导体制造场景的 **多智能体协作（Multi-Agent）+ Agentic RAG** 设备异常诊断系统。
> 用户一句话描述设备异常，系统由 6 个分工 Agent 协作完成
> 「数据查询 → 知识检索 → 根因诊断 → 质量评审 → 生成工单」全闭环，
> 具备**记忆管理、并行执行、防幻觉校验、全链路可观测性、量化评估**等工程化能力。

---

## ✨ 核心亮点

| 维度 | 能力 |
|------|------|
| 🤝 多智能体协作 | 协调者-执行者架构，1协调 + 6执行 Agent，自主规划调度 |
| 🧠 记忆管理 | 三层记忆：会话记忆（多轮）+ 诊断历史持久化 + 经验自动召回 |
| ⚡ 并行执行 | 无依赖的 Agent 用线程池并发，线程安全可观测性 |
| 📚 Agentic RAG | 企业知识库检索，**FAISS / Milvus / LlamaIndex 三套实现** |
| 🔧 工具调用 | 13 个工具，输入校验 + 异常隔离 + 错误码体系 |
| 🛡️ 防幻觉 | 提示约束 + 事后正则核对报告数字与数据库真实值 |
| 📊 可观测性 | 结构化轨迹 + 耗时/调用统计 + JSON 导出 |
| 📈 量化评估 | Agent 级评估框架（任务完成率/根因命中率/响应时效）|
| 🚀 工程质量 | 41 单元测试 + GitHub Actions CI + 配置中心 + 统一日志 |
| 🌐 系统集成 | FastAPI REST 接口 + Docker/K8s 部署方案 |

---

## 🎬 演示场景（真实运行输出）

```
用户：3号机台 EQP-03 这批晶圆良率掉到 88% 了，帮我分析原因并处理。

系统自主完成：
  🧠 记忆召回 → 调取该设备历史诊断经验注入上下文
  🤖 并行调用 数据分析Agent + 知识检索Agent
     ├─ 数据：良率↓7.8%、保养超期15天(红色)、颗粒138超标(176%)、4条未解决报警
     └─ 知识：检索排查SOP + 命中相似历史案例
  🤖 并行调用 质量评审Agent + 行动执行Agent
     ├─ 质量评审：颗粒超标176% > 报废阈值 → 建议报废
     └─ 行动：生成异常处理工单 WO-xxxx
  📋 输出结构化诊断报告
  🔍 防幻觉核对：3/3 关键数字与数据库一致
  🧠 诊断结果沉淀入记忆库，未来自动召回
  📊 运行统计：5 Agent / 14 工具调用 / 4 LLM调用 / 51.6s
```

---

## 🏗️ 系统架构

```
                       用户请求
                          │
                  ┌───────▼───────┐
       记忆召回 ──▶│   协调 Agent   │◀── 记忆沉淀
      (历史经验)   │  规划·并行调度  │   (诊断入库)
                  └───────┬───────┘
     ┌──────┬──────┬──────┴┬──────┬──────┐
 ┌───▼──┐┌──▼───┐┌─▼────┐┌▼─────┐┌▼─────┐┌─▼────┐
 │数据分析││知识检索││质量评审││行动执行││保养规划││数据探索│ 执行层
 │Agent ││Agent ││Agent ││Agent ││Agent ││Agent │ (6个)
 └───┬──┘└──┬───┘└─┬────┘└┬─────┘└┬─────┘└─┬────┘
     │      │      │      │       │        │
 ┌───▼──────▼──────▼──────▼───────▼────────▼─────────────────┐
 │  工具层（13个）：MES查询 / RAG检索 / 工单 / 横向对比 / 统计    │
 │  ＋ 数据探索Agent 经只读SQL沙箱动态生成查询（Text2SQL）       │
 └───┬──────────────────┬──────────────────┬────────────────┘
   ┌────▼────┐      ┌───────▼────────┐   ┌─────▼─────┐
   │factory  │      │ 向量知识库      │   │ memory.db │
   │.db(MES) │      │FAISS/Milvus/LI │   │(Agent记忆) │
   └─────────┘      └────────────────┘   └───────────┘
                          │
              ┌───────────┴───────────┐
         防幻觉核对 · 可观测性 · 量化评估（横切能力）
```

---

## 🔧 技术栈

| 类别 | 技术 |
|------|------|
| 语言 | Python |
| 大模型 | 通义千问 Qwen（OpenAI 兼容，模型路由：协调用plus/执行用flash） |
| Agent框架 | 原生手写 + **LangChain/LangGraph** + **LlamaIndex**（三套对照实现） |
| 向量检索 | FAISS / Milvus / LlamaIndex |
| 数据库 | SQLite（模拟 MES/ERP）+ memory.db（Agent记忆） |
| API服务 | FastAPI + Swagger |
| 部署 | Docker / docker-compose / Kubernetes（deployment/service/hpa） |
| 工程 | pytest + GitHub Actions CI + 配置中心 + 结构化日志 |
| 微调 | 百炼 LoRA（数据集 + 流程就绪） |

---

## 📁 目录结构

```
Multi_Agent/
├── settings.py              # 配置中心（模型/RAG/重试/防幻觉等全局参数）
├── logger_config.py         # 统一结构化日志
├── api_server.py            # FastAPI REST 服务（8接口 + Swagger，含拍照诊断/动态问数）
├── eval_agent.py            # ★ Agent级量化评估框架（任务完成率/响应时效）
│
├── data/
│   ├── build_database.py    # 构建模拟工厂库(8设备/488良率/1240参数/24报警/工单)
│   └── knowledge/           # 企业知识库(SOP/历史案例/工艺/保养规范, 3362行)
├── tools/                   # 工具层（输入校验+异常隔离+错误码）
│   ├── db_tools.py          # MES数据查询(8个工具)
│   ├── kb_tools.py          # RAG检索(FAISS, 置信度过滤+去重+缓存)
│   ├── kb_tools_milvus.py   # RAG检索(Milvus生产级)
│   ├── kb_tools_llamaindex.py # RAG检索(LlamaIndex框架版)
│   ├── sql_sandbox.py       # ★ 只读SQL沙箱(Text2SQL安全底座，多层护栏)
│   └── action_tools.py      # 工单(创建/查询/状态更新/统计)
├── agents/
│   ├── main.py              # ★ 主程序(对话助手 + 并行+记忆+防幻觉+可观测性)
│   ├── coordinator.py       # 协调Agent(遗留基础版)
│   ├── coordinator_langchain.py # ★ LangChain/LangGraph框架版
│   ├── executor_agents.py   # 5个执行Agent(重试+异常隔离)
│   ├── toolsmith.py         # ★ 工具设计师Agent(理解需求→设计只读查询工具→执行)
│   ├── vision_agent.py      # ★ 视觉检测Agent(多模态：看故障照片)
│   ├── memory.py            # ★ 三层记忆管理
│   ├── observability.py     # 可观测性(线程安全)
│   ├── fact_checker.py      # 防幻觉事实校验
│   └── stream_demo.py       # 流式输出示例(响应时效优化)
├── finetune/                # 模型微调
│   ├── generate_dataset.py  # 从知识库自动生成SFT数据集
│   ├── sft_dataset.jsonl    # 百炼微调数据集(35样本)
│   └── use_finetuned.py     # 微调前后对比脚本
├── tests/                   # ★ 41个单元测试(含SQL沙箱安全测试)
├── k8s/                     # Kubernetes部署(deployment/service/hpa)
├── Dockerfile / docker-compose.yml
└── docs/                    # 设计文档(框架对比/性能优化/演进设计)
```

---

## 🚀 快速开始

```bash
pip install -r requirements.txt
export DASHSCOPE_API_KEY=你的key        # Windows: $env:DASHSCOPE_API_KEY="你的key"

cd data && python build_database.py && cd ..   # 构建数据库
python agents/main.py                           # 对话助手(闲聊+按需诊断，默认)
python agents/main.py --once                     # 跑一次固定案例诊断
python agents/main.py --diag                      # 旧版：强制带设备号的交互诊断
python eval_agent.py --quick                     # 运行评估框架
python -m pytest                                 # 运行测试
uvicorn api_server:app --port 8000              # 启动REST API → /docs
```

交互模式三种退出方式：输入 `quit`/`exit`/`q`、闲置自动退出（默认 5 分钟，退出前 30 秒预警一次，期间输入即可继续）、或 `Ctrl+Z`(Windows)/管道结束。闲置秒数可调：

```bash
python agents/main.py --idle 600   # 闲置 10 分钟才退出
python agents/main.py --idle 0     # 禁用闲置退出（永远等 quit）
```
> 默认值在 `settings.py` 的 `idle_timeout_seconds` / `idle_warn_seconds`。注：闲置退出仅作用于演示版 CLI；生产 Web/API 形态由服务端 session TTL 管理。

REST 接口（共 8 个，详见 `/docs`）除诊断/查询/工单外，新增：
- `POST /diagnose_image`：上传故障照片 → 视觉Agent转结构化观察 →（可选）接入诊断流水线
- `POST /ask`：自然语言问数据 → 工具设计师Agent动态生成只读SQL作答

详细部署（Docker/K8s）见 [DEPLOY.md](DEPLOY.md)。

---

## 🔬 一套系统，三种框架实现（对照学习）

| 能力 | 手写原生 | LangChain/LangGraph | LlamaIndex |
|------|---------|--------------------|-----------|
| 协调层 | main.py | coordinator_langchain.py | — |
| RAG | kb_tools.py | — | kb_tools_llamaindex.py |
| 价值 | 完全可控(并行/记忆/可观测) | 标准工作流快速搭建 | RAG组件化 |

设计原则：**先懂底层原理，再用框架**。详见 [docs/框架对比.md](docs/框架对比.md)。

---

## 📊 工程化成果（有实测数据）

### 量化评估（对应 JD：任务完成率、响应时效）
```
任务完成率 100% | 根因命中率 100% | 数字一致率 100% | 平均响应 51.6s
```

### 性能优化（数据驱动）
```
模型路由优化：平均响应 71.7s → 51.6s（↓28%），任务完成率保持100%
流式输出：首字延迟 6.6s（vs 总耗时12s），感知延迟↓45%
详见 docs/性能优化.md
```

### 工程质量
```
41 单元测试通过 + GitHub Actions CI 自动化
```

---

## 📋 与目标岗位（AI Agent 工程师）对应

| 岗位要求 | 本项目实现 |
|---------|-----------|
| Agent架构(规划/记忆/工具) | 协调者-执行者 + 三层记忆 + 13工具 |
| 多智能体协作 | 6 Agent 分工 + 并行调度 |
| RAG + 企业知识库 | 三套RAG + 检索优化 + 评测 |
| 向量库 Milvus/FAISS | 两者 + LlamaIndex 共三套 |
| LangChain/LlamaIndex框架 | 均有对照实现 |
| 大模型API + Prompt工程 | Qwen + 防幻觉 + 模型路由 |
| 微调 LoRA | 数据集 + 流程就绪 |
| MES/ERP集成 | 模拟 + 真实API接入注释 |
| RESTful API / 云原生K8s | FastAPI + Docker/K8s方案 |
| 监控运行效果 | 评估框架 + 可观测性 |
| 优化输出准确性 | 防幻觉双层 + 事后核对 |

---

## 📈 从原型到生产
本项目已实现核心链路 + 工程化能力。生产演进路径（数据/向量/编排/工程层）
详见 [docs/从原型到生产_演进设计.md](docs/从原型到生产_演进设计.md)。
