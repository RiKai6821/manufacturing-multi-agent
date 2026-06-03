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
import re
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

def _role(m):
    """兼容 dict 消息与 OpenAI 消息对象，统一取 role。"""
    return m["role"] if isinstance(m, dict) else getattr(m, "role", None)


def _compress_history(messages):
    """保留 system + 首条user + 最近N轮交互，中间过长部分用摘要替换。
    协调者通常3~5轮就结束，这里是安全兜底，应对扩展为多轮交互的场景。

    关键：tool 消息必须紧跟其 assistant(tool_calls)。压缩后若尾部以"孤儿
    tool 消息"开头，会触发 LLM API 400，故向后推进起点直到非 tool 消息，
    保证 tool_calls 与其结果不被拆散。"""
    keep = settings.max_tool_results_in_history * 2
    if len(messages) <= keep + 2:
        return messages   # 不长，无需压缩

    head = messages[:2]              # system + 首条user
    start = len(messages) - keep     # 尾部起点

    # 边界对齐：跳过开头的孤儿 tool 消息，避免切断 tool_call 配对
    while start < len(messages) and _role(messages[start]) == "tool":
        start += 1

    tail = messages[start:]
    omitted = start - len(head)
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


# 对话助手用的 system 提示：可闲聊，也能按需调用专家Agent（区别于上面强制走诊断流程的协调者提示）
_CHAT_SYSTEM = """你是制造企业的智能助手「小诊」。你既能正常聊天答疑，也能在需要时诊断设备异常。

可用设备：EQP-01 ~ EQP-08（覆盖光刻/刻蚀/薄膜/注入/CMP/清洗/检测等工序）。
你可调用五个专家Agent作为工具：
- 数据分析Agent：查良率/保养/工艺参数/报警，可横向对比同类设备
- 知识检索Agent：检索排查SOP、历史案例、判级标准
- 行动执行Agent：生成/查询/更新异常处理工单
- 质量评审Agent：评估批次放行/返工/报废
- 保养规划Agent：输出优先级保养计划

行为准则：
1. 用户寒暄、问你能做什么、或与设备数据无关的问题 —— 直接用自然语言回答，【不要】调用任何工具。
2. 用户询问某设备的良率/保养/参数/报警/工单/批次处置/保养计划 —— 调用对应专家Agent取真实数据后再回答；
   若多个查询互不依赖，可在同一轮并行调用多个Agent以提速。
3. 形成明确根因和处理建议、且用户希望落实时，才调用行动执行Agent生成工单。
4. 始终用中文，简洁清晰；做设备诊断时给出"关键数据发现 + 根因 + 处理建议"。
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


# ════════════════════════════════════════════════════════════
# 对话式助手（闲聊 + 按需诊断）
# ════════════════════════════════════════════════════════════

def chat_session():
    """对话式助手「小诊」：既能闲聊/答疑，也能在用户问到设备时按需调用专家Agent诊断。
    不强制设备编号——由模型自行判断是否要查数据。整段会话共享一条消息历史，天然记得上下文。
    输入 quit/exit/q 退出。"""
    print("=" * 70)
    print("🤖 制造智能助手「小诊」—— 对话模式")
    print("   可以直接聊：问我能做什么、或问某台设备(EQP-01~08)的良率/保养/报警…")
    print("   例：你能做什么？ / EQP-03良率掉到88%帮我看看 / 那EQP-05呢？")
    print("   输入 quit 退出")
    print("=" * 70)

    messages = [{"role": "system", "content": _CHAT_SYSTEM}]
    session = SessionMemory()   # 仅用于给本会话标个ID，便于记忆沉淀溯源

    while True:
        user_input = input("\n你：").strip()
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("小诊：再见，有设备问题随时找我！")
            break

        # 检测设备号：仅在真正做诊断时用于记忆召回 + 事后校验（闲聊不受影响）
        m = re.search(r"EQP-\d+", user_input.upper())
        equipment_id = m.group() if m else None

        # 提到具体设备时，把历史经验作为参考补进本轮用户消息（不强制模型采用）
        content = user_input
        if equipment_id:
            recall = diagnosis_memory.build_recall_prompt(equipment_id, user_input)
            if recall:
                content += ("\n\n（系统补充·过往诊断经验，仅供参考，须以本次实际数据为准）\n" + recall)
        messages.append({"role": "user", "content": content})

        # 每轮一个 tracer：把五个专家Agent包成工具，交给模型自主决定要不要调
        tracer = Tracer(verbose=settings.verbose)
        coord_map = {
            "call_data_agent": lambda task: data_agent(task, tracer),
            "call_knowledge_agent": lambda task: kb_agent(task, tracer),
            "call_action_agent": lambda task: act_agent(task, tracer),
            "call_quality_agent": lambda task: qa_agent(task, tracer),
            "call_maintenance_agent": lambda task: pm_agent(task, tracer),
        }

        used_tools = False
        reply = "（已达最大处理步数，未能给出完整答复）"
        for _ in range(settings.max_steps_coordinator):
            messages = _compress_history(messages)
            try:
                resp = _llm_call(messages, _COORD_TOOLS)
            except Exception:
                reply = "（抱歉，AI 服务暂时不可用，请稍后再试）"
                break

            msg = resp.choices[0].message
            if not msg.tool_calls:        # 模型直接作答（闲聊或已综合完毕）
                reply = msg.content or "（无内容）"
                break

            used_tools = True
            messages.append(msg)
            calls = [(tc.id, tc.function.name, json.loads(tc.function.arguments))
                     for tc in msg.tool_calls]

            if len(calls) > 1:            # 并行调用
                tracer.log("INFO", "助手", f"并行调用 {len(calls)} 个Agent",
                           "、".join(c[1] for c in calls))
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(calls)) as pool:
                    futures = {pool.submit(coord_map[name], args["task"]): tc_id
                               for tc_id, name, args in calls}
                    results = {}
                    for fut in concurrent.futures.as_completed(futures):
                        results[futures[fut]] = fut.result()
                for tc_id, name, args in calls:
                    messages.append({"role": "tool", "tool_call_id": tc_id,
                                     "content": results[tc_id]})
            else:                         # 单个调用
                tc_id, name, args = calls[0]
                result = coord_map[name](args["task"])
                messages.append({"role": "tool", "tool_call_id": tc_id, "content": result})

        # 把最终回复落入历史，供下一轮追问
        messages.append({"role": "assistant", "content": reply})
        print(f"\n小诊：{reply}")

        # 仅当真正做了诊断（用了工具）才显示统计；锁定了设备再做事后校验 + 记忆沉淀
        if used_tools:
            print(tracer.summary())
            if equipment_id:
                print("\n" + verify_report(equipment_id, reply))
                rec_id = diagnosis_memory.save(equipment_id, user_input, reply,
                                               session_id=session.session_id)
                print(f"🧠 已存入记忆库（REC-{rec_id:04d}），未来问到该设备会自动召回。")


if __name__ == "__main__":
    if not os.getenv("DASHSCOPE_API_KEY"):
        raise SystemExit("请先设置 DASHSCOPE_API_KEY")

    if "--once" in sys.argv:
        # 单次诊断示例（CI / 演示用）
        diagnose("3号机台 EQP-03 这批晶圆良率掉到 88% 了，帮我分析原因并处理。", "EQP-03")
    elif "--diag" in sys.argv:
        # 旧版：强制带设备号的交互式诊断
        interactive_session()
    else:
        # 默认：对话式助手（闲聊 + 按需诊断）
        chat_session()
