# -*- coding: utf-8 -*-
"""
升级版主程序：可信 + 高效的多智能体诊断系统
工程化改造 v2.0（在防幻觉/可观测性/事后校验基础上再加）：
  1. 并行执行：协调者在同一轮请求多个 Agent 时，用线程池并行跑（数据/知识可同时进行）
  2. History 压缩：对话历史过长时自动压缩，防止 context 溢出
  3. LLM 调用重试：协调层 API 调用也带重试
  4. 5 个专业 Agent：数据分析/知识检索/行动执行/质量评审/保养规划
  5. 配置统一 settings，日志统一 logger

运行：python main.py
"""

import os
import sys
import json
import time
import concurrent.futures
from functools import wraps

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows GBK 终端兼容

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config  # 自动加载 DASHSCOPE_API_KEY
from settings import settings
from logger_config import get_logger
from openai import OpenAI

sys.path.append(os.path.dirname(__file__))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "tools"))
from observability import Tracer
from fact_checker import verify_report
from memory import DiagnosisMemory, SessionMemory
import executor_agents as ex

logger = get_logger(__name__)

# 诊断记忆（长期，跨诊断持久化）—— 模块级单例
diagnosis_memory = DiagnosisMemory()
client = OpenAI(api_key=os.getenv("DASHSCOPE_API_KEY"),
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")

# 防幻觉约束统一从 settings 读取（执行Agent和协调者共用同一条）
ANTI_HALLUCINATION = settings.anti_hallucination


# ════════════════════════════════════════════════════════════
# LLM 调用（带重试）
# ════════════════════════════════════════════════════════════

def retry_on_failure(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        last_err = None
        for attempt in range(settings.api_max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_err = e
                wait = settings.api_retry_delay * (2 ** attempt)
                logger.warning(f"协调者LLM第{attempt+1}次失败: {e}，{wait}秒后重试")
                time.sleep(wait)
        raise last_err
    return wrapper


@retry_on_failure
def _llm_call(messages, tools):
    return client.chat.completions.create(
        model=settings.chat_model, messages=messages,
        tools=tools, tool_choice="auto", temperature=settings.temperature)


# ════════════════════════════════════════════════════════════
# History 压缩：防止 context 溢出
# ════════════════════════════════════════════════════════════

def _compress_history(messages):
    """保留 system + 首条user + 最近N轮交互，中间过长的工具结果用摘要替换。
    协调者通常3~5轮就结束，这里是安全兜底，应对扩展为多轮交互的场景。"""
    if len(messages) <= settings.max_tool_results_in_history * 2 + 2:
        return messages   # 不长，无需压缩

    head = messages[:2]                                    # system + 首条user
    tail = messages[-(settings.max_tool_results_in_history * 2):]  # 最近N轮
    omitted = len(messages) - len(head) - len(tail)
    if omitted > 0:
        summary = {"role": "user",
                   "content": f"（系统提示：为节省上下文，已省略中间 {omitted} 条历史消息）"}
        logger.info(f"History 压缩：省略 {omitted} 条中间消息")
        return head + [summary] + tail
    return messages


# ════════════════════════════════════════════════════════════
# 执行 Agent 包装（带 tracer）
# ════════════════════════════════════════════════════════════

def _wrap(agent_func, agent_name, task, tracer):
    """统一包装执行 Agent：记录轨迹（防幻觉约束已在 executor 层统一注入）。"""
    tracer.log("AGENT", agent_name, "接受任务", task[:40] + "…")
    sub_log = []
    result = agent_func(task, log=sub_log)
    for l in sub_log:
        tracer.log("TOOL", agent_name, l.strip())
    tracer.log("RESULT", agent_name, "完成")
    return result


def data_agent(task, tracer):
    return _wrap(ex.data_analysis_agent, "数据分析Agent", task, tracer)

def kb_agent(task, tracer):
    return _wrap(ex.knowledge_agent, "知识检索Agent", task, tracer)

def act_agent(task, tracer):
    return _wrap(ex.action_agent, "行动执行Agent", task, tracer)

def qa_agent(task, tracer):
    return _wrap(ex.quality_review_agent, "质量评审Agent", task, tracer)

def pm_agent(task, tracer):
    return _wrap(ex.maintenance_planning_agent, "保养规划Agent", task, tracer)


# ════════════════════════════════════════════════════════════
# 协调 Agent
# ════════════════════════════════════════════════════════════

def _coord_tool(name, desc):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object",
                       "properties": {"task": {"type": "string", "description": "交给该Agent的任务描述"}},
                       "required": ["task"]}}}

