# -*- coding: utf-8 -*-
"""
执行 Agent 层（专业 Agent）
工程化改造 v2.0：
  - LLM 调用加重试装饰器（指数退避），应对 API 限流/网络抖动
  - 工具调用加异常隔离：单个工具失败不会让整个 Agent 崩溃
  - 配置统一从 settings 读取，日志统一用 logger
  - 数据分析 Agent 扩充工具（横向对比、报警统计）
  - 新增两个专业 Agent：
      质量评审 Agent（quality_review_agent）：评估批次放行/返工/报废
      保养规划 Agent（maintenance_planning_agent）：输出优先级保养计划

Agent 数量：3 → 5
"""

import os
import sys
import json
import time
from functools import wraps

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config  # 自动加载 DASHSCOPE_API_KEY
from settings import settings
from logger_config import get_logger
from openai import OpenAI

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "tools"))
import db_tools
import kb_tools
import action_tools

logger = get_logger(__name__)
client = OpenAI(api_key=os.getenv("DASHSCOPE_API_KEY"),
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")


# ════════════════════════════════════════════════════════════
# 重试装饰器（应对 API 限流/网络抖动）
# ════════════════════════════════════════════════════════════

def retry_on_failure(func):
    """LLM API 调用失败时自动重试，指数退避。"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        last_err = None
        for attempt in range(settings.api_max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_err = e
                wait = settings.api_retry_delay * (2 ** attempt)
                logger.warning(f"{func.__name__} 第{attempt+1}次失败: {e}，{wait}秒后重试")
                time.sleep(wait)
        logger.error(f"{func.__name__} 重试{settings.api_max_retries}次仍失败")
        raise last_err
    return wrapper


@retry_on_failure
def _llm_call(messages, tools_schema):
    """统一的 LLM 调用入口，带重试。
    执行Agent用 worker_model（更快的flash），任务简单不需要最强模型——
    这是响应时效优化的关键：把大量的工具调用轮次放到快模型上。"""
    return client.chat.completions.create(
        model=settings.worker_model, messages=messages,
        tools=tools_schema, tool_choice="auto",
        temperature=settings.temperature)


# ════════════════════════════════════════════════════════════
# 通用 ReAct 循环（带工具异常隔离）
# ════════════════════════════════════════════════════════════

def _run_agent(system_prompt, user_task, tools_schema, tools_map,
               max_steps=None, log=None):
    max_steps = max_steps or settings.max_steps_executor
    # 统一注入防幻觉约束，所有执行Agent共享同一条纪律
    messages = [{"role": "system", "content": system_prompt + settings.anti_hallucination},
                {"role": "user", "content": user_task}]

    for step in range(max_steps):
        try:
            resp = _llm_call(messages, tools_schema)
        except Exception:
            return "（LLM 服务暂时不可用，请稍后重试）"

        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content

        messages.append(msg)
        for tc in msg.tool_calls:
            fn = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                result = f"错误：工具 {fn} 的参数解析失败。"
                logger.error(f"参数解析失败: {tc.function.arguments}")
            else:
                if log is not None:
                    log.append(f"      ↳ 调用工具 {fn}({args})")
                # 工具异常隔离：单个工具失败不影响整个 Agent
                try:
                    result = tools_map[fn](**args)
                except KeyError:
                    result = f"错误：未知工具 {fn}。"
                    logger.error(f"未知工具调用: {fn}")
                except Exception as e:
                    result = f"工具 {fn} 执行出错：{e}"
                    logger.error(f"工具 {fn} 执行异常: {e}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    return "（已达最大步数，未完全收敛）"


# ════════════════════════════════════════════════════════════
# 工具 schema 定义
# ════════════════════════════════════════════════════════════

def _tool(name, desc, props, required):
    """简化工具 schema 定义的小工厂。"""
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}

_EQUIP_PROP = {"equipment_id": {"type": "string", "description": "设备编号，如 EQP-03"}}

# ── 数据分析 Agent 工具集（扩充到6个）──
_DATA_TOOLS = [
    _tool("query_yield_trend", "查询指定设备最近若干天的良率记录及趋势",
          {**_EQUIP_PROP, "days": {"type": "integer", "description": "查询天数，默认7"}}, ["equipment_id"]),
    _tool("query_equipment_maintenance", "查询指定设备的保养状态及超期风险等级",
          _EQUIP_PROP, ["equipment_id"]),
    _tool("query_process_parameters", "查询指定设备最新工艺参数，标出超标项及幅度",
          _EQUIP_PROP, ["equipment_id"]),
    _tool("query_alarms", "查询指定设备未解决的报警记录（按严重程度排序）",
          _EQUIP_PROP, ["equipment_id"]),
    _tool("query_cross_equipment_comparison", "横向对比同类型设备良率，判断是否设备专属问题",
          {"equipment_type": {"type": "string", "description": "设备类型：光刻/刻蚀/薄膜/注入/CMP/清洗/检测"},
           "days": {"type": "integer", "description": "对比天数，默认7"}}, ["equipment_type"]),
    _tool("query_alarm_statistics", "统计指定设备近N天报警频率和类型分布",
          {**_EQUIP_PROP, "days": {"type": "integer", "description": "统计天数，默认30"}}, ["equipment_id"]),
]
_DATA_MAP = {
    "query_yield_trend": db_tools.query_yield_trend,
    "query_equipment_maintenance": db_tools.query_equipment_maintenance,
    "query_process_parameters": db_tools.query_process_parameters,
    "query_alarms": db_tools.query_alarms,
    "query_cross_equipment_comparison": db_tools.query_cross_equipment_comparison,
    "query_alarm_statistics": db_tools.query_alarm_statistics,
}

# ── 知识检索 Agent 工具集 ──
_KB_TOOLS = [
    _tool("search_knowledge_base", "检索企业知识库（排查SOP、历史案例、工艺/保养规范）",
          {"query": {"type": "string", "description": "检索问题"},
           "top_k": {"type": "integer", "description": "返回条数，默认3"}}, ["query"]),
]
_KB_MAP = {"search_knowledge_base": kb_tools.search_knowledge_base}

# ── 行动执行 Agent 工具集（扩充到3个）──
_ACTION_TOOLS = [
    _tool("create_work_order", "为指定设备生成异常处理工单",
          {**_EQUIP_PROP,
           "title": {"type": "string", "description": "工单标题"},
           "description": {"type": "string", "description": "工单详细描述，含根因和处理建议"},
           "priority": {"type": "string", "description": "优先级：高/中/低"}},
          ["equipment_id", "title", "description"]),
    _tool("list_work_orders", "查询工单列表，可按设备和状态过滤",
          {**_EQUIP_PROP, "status": {"type": "string", "description": "状态：待处理/处理中/已完成/已关闭"}}, []),
    _tool("update_work_order_status", "更新工单状态（处理中/已完成/已关闭）",
          {"work_order_id": {"type": "integer", "description": "工单数字编号"},
           "new_status": {"type": "string", "description": "新状态"},
           "remark": {"type": "string", "description": "备注说明"}}, ["work_order_id", "new_status"]),
]
_ACTION_MAP = {
    "create_work_order": action_tools.create_work_order,
    "list_work_orders": action_tools.list_work_orders,
    "update_work_order_status": action_tools.update_work_order_status,
}

# ── 质量评审 Agent 工具集（复用数据+知识工具）──
_QA_TOOLS = [
    _tool("query_process_parameters", "查询设备工艺参数超标情况", _EQUIP_PROP, ["equipment_id"]),
    _tool("query_yield_trend", "查询设备良率趋势",
          {**_EQUIP_PROP, "days": {"type": "integer"}}, ["equipment_id"]),
    _tool("search_knowledge_base", "检索批次处置标准（放行/返工/报废依据）",
          {"query": {"type": "string"}}, ["query"]),
]
_QA_MAP = {
    "query_process_parameters": db_tools.query_process_parameters,
    "query_yield_trend": db_tools.query_yield_trend,
    "search_knowledge_base": kb_tools.search_knowledge_base,
}

# ── 保养规划 Agent 工具集 ──
_PM_TOOLS = [
    _tool("query_upcoming_maintenance", "查询未来N天内保养到期的设备清单",
          {"days_ahead": {"type": "integer", "description": "未来天数，默认7"}}, []),
    _tool("query_equipment_maintenance", "查询单台设备保养状态", _EQUIP_PROP, ["equipment_id"]),
    _tool("query_alarm_statistics", "查询设备报警频率，辅助排序保养优先级",
          {**_EQUIP_PROP, "days": {"type": "integer"}}, ["equipment_id"]),
]
_PM_MAP = {
    "query_upcoming_maintenance": db_tools.query_upcoming_maintenance,
    "query_equipment_maintenance": db_tools.query_equipment_maintenance,
    "query_alarm_statistics": db_tools.query_alarm_statistics,
}


# ════════════════════════════════════════════════════════════
# 五个执行 Agent
# ════════════════════════════════════════════════════════════

def data_analysis_agent(task: str, log=None) -> str:
    """数据分析专家：查询良率、保养、参数、报警，横向对比，汇总客观数据。"""
    sys_p = ("你是制造数据分析专家。针对用户给出的设备和问题，调用数据查询工具，"
             "全面查询良率趋势、保养状态、工艺参数、报警记录；必要时用横向对比工具"
             "判断是否设备专属问题。然后用简洁条理的中文汇总客观数据发现"
             "（只陈述数据事实，不下最终诊断结论）。")
    return _run_agent(sys_p, task, _DATA_TOOLS, _DATA_MAP, log=log)


def knowledge_agent(task: str, log=None) -> str:
    """知识检索专家：检索SOP、历史案例、判级标准。"""
    sys_p = ("你是知识库检索专家。针对用户的问题，调用知识库检索工具，"
             "可多次检索（如分别检索'排查流程'、'相似历史案例'、'判级标准'），"
             "然后用中文总结检索到的关键知识点，特别要点出相似的历史案例及其处理方式。")
    return _run_agent(sys_p, task, _KB_TOOLS, _KB_MAP, log=log)


def action_agent(task: str, log=None) -> str:
    """行动执行专家：生成/查询/更新工单。"""
    sys_p = ("你是行动执行专家。根据用户提供的诊断结论，调用工具生成异常处理工单，"
             "工单描述要包含根因和具体处理建议。生成后用中文确认工单已创建。")
    return _run_agent(sys_p, task, _ACTION_TOOLS, _ACTION_MAP, log=log)


def quality_review_agent(task: str, log=None) -> str:
    """质量评审专家：评估批次应放行/返工/报废。"""
    sys_p = ("你是质量评审专家。根据设备的工艺参数偏差幅度、良率水平，"
             "并检索知识库中的批次处置标准，给出明确的批次处置建议："
             "【放行】【返工】或【报废】，并说明判断依据。"
             "判断原则：参数超规<10%且良率达标可放行；超规10~30%需返工评估；"
             "超规>30%或良率严重不达标建议报废。")
    return _run_agent(sys_p, task, _QA_TOOLS, _QA_MAP, log=log)


def maintenance_planning_agent(task: str, log=None) -> str:
    """保养规划专家：输出优先级排序的保养计划。"""
    sys_p = ("你是设备保养规划专家。调用工具查询即将到期或已超期的设备，"
             "结合各设备的报警频率，按风险优先级（红色>橙色>黄色）输出本周保养计划，"
             "给出每台设备的推荐保养顺序和理由。用中文条理清晰地呈现。")
    return _run_agent(sys_p, task, _PM_TOOLS, _PM_MAP, log=log)


# ════════════════════════════════════════════════════════════
# 本地测试
# ════════════════════════════════════════════════════════════
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not os.getenv("DASHSCOPE_API_KEY"):
        raise SystemExit("请先设置 DASHSCOPE_API_KEY")

    print("【测试 保养规划 Agent（新增）】\n")
    log = []
    out = maintenance_planning_agent("请规划本周的设备保养计划", log=log)
    print("\n".join(log))
    print("\n--- 保养规划结果 ---")
    print(out)
