# -*- coding: utf-8 -*-
"""
工具设计师 Agent（ToolSmith）—— 自行设计工具并映射给执行器
========================================================================
这是一个"更高级的 Agent"：当用户的数据需求没有现成工具覆盖时，它会：

  1. 理解需求 + 读数据库真实结构（schema）
  2. 【设计一个工具】：自动产出工具规格（名字/描述/参数）+ 一条只读 SQL
  3. 【映射给执行器】：把工具注册进动态工具表（DYNAMIC_REGISTRY），
     由"SQL执行Agent"负责安全执行（只读沙箱）
  4. 用真实查询结果生成中文回答

安全取向（重要）：
  本实现走 Text2SQL —— 模型"设计的工具"本质是受约束的只读 SELECT，
  经 sql_sandbox 多层护栏校验后执行；【不执行模型生成的任意 Python 代码】，
  避免动态 exec 带来的任意代码执行风险。

  动态生成 vs 手写工具：动态工具灵活、覆盖长尾问题，但正确性/可控性弱于
  手写专用工具；高可靠场景仍应优先用 tools/db_tools.py 里写死的专用工具，
  动态工具用于兜住临时/长尾需求。

运行（需 DASHSCOPE_API_KEY）：python toolsmith.py
"""

import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config  # 自动加载 DASHSCOPE_API_KEY
from settings import settings
from logger_config import get_logger
from openai import OpenAI

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "tools"))
import sql_sandbox

