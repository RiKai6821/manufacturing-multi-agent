# -*- coding: utf-8 -*-
"""
========================================================================
⚠️ 遗留版本（DEPRECATED）—— 仅作阶段3教学/对照保留，请勿用于生产。
   生产入口为 agents/main.py 的 diagnose()，它在此基础上补齐了：
   LLM 重试、并行调用、History 压缩、记忆召回、防幻觉事后校验、可观测性。
   本文件无重试/记忆/校验，功能已被 main.py 覆盖。
========================================================================
阶段 3 - 协调 Agent（系统大脑）+ 主程序
========================================================================
协调 Agent 采用"协调者-执行者"架构：把三个执行 Agent 封装成它可调用的
三个"高级工具"，由它自主规划调用顺序，完成端到端的诊断与处理。

协作流程（由协调 Agent 自主规划，非写死）：
  用户提问 → 协调Agent规划 →
    1. 调 数据分析Agent  → 摸清良率/保养/参数/报警
    2. 调 知识检索Agent  → 找排查流程 + 历史相似案例
    3. 综合判断根因
    4. 调 行动执行Agent  → 生成异常处理工单
  → 汇总成诊断报告返回用户

可观测性：全程打印"哪个Agent被调用、内部调了哪些工具"，让协作过程透明可追溯。

运行：python coordinator.py
"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config  # 自动加载 DASHSCOPE_API_KEY
import json
from openai import OpenAI
from settings import settings

sys.path.append(os.path.dirname(__file__))
import executor_agents as ex

client = OpenAI(api_key=os.getenv("DASHSCOPE_API_KEY"),
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
MODEL = settings.chat_model   # 统一走 settings（协调者用 qwen-plus）

# 全局执行日志（可观测性）
EXECUTION_LOG = []


def _log(msg):
    EXECUTION_LOG.append(msg)
    print(msg)


# ── 把三个执行 Agent 包装成协调者可调用的"高级工具" ──
def _call_data_agent(task: str) -> str:
    _log(f"\n  🔧 [协调者] → 派给【数据分析Agent】：{task}")
    sub_log = []
    result = ex.data_analysis_agent(task, log=sub_log)
    for l in sub_log:
        _log(l)
    _log(f"  ✅ [数据分析Agent] 返回汇总")
    return result


def _call_knowledge_agent(task: str) -> str:
    _log(f"\n  🔧 [协调者] → 派给【知识检索Agent】：{task}")
    sub_log = []
    result = ex.knowledge_agent(task, log=sub_log)
    for l in sub_log:
        _log(l)
    _log(f"  ✅ [知识检索Agent] 返回汇总")
    return result


def _call_action_agent(task: str) -> str:
    _log(f"\n  🔧 [协调者] → 派给【行动执行Agent】：{task}")
    sub_log = []
    result = ex.action_agent(task, log=sub_log)
    for l in sub_log:
        _log(l)
    _log(f"  ✅ [行动执行Agent] 返回结果")
    return result


_COORD_TOOLS = [
    {"type": "function", "function": {"name": "call_data_agent",
        "description": "调用数据分析Agent，查询某设备的良率、保养、工艺参数、报警等生产数据。输入应说明要分析哪个设备及关注点。",
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "交给数据分析Agent的具体任务描述"}},
            "required": ["task"]}}},
    {"type": "function", "function": {"name": "call_knowledge_agent",
        "description": "调用知识检索Agent，从企业知识库检索排查流程、历史相似案例、判级标准等知识。",
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "交给知识检索Agent的具体检索任务"}},
            "required": ["task"]}}},
    {"type": "function", "function": {"name": "call_action_agent",
        "description": "调用行动执行Agent，根据诊断结论生成异常处理工单。仅在已形成明确根因和处理建议后调用。",
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "交给行动Agent的任务，需包含设备、根因、处理建议"}},
            "required": ["task"]}}},
]
_COORD_MAP = {
    "call_data_agent": _call_data_agent,
    "call_knowledge_agent": _call_knowledge_agent,
    "call_action_agent": _call_action_agent,
}

_COORD_SYSTEM = """你是制造企业异常诊断的总协调者（主管Agent）。你手下有三个专家Agent可供调用：
- 数据分析Agent：查询设备的良率、保养、工艺参数、报警等生产数据
- 知识检索Agent：从企业知识库检索排查流程SOP、历史相似案例、判级标准
- 行动执行Agent：根据诊断结论生成异常处理工单

处理用户的设备异常请求时，请按合理顺序规划：
1. 先调用数据分析Agent，摸清该设备的客观数据状况；
2. 再调用知识检索Agent，获取标准排查流程和历史相似案例作为佐证；
3. 综合数据与知识，判断最可能的根本原因；
4. 形成明确根因和处理建议后，调用行动执行Agent生成工单；
5. 最后输出一份结构清晰的中文诊断报告，包含：问题概述、关键数据发现、根因分析（结合历史案例）、处理建议、已生成的工单。
请一步步推进，每次只在必要时调用合适的专家。"""


def diagnose(user_request: str) -> str:
    EXECUTION_LOG.clear()
    _log("=" * 70)
    _log(f"📥 用户请求：{user_request}")
    _log("=" * 70)
    _log("\n🧠 [协调者] 开始规划任务…")

    messages = [{"role": "system", "content": _COORD_SYSTEM},
                {"role": "user", "content": user_request}]
    for step in range(8):
        resp = client.chat.completions.create(
            model=MODEL, messages=messages, tools=_COORD_TOOLS, tool_choice="auto")
        msg = resp.choices[0].message
        if not msg.tool_calls:
            _log("\n" + "=" * 70)
            _log("📋 协调者最终诊断报告：")
            _log("=" * 70)
            return msg.content
        messages.append(msg)
        for tc in msg.tool_calls:
            fn = tc.function.name
            args = json.loads(tc.function.arguments)
            result = _COORD_MAP[fn](**args)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    return "（协调流程已达最大步数）"


if __name__ == "__main__":
    if not os.getenv("DASHSCOPE_API_KEY"):
        raise SystemExit("请先设置 DASHSCOPE_API_KEY")
    report = diagnose("3号机台 EQP-03 这批晶圆良率掉到 88% 了，帮我分析原因并处理。")
    print(report)