_COORD_TOOLS = [
    _coord_tool("call_data_agent", "调用数据分析Agent查询设备良率/保养/参数/报警，支持横向对比"),
    _coord_tool("call_knowledge_agent", "调用知识检索Agent检索排查流程、历史案例、判级标准"),
    _coord_tool("call_action_agent", "调用行动执行Agent生成/查询/更新工单（须在形成明确根因后）"),
    _coord_tool("call_quality_agent", "调用质量评审Agent评估批次放行/返工/报废"),
    _coord_tool("call_maintenance_agent", "调用保养规划Agent输出优先级保养计划"),
]

_COORD_SYSTEM = """你是制造企业异常诊断的总协调者。你可调用五个专家Agent：
- 数据分析Agent：查良率/保养/参数/报警，可横向对比同类设备
- 知识检索Agent：检索排查SOP、历史案例、判级标准
- 行动执行Agent：生成/查询/更新异常处理工单
- 质量评审Agent：评估批次放行/返工/报废
- 保养规划Agent：输出优先级保养计划

处理设备异常请求时按顺序规划：1.先查数据 2.再检索知识与历史案例 3.综合判断根因
4.必要时评估批次处置 5.生成工单 6.输出中文诊断报告
（含：问题概述、关键数据发现、根因分析（结合历史案例）、处理建议、已生成工单）。
为提升效率，第1、2步若无依赖关系，可在同一轮同时调用数据分析Agent和知识检索Agent。
""" + ANTI_HALLUCINATION