logger = get_logger(__name__)
client = OpenAI(api_key=os.getenv("DASHSCOPE_API_KEY"),
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")

# 动态工具注册表：name -> {"spec": 工具规格, "sql": 查询模板}
# 这就是"SQL执行Agent"持有的工具集——设计师设计完即注册到这里
DYNAMIC_REGISTRY: dict[str, dict] = {}


# ════════════════════════════════════════════════════════════
# 第 1 步：设计师 —— 理解需求，设计工具规格 + SQL
# ════════════════════════════════════════════════════════════

_DESIGN_SYSTEM = """你是"工具设计师Agent"。用户有一个关于工厂数据库的查询需求，
但现有工具未覆盖。请基于下面的数据库真实结构，为该需求设计一个【只读查询工具】。

数据库结构：
{schema}

只输出一个 JSON（不要任何多余文字、不要代码块标记），字段如下：
{{
  "name": "工具英文名(snake_case)",
  "description": "工具的中文用途说明",
  "parameters": [{{"name":"参数名","type":"string/integer","description":"说明"}}],
  "sql": "一条 SQLite SELECT 语句，用 :参数名 做占位符",
  "call_args": {{"参数名": "为满足本次需求填入的具体值"}}
}}

硬性要求：
- sql 必须以 SELECT 或 WITH 开头，且只能读取上面出现过的表和字段；
- 严禁出现 insert/update/delete/drop/alter/create/pragma 等写操作或 DDL；
- 合理使用 WHERE/GROUP BY/ORDER BY/LIMIT；无需参数时 parameters 用空数组、call_args 用空对象。"""


def _strip_fences(text: str) -> str:
    """去掉模型可能加的 ```json ... ``` 包裹。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return text.strip()


def _extract_json(text: str) -> dict:
    """从模型输出稳健解析 JSON：容忍代码块包裹或前后多余文字。"""
    t = _strip_fences(text)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        # 退而求其次：截取第一个配平的 {...} 块
        start = t.find("{")
        if start != -1:
            depth = 0
            for i in range(start, len(t)):
                if t[i] == "{":
                    depth += 1
                elif t[i] == "}":
                    depth -= 1
                    if depth == 0:
                        return json.loads(t[start:i + 1])
        raise


def design_tool(need: str, log=None) -> dict:
    """设计师：理解需求 → 产出工具规格 + SQL（dict）。"""
    schema = sql_sandbox.get_schema()
    msgs = [{"role": "system", "content": _DESIGN_SYSTEM.format(schema=schema)},
            {"role": "user", "content": f"需求：{need}"}]
    try:
        resp = client.chat.completions.create(
            model=settings.chat_model, messages=msgs,
            temperature=settings.temperature,
            response_format={"type": "json_object"})
    except Exception as e:
        # 个别模型/网关不支持 response_format，回退普通调用（靠强提示 + 稳健解析兜底）
        logger.warning(f"response_format 不被支持，回退普通调用: {e}")
        resp = client.chat.completions.create(
            model=settings.chat_model, messages=msgs,
            temperature=settings.temperature)
    spec = _extract_json(resp.choices[0].message.content)
    if log is not None:
        log.append(f"      ↳ 设计工具 {spec.get('name')}：{spec.get('sql')}")
    return spec


# ════════════════════════════════════════════════════════════
# 第 2 步：把工具注册（映射）给"SQL执行Agent"
# ════════════════════════════════════════════════════════════

def register_tool(spec: dict) -> str:
    """校验并注册动态工具到执行器。返回工具名；非法则抛 ValueError。"""
    name = spec.get("name")
    sql = spec.get("sql", "")
    if not name or not sql:
        raise ValueError("工具规格缺少 name 或 sql。")
    ok, reason = sql_sandbox.validate(sql)   # 设计期就把不安全的 SQL 挡掉
    if not ok:
        raise ValueError(f"设计的 SQL 未通过安全校验：{reason}")
    DYNAMIC_REGISTRY[name] = {"spec": spec, "sql": sql}
    logger.info(f"动态工具已注册: {name}")
    return name


# ════════════════════════════════════════════════════════════
# 第 3 步：SQL执行Agent —— 用只读沙箱执行已注册的动态工具
# ════════════════════════════════════════════════════════════

def run_dynamic_tool(name: str, args: dict | None = None, log=None) -> tuple[list, list]:
    """执行器：调用一个已注册的动态工具。返回 (列名, 行)。"""
    if name not in DYNAMIC_REGISTRY:
        raise ValueError(f"未注册的动态工具：{name}")
    sql = DYNAMIC_REGISTRY[name]["sql"]
    if log is not None:
        log.append(f"      ↳ SQL执行Agent 运行 {name}({args or {}})")
    return sql_sandbox.run_select(sql, args or {})


# ════════════════════════════════════════════════════════════
# 第 4 步：把查询结果转成中文回答
# ════════════════════════════════════════════════════════════

_ANSWER_SYSTEM = ("你是数据分析助手。下面是为回答用户问题而执行的真实数据库查询结果，"
                  "请用简洁中文直接回答用户问题。" + settings.anti_hallucination)


def _summarize(need: str, spec: dict, cols: list, rows: list) -> str:
    table = sql_sandbox.format_result(cols, rows)
    user = (f"用户问题：{need}\n\n"
            f"为此动态设计并执行的查询（{spec.get('name')}）结果如下：\n{table}\n\n"
            f"请基于以上真实结果回答。")
    resp = client.chat.completions.create(
        model=settings.chat_model,
        messages=[{"role": "system", "content": _ANSWER_SYSTEM},
                  {"role": "user", "content": user}],
        temperature=settings.temperature)
    return resp.choices[0].message.content


# ════════════════════════════════════════════════════════════
# 编排：理解需求 → 设计工具 → 注册 → 执行 → 作答
# ════════════════════════════════════════════════════════════

def serve(need: str, log=None) -> str:
    """一站式：把一个未被现有工具覆盖的数据需求，端到端处理掉。"""
    try:
        spec = design_tool(need, log=log)
        name = register_tool(spec)
        cols, rows = run_dynamic_tool(name, spec.get("call_args"), log=log)
    except ValueError as e:
        return f"（无法安全完成该查询：{e}）"
    except Exception as e:
        logger.error(f"toolsmith 处理失败: {e}")
        return f"（处理出错：{e}）"
    return _summarize(need, spec, cols, rows)


def toolsmith_agent(task: str, log=None) -> str:
    """可被协调者当成执行Agent调用的包装（与 executor_agents 里的 *_agent 同形）。"""
    return serve(task, log=log)


# ════════════════════════════════════════════════════════════
# 本地演示
# ════════════════════════════════════════════════════════════
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not os.getenv("DASHSCOPE_API_KEY"):
        raise SystemExit("请先设置 DASHSCOPE_API_KEY")

    for need in [
        "现在有哪些设备？分别是什么工序？",          # 现有工具没覆盖的"列设备"
        "哪台设备未解决的报警最多？列出前三。",        # 跨表聚合，没有现成工具
        "EQP-03 最近30天平均良率是多少？",            # 动态聚合
    ]:
        print("=" * 70)
        print(f"需求：{need}")
        log = []
        ans = serve(need, log=log)
        print("\n".join(log))
        print("回答：", ans, "\n")
