# -*- coding: utf-8 -*-
"""
协调层的 LangChain / LangGraph 实现（对照手写版 coordinator.py / main.py）
========================================================================
同样的"协调者-执行者"多智能体架构，这次用 LangGraph 框架实现，
目的是对比：手写循环 vs 框架封装，到底框架替我们做了什么。

【手写版(main.py) ←→ 框架版(本文件) 对照】
  手写的 _run_agent for循环         →  create_react_agent() 自带ReAct循环
  手写 tools_schema JSON            →  @tool 装饰器自动从函数签名+docstring生成
  手写 messages.append 维护历史      →  框架内部 state["messages"] 自动维护
  手写 if msg.tool_calls 路由        →  框架内部条件边自动路由
  手写协调者调度执行Agent            →  执行Agent包成@tool给supervisor调用

  结论：框架把"控制流"全包了，你只需定义工具和提示词。
  但手写版能精确控制并行/重试/记忆/可观测性——框架要做到这些需额外学其扩展点。
  懂底层再用框架，出问题知道去哪找——这就是两个都写一遍的意义。

运行：python coordinator_langchain.py
"""

import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config  # 自动加载 DASHSCOPE_API_KEY
from settings import settings

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "tools"))
import db_tools
import kb_tools
import action_tools

from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain.agents import create_agent   # LangChain v1.0 新位置（替代旧的 create_react_agent）

# ── 大模型：LangChain 的 ChatOpenAI，base_url 指向百炼 ──
model = ChatOpenAI(
    model=settings.chat_model,
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    temperature=settings.temperature,
)


# ════════════════════════════════════════════════════════════
# 第 1 层：底层工具用 @tool 包装（复用现有 db_tools/kb_tools/action_tools）
# @tool 自动从函数签名+docstring 生成给模型看的 schema，省掉手写JSON
# ════════════════════════════════════════════════════════════

@tool
def query_yield_trend(equipment_id: str, days: int = 7) -> str:
    """查询指定设备最近若干天的良率记录及趋势。equipment_id 如 EQP-03。"""
    return db_tools.query_yield_trend(equipment_id, days)

@tool
def query_equipment_maintenance(equipment_id: str) -> str:
    """查询指定设备的保养状态及超期风险等级。"""
    return db_tools.query_equipment_maintenance(equipment_id)

@tool
def query_process_parameters(equipment_id: str) -> str:
    """查询指定设备最新工艺参数，标出超标项。"""
    return db_tools.query_process_parameters(equipment_id)

@tool
def query_alarms(equipment_id: str) -> str:
    """查询指定设备未解决的报警记录。"""
    return db_tools.query_alarms(equipment_id)

@tool
def search_knowledge_base(query: str) -> str:
    """检索企业知识库（排查SOP、历史案例、工艺/保养规范）。"""
    return kb_tools.search_knowledge_base(query)

@tool
def create_work_order(equipment_id: str, title: str, description: str, priority: str = "高") -> str:
    """为指定设备生成异常处理工单，description 须含根因和处理建议。"""
    return action_tools.create_work_order(equipment_id, title, description, priority)


# ════════════════════════════════════════════════════════════
# 第 2 层：用 create_react_agent 构建执行 Agent（替代手写的 _run_agent 循环）
# 一行替代手写几十行的 调模型→执行工具→喂回 循环
# ════════════════════════════════════════════════════════════

data_worker = create_agent(
    model, tools=[query_yield_trend, query_equipment_maintenance,
                  query_process_parameters, query_alarms],
    system_prompt="你是数据分析专家，查询良率/保养/参数/报警，汇总客观数据事实，不下最终诊断。")

kb_worker = create_agent(
    model, tools=[search_knowledge_base],
    system_prompt="你是知识检索专家，检索排查流程、历史案例、判级标准并总结要点。")

action_worker = create_agent(
    model, tools=[create_work_order],
    system_prompt="你是行动执行专家，根据诊断结论生成异常处理工单。")


def _run_worker(worker, task: str) -> str:
    """调用一个执行Agent，取最终回复文本。"""
    result = worker.invoke({"messages": [("user", task)]})
    return result["messages"][-1].content


# ════════════════════════════════════════════════════════════
# 第 3 层：执行 Agent 包成 supervisor 的工具（协调者-执行者）
# ════════════════════════════════════════════════════════════

@tool
def call_data_agent(task: str) -> str:
    """调用数据分析Agent查询设备良率/保养/参数/报警数据。"""
    print(f"\n  🔧 [Supervisor] → 数据分析Agent：{task[:40]}…")
    return _run_worker(data_worker, task)

@tool
def call_knowledge_agent(task: str) -> str:
    """调用知识检索Agent检索SOP、历史案例、判级标准。"""
    print(f"\n  🔧 [Supervisor] → 知识检索Agent：{task[:40]}…")
    return _run_worker(kb_worker, task)

@tool
def call_action_agent(task: str) -> str:
    """调用行动执行Agent生成工单（须在形成明确根因后）。"""
    print(f"\n  🔧 [Supervisor] → 行动执行Agent：{task[:40]}…")
    return _run_worker(action_worker, task)


SUPERVISOR_PROMPT = (
    "你是制造企业异常诊断的总协调者。可调用三个专家：数据分析Agent、知识检索Agent、行动执行Agent。"
    "按顺序：1.先查数据 2.再检索知识与历史案例 3.综合判断根因 4.生成工单 "
    "5.输出中文诊断报告（问题概述、关键数据、根因分析、处理建议、已生成工单）。"
    "只依据工具返回的真实数据作答，不编造。")

supervisor = create_agent(
    model, tools=[call_data_agent, call_knowledge_agent, call_action_agent],
    system_prompt=SUPERVISOR_PROMPT)


def diagnose_langchain(user_request: str) -> str:
    """LangGraph 版诊断入口。对比 main.py 的手写版 diagnose()。"""
    print("=" * 70)
    print(f"📥 用户请求：{user_request}")
    print("=" * 70)
    result = supervisor.invoke({"messages": [("user", user_request)]})
    report = result["messages"][-1].content
    print("\n" + "=" * 70)
    print("📋 诊断报告（LangGraph 版）")
    print("=" * 70)
    print(report)
    return report


if __name__ == "__main__":
    if not os.getenv("DASHSCOPE_API_KEY"):
        raise SystemExit("请先设置 DASHSCOPE_API_KEY")
    diagnose_langchain("3号机台 EQP-03 这批晶圆良率掉到 88% 了，帮我分析原因并处理。")