def diagnose(user_request, equipment_id, session_memory=None):
    tracer = Tracer(verbose=settings.verbose)
    print("=" * 70)
    print(f"📥 用户请求：{user_request}")
    print("=" * 70 + "\n")
    tracer.log("INFO", "协调者", "开始规划任务")

    # ── 记忆召回：诊断前自动调取该设备/相似症状的历史经验 ──
    recall = diagnosis_memory.build_recall_prompt(equipment_id, user_request)
    if recall:
        tracer.log("INFO", "记忆系统", "召回历史经验", f"{equipment_id} 命中历史记录")
        print(recall + "\n")
    else:
        tracer.log("INFO", "记忆系统", "无历史记录", f"{equipment_id} 首次诊断")

    coord_map = {
        "call_data_agent": lambda task: data_agent(task, tracer),
        "call_knowledge_agent": lambda task: kb_agent(task, tracer),
        "call_action_agent": lambda task: act_agent(task, tracer),
        "call_quality_agent": lambda task: qa_agent(task, tracer),
        "call_maintenance_agent": lambda task: pm_agent(task, tracer),
    }

    # system 提示 = 基础协调提示 + 记忆召回（若有）
    system_content = _COORD_SYSTEM + ("\n\n" + recall if recall else "")
    messages = [{"role": "system", "content": system_content}]
    # 会话记忆：注入多轮历史上下文（支持追问）
    if session_memory:
        messages += session_memory.get_context()
    messages.append({"role": "user", "content": user_request})
    report = "（未生成报告）"

    for _ in range(settings.max_steps_coordinator):
        tracer.log("LLM", "协调者", "规划下一步")
        messages = _compress_history(messages)
        try:
            resp = _llm_call(messages, _COORD_TOOLS)
        except Exception:
            report = "（协调者LLM服务不可用，诊断中止）"
            break

        msg = resp.choices[0].message
        if not msg.tool_calls:
            report = msg.content
            break

        messages.append(msg)

        # ── 并行执行：同一轮的多个 Agent 调用并发跑 ──
        calls = [(tc.id, tc.function.name, json.loads(tc.function.arguments))
                 for tc in msg.tool_calls]

        if len(calls) > 1:
            tracer.log("INFO", "协调者", f"并行调用 {len(calls)} 个Agent",
                       "、".join(c[1] for c in calls))
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(calls)) as pool:
                futures = {pool.submit(coord_map[name], args["task"]): tc_id
                           for tc_id, name, args in calls}
                results = {}
                for fut in concurrent.futures.as_completed(futures):
                    results[futures[fut]] = fut.result()
            # 按原顺序写回 messages
            for tc_id, name, args in calls:
                messages.append({"role": "tool", "tool_call_id": tc_id,
                                 "content": results[tc_id]})
        else:
            tc_id, name, args = calls[0]
            result = coord_map[name](args["task"])
            messages.append({"role": "tool", "tool_call_id": tc_id, "content": result})

    # ── 输出报告 ──
    print("\n" + "=" * 70)
    print("📋 诊断报告")
    print("=" * 70)
    print(report)

    # ── 可观测性统计 ──
    print(tracer.summary())

    # ── 防幻觉事后校验 ──
    print("\n" + "=" * 70)
    print(verify_report(equipment_id, report))
    print("=" * 70)

    # ── 记忆沉淀：本次诊断结果存入长期记忆库，供未来召回 ──
    sid = session_memory.session_id if session_memory else ""
    rec_id = diagnosis_memory.save(equipment_id, user_request, report, session_id=sid)
    tracer.log("RESULT", "记忆系统", "诊断已沉淀", f"REC-{rec_id:04d}")
    print(f"\n🧠 本次诊断已存入记忆库（REC-{rec_id:04d}），未来诊断该设备时将自动召回。")
    # 会话记忆：记录本轮，供下一轮追问使用
    if session_memory:
        session_memory.add_user(user_request)
        session_memory.add_assistant(report)

    tracer.export_json(os.path.join(os.path.dirname(__file__), settings.trace_export_path))
    return report


# ════════════════════════════════════════════════════════════
# 多轮交互式诊断（会话记忆）
# ════════════════════════════════════════════════════════════

def interactive_session():
    """交互式多轮诊断：用户可连续追问，系统记得会话上下文。
    输入 quit 退出。每轮需指明设备编号（格式：设备号 问题）。"""
    sess = SessionMemory()
    print("=" * 70)
    print("🤖 多智能体诊断系统 - 交互式会话模式")
    print(f"   会话ID：{sess.session_id}")
    print("   用法：输入'设备号 问题'，如：EQP-03 良率掉到88%了")
    print("   输入 quit 退出")
    print("=" * 70)

    while True:
        user_input = input("\n你：").strip()
        if user_input.lower() in ("quit", "exit", "q"):
            print("会话结束。")
            break
        # 解析设备号（取第一个 EQP-xx）
        import re
        m = re.search(r"EQP-\d+", user_input.upper())
        if not m:
            print("请在问题中包含设备编号（如 EQP-03）。")
            continue
        equipment_id = m.group()
        diagnose(user_input, equipment_id, session_memory=sess)


if __name__ == "__main__":
    if not os.getenv("DASHSCOPE_API_KEY"):
        raise SystemExit("请先设置 DASHSCOPE_API_KEY")

    # 单次诊断（默认）
    diagnose("3号机台 EQP-03 这批晶圆良率掉到 88% 了，帮我分析原因并处理。", "EQP-03")

    # 多轮交互模式（取消下面注释启用）：
    # interactive_session()
