# -*- coding: utf-8 -*-
"""
全局配置中心
所有可调参数统一在这里管理，避免散落在各文件中。
"""
import datetime
from dataclasses import dataclass, field
from typing import Set


@dataclass
class Settings:
    # ── 系统基准日期（演示数据固定在此日）──
    # 统一各模块的"今天"，避免 today/now 散落硬编码导致漂移。
    # 接入真实生产时，改为 datetime.date.today() / datetime.datetime.now() 即可。
    today: datetime.date = datetime.date(2026, 6, 2)
    now: datetime.datetime = datetime.datetime(2026, 6, 2, 10, 30)

    # ── 模型配置（模型路由：不同角色用不同模型，平衡速度/成本/质量）──
    chat_model: str = "qwen-plus"     # 协调者：综合判断和最终报告，用强模型
    worker_model: str = "qwen-flash"  # 执行Agent：任务较简单（查数据/检索/建单），用快模型（模型路由：强弱搭配省时省钱）
    embed_model: str = "text-embedding-v4"
    embed_dim: int = 1024
    temperature: float = 0.0          # 0 = 稳定可复现，调试首选

    # ── 多模态 / 动态工具 ──
    vision_model: str = "qwen-vl-max"   # 视觉理解模型（看故障照片）
    sql_max_rows: int = 200             # Text2SQL 单次返回最大行数（防刷屏）
    sql_timeout_seconds: int = 5        # SQL 执行超时秒数（防慢查询）

    # ── Agent 配置 ──
    max_steps_executor: int = 6       # 执行Agent最大循环步数
    max_steps_coordinator: int = 8    # 协调Agent最大循环步数
    executor_timeout: int = 120       # 单个执行Agent超时秒数

    # ── RAG 配置 ──
    chunk_size: int = 200
    chunk_overlap: int = 50
    top_k: int = 3
    min_score: float = 0.5            # 低于此相关度的检索结果过滤掉

    # ── 数据库配置 ──
    db_timeout: int = 10              # SQLite 连接超时秒数
    valid_equipment: Set[str] = field(default_factory=lambda: {
        "EQP-01", "EQP-02", "EQP-03", "EQP-04",
        "EQP-05", "EQP-06", "EQP-07", "EQP-08"
    })
    valid_priorities: Set[str] = field(default_factory=lambda: {"高", "中", "低"})

    # ── 重试配置 ──
    api_max_retries: int = 3
    api_retry_delay: int = 2          # 每次重试间隔秒数

    # ── 可观测性配置 ──
    verbose: bool = True              # 是否实时打印日志
    trace_export_path: str = "last_trace.json"

    # ── History 管理 ──
    max_tool_results_in_history: int = 4   # 保留最近N条工具结果，防止context溢出
    max_tool_result_length: int = 800      # 单条工具结果最大字符数，超出截断

    # ── 防幻觉通用约束（追加到每个 Agent 的 system prompt）──
    anti_hallucination: str = (
        "\n\n【重要纪律】你只能基于工具实际返回的数据作答，严禁编造或'补全'任何"
        "数据库/知识库中没有的具体数字、设备参数、SOP编号、人名、化学名词或时间。"
        "若某项信息工具未提供，就如实说明'该信息未获取'，绝不臆测。引用历史案例时，"
        "只能复述检索到的原文内容，不得添加原文没有的细节。"
        "\n特别注意：工艺参数查询返回的是【每个参数的最新单点读数】，不是历史多组数据——"
        "严禁臆造'最近N组'、数值区间(如567~570)、标准差/均值等统计量，也不得编造测量日期；"
        "工具结果里没写的日期/时间一律不要出现在回答中。")


# 全局单例，所有模块 import 这个对象
settings = Settings()
