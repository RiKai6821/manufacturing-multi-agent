# -*- coding: utf-8 -*-
"""
只读 SQL 沙箱（Text2SQL 的安全执行底座）
========================================================================
让"工具设计师Agent"生成的查询能安全地跑在 factory.db 上。
核心护栏（多层防御，任一层失效仍有下一层）：
  1. 语句白名单：只允许单条 SELECT / WITH 查询，禁止增删改、DDL、PRAGMA、ATTACH 等
  2. 只读连接：用 SQLite 的 file:...?mode=ro URI 打开，物理上无法写
  3. 执行超时：progress handler 在超时后中止查询，防慢查询/恶意笛卡尔积
  4. 行数上限：fetchmany 截断，防把整库刷进上下文
  5. schema 自省：把真实表结构喂给模型，避免它瞎猜字段
"""

import os
import re
import sys
import time
import sqlite3
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from settings import settings
from logger_config import get_logger

logger = get_logger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "factory.db")

# 危险关键词（按词边界匹配，大小写不敏感）——命中即拒绝
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|"
    r"attach|detach|pragma|vacuum|reindex|grant|revoke|begin|commit)\b",
    re.IGNORECASE,
)


def _strip_comments(sql: str) -> str:
    """去掉 SQL 注释，防止用注释藏匿危险语句。"""
    sql = re.sub(r"--[^\n]*", " ", sql)        # 行注释
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)  # 块注释
    return sql


def _strip_string_literals(sql: str) -> str:
    """把单引号字符串字面量替换成空格（'' 转义引号一并处理）。
    仅用于"危险词/多语句"扫描，不影响真正执行的原始 SQL。
    目的：避免数据值里的普通词（如报警消息含 'delete'、描述含 'create'）
    或字符串里的分号被误判为写操作/多语句——消除安全护栏的误杀。"""
    return re.sub(r"'(?:[^']|'')*'", " ", sql)


def validate(sql: str) -> tuple[bool, str]:
    """校验 SQL 是否为安全的单条只读查询。返回 (是否合法, 原因)。"""
    if not sql or not sql.strip():
        return False, "SQL 为空。"
    # 扫描前先剥注释、再剥字符串字面量，使关键词/分号检查只针对"真正的语句结构"
    scan = _strip_string_literals(_strip_comments(sql)).strip().rstrip(";").strip()

    # 单语句：去掉尾分号后不应再含分号
    if ";" in scan:
        return False, "只允许单条语句，不得包含多条 SQL。"
    # 必须以 SELECT 或 WITH（CTE）开头
    if not re.match(r"(?is)^\s*(select|with)\b", scan):
        return False, "只允许 SELECT / WITH 查询。"
    # 危险关键词（此时字面量已剥离，命中即为语句结构里的真实写操作/DDL）
    m = _FORBIDDEN.search(scan)
    if m:
        return False, f"检测到禁止的关键词：{m.group(0)}（只读沙箱不允许写操作/DDL）。"
    return True, "ok"


def _connect_readonly() -> sqlite3.Connection:
    """以只读模式打开数据库（物理层禁止写）。"""
    uri = Path(DB_PATH).resolve().as_uri() + "?mode=ro"   # 自动处理空格/反斜杠
    return sqlite3.connect(uri, uri=True, timeout=settings.db_timeout)


def get_schema() -> str:
    """自省数据库结构（表名 + 列名/类型），喂给模型用于生成正确 SQL。"""
    try:
        conn = _connect_readonly()
    except sqlite3.OperationalError as e:
        logger.error(f"打开只读数据库失败: {e}")
        return "（无法读取数据库结构）"
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        lines = []
        for (t,) in tables:
            cols = conn.execute(f"PRAGMA table_info({t})").fetchall()  # 内部自省，非用户输入
            col_desc = ", ".join(f"{c[1]} {c[2]}" for c in cols)
            lines.append(f"表 {t}（{col_desc}）")
        return "\n".join(lines)
    finally:
        conn.close()


def run_select(sql: str, params: dict | None = None,
               max_rows: int = None, timeout: float = None) -> tuple[list, list]:
    """安全执行只读查询。返回 (列名列表, 行列表)。非法或出错时抛 ValueError。"""
    ok, reason = validate(sql)
    if not ok:
        raise ValueError(f"SQL 被沙箱拒绝：{reason}")

    max_rows = max_rows or settings.sql_max_rows
    timeout = timeout or settings.sql_timeout_seconds
    params = params or {}

    conn = _connect_readonly()
    # 超时护栏：每执行若干虚拟机指令回调一次，超时返回非 0 即中止查询
    start = time.time()
    conn.set_progress_handler(lambda: 1 if time.time() - start > timeout else 0, 2000)
    try:
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(max_rows)
        return cols, rows
    except sqlite3.OperationalError as e:
        raise ValueError(f"查询执行失败（可能超时或语法错误）：{e}")
    finally:
        conn.close()


def format_result(cols: list, rows: list) -> str:
    """把查询结果转成简洁文本，供模型/人阅读。"""
    if not rows:
        return "（查询无结果）"
    head = " | ".join(cols)
    body = "\n".join(" | ".join(str(v) for v in r) for r in rows)
    more = f"\n…（仅显示前 {len(rows)} 行）" if len(rows) >= settings.sql_max_rows else ""
    return f"{head}\n{'-' * len(head)}\n{body}{more}"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("=== 数据库结构 ===")
    print(get_schema())
    print("\n=== 合法查询测试 ===")
    cols, rows = run_select(
        "SELECT equipment_id, name, type FROM equipment ORDER BY equipment_id LIMIT 3")
    print(format_result(cols, rows))
    print("\n=== 非法查询拦截测试 ===")
    for bad in ["DELETE FROM equipment",
                "SELECT * FROM equipment; DROP TABLE equipment",
                "UPDATE equipment SET status='x'",
                "PRAGMA table_info(equipment)"]:
        ok, why = validate(bad)
        print(f"  {bad[:40]:<42} → {'放行' if ok else '拦截：' + why}")
