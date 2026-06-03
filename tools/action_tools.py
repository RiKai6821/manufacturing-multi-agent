# -*- coding: utf-8 -*-
"""
工具模块 3：行动工具（对接模拟 ERP/工单系统）
工程化改造 v2.0：
  - 输入校验：设备编号/优先级/必填字段缺失直接拦截
  - 上下文管理器统一连接，写操作异常自动回滚
  - 工单查询增强：支持按状态过滤、统计
  - 新增工单状态更新工具（闭环：建单→处理→关单）
  - 结构化日志 + settings 配置

对应 JD：「完成 Agent 与 ERP 平台的集成」
"""

import sqlite3
import os
import sys
import datetime
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from settings import settings
from logger_config import get_logger

logger = get_logger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "factory.db")
NOW = datetime.datetime(2026, 6, 2, 10, 30)

VALID_STATUS = {"待处理", "处理中", "已完成", "已关闭"}


@contextmanager
def _get_conn():
    """统一连接管理：写操作自动提交，异常自动回滚。"""
    conn = sqlite3.connect(DB_PATH, timeout=settings.db_timeout)
    try:
        yield conn
        conn.commit()
    except sqlite3.OperationalError as e:
        conn.rollback()
        logger.error(f"数据库写操作失败，已回滚: {e}")
        raise
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════
# 工具1：生成异常处理工单
# ════════════════════════════════════════════════════════════

def create_work_order(equipment_id: str, title: str, description: str,
                      priority: str = "高", created_by: str = "诊断Agent") -> str:
    """为指定设备生成一张异常处理工单，写入工单系统。

    Args:
        equipment_id: 设备编号，须在有效设备列表内
        title:        工单标题（必填，不能为空）
        description:  工单详细描述，应包含根因和处理建议（必填）
        priority:     优先级，高/中/低，默认高
        created_by:   创建人，默认"诊断Agent"
    """
    # ── 输入校验 ──
    if equipment_id not in settings.valid_equipment:
        return f"错误：设备编号 '{equipment_id}' 不存在，无法创建工单。"
    if not title or not title.strip():
        return "错误：工单标题不能为空。"
    if not description or not description.strip():
        return "错误：工单描述不能为空，须包含根因和处理建议。"
    if priority not in settings.valid_priorities:
        return f"错误：优先级 '{priority}' 不合法，须为 高/中/低。"

    logger.info(f"创建工单: {equipment_id} - {title} [{priority}]")
    try:
        with _get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO work_orders "
                "(created_at,equipment_id,title,description,priority,status,created_by) "
                "VALUES (?,?,?,?,?,?,?)",
                (NOW.strftime("%Y-%m-%d %H:%M"), equipment_id, title,
                 description, priority, "待处理", created_by),
            )
            wo_id = cur.lastrowid
    except sqlite3.OperationalError:
        return "工单创建失败（错误码：WO-001），数据库写入异常，请联系管理员。"

    logger.info(f"工单创建成功: WO-{wo_id:04d}")
    return (f"✅ 工单已生成：\n"
            f"  工单号：WO-{wo_id:04d}\n"
            f"  设备：{equipment_id}\n"
            f"  标题：{title}\n"
            f"  优先级：{priority}　状态：待处理\n"
            f"  描述：{description}\n"
            f"  创建时间：{NOW.strftime('%Y-%m-%d %H:%M')}　创建人：{created_by}")


# ════════════════════════════════════════════════════════════
# 工具2：查询工单列表（支持状态过滤）
# ════════════════════════════════════════════════════════════

def list_work_orders(equipment_id: str = None, status: str = None) -> str:
    """查询工单列表，可按设备和状态过滤。

    Args:
        equipment_id: 设备编号，不传则查所有设备
        status:       工单状态（待处理/处理中/已完成/已关闭），不传则查所有状态
    """
    if equipment_id and equipment_id not in settings.valid_equipment:
        return f"错误：设备编号 '{equipment_id}' 不存在。"
    if status and status not in VALID_STATUS:
        return f"错误：状态 '{status}' 不合法，须为 {sorted(VALID_STATUS)}。"

    # 动态拼接查询条件
    conditions, params = [], []
    if equipment_id:
        conditions.append("equipment_id=?")
        params.append(equipment_id)
    if status:
        conditions.append("status=?")
        params.append(status)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    logger.debug(f"查询工单: equipment={equipment_id}, status={status}")
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT id,created_at,equipment_id,title,priority,status "
                f"FROM work_orders{where} ORDER BY id DESC", params
            ).fetchall()
    except sqlite3.OperationalError:
        return "工单查询失败（错误码：WO-002），请联系管理员。"

    if not rows:
        return "未查到符合条件的工单。"

    lines = [f"工单列表（共 {len(rows)} 张）："]
    for wo_id, created, eid, title, pri, st in rows:
        lines.append(f"  WO-{wo_id:04d} | {created} | {eid} | [{pri}] {title} | {st}")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════
# 工具3（新）：更新工单状态（运维闭环）
# ════════════════════════════════════════════════════════════

def update_work_order_status(work_order_id: int, new_status: str,
                             remark: str = "") -> str:
    """更新工单状态，形成 建单→处理中→已完成 的运维闭环。

    Args:
        work_order_id: 工单编号（数字，如 7 表示 WO-0007）
        new_status:    新状态（处理中/已完成/已关闭）
        remark:        备注说明（可选）
    """
    if new_status not in VALID_STATUS:
        return f"错误：状态 '{new_status}' 不合法，须为 {sorted(VALID_STATUS)}。"

    logger.info(f"更新工单 WO-{work_order_id:04d} 状态 → {new_status}")
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT title, status FROM work_orders WHERE id=?",
                (work_order_id,)
            ).fetchone()
            if not row:
                return f"错误：工单 WO-{work_order_id:04d} 不存在。"
            old_status = row[1]
            new_desc_suffix = f"\n[{NOW.strftime('%Y-%m-%d %H:%M')}] 状态变更：{old_status}→{new_status}"
            if remark:
                new_desc_suffix += f"，备注：{remark}"
            conn.execute(
                "UPDATE work_orders SET status=?, "
                "description=description||? WHERE id=?",
                (new_status, new_desc_suffix, work_order_id)
            )
    except sqlite3.OperationalError:
        return "工单状态更新失败（错误码：WO-003），请联系管理员。"

    return (f"✅ 工单 WO-{work_order_id:04d}（{row[0]}）状态已更新："
            f"{old_status} → {new_status}" + (f"，备注：{remark}" if remark else ""))


# ════════════════════════════════════════════════════════════
# 工具4（新）：工单统计
# ════════════════════════════════════════════════════════════

def work_order_statistics() -> str:
    """统计所有工单的状态分布和设备分布，用于运维概览。"""
    logger.debug("统计工单概览")
    try:
        with _get_conn() as conn:
            by_status = conn.execute(
                "SELECT status, COUNT(*) FROM work_orders GROUP BY status"
            ).fetchall()
            by_equip = conn.execute(
                "SELECT equipment_id, COUNT(*) FROM work_orders "
                "GROUP BY equipment_id ORDER BY COUNT(*) DESC LIMIT 5"
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM work_orders").fetchone()[0]
    except sqlite3.OperationalError:
        return "工单统计失败（错误码：WO-004），请联系管理员。"

    if total == 0:
        return "当前系统中没有工单。"

    lines = [f"工单统计概览（共 {total} 张）：", "  按状态分布："]
    for st, cnt in by_status:
        lines.append(f"    {st}：{cnt} 张")
    lines.append("  工单最多的设备（TOP5）：")
    for eid, cnt in by_equip:
        lines.append(f"    {eid}：{cnt} 张")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════
# 本地自测
# ════════════════════════════════════════════════════════════
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("【测试1：创建工单】\n")
    print(create_work_order(
        equipment_id="EQP-03",
        title="3号刻蚀机颗粒污染异常处理",
        description="良率骤降至88%，颗粒计数138超规格上限(50)且保养超期15天，"
                    "疑似反应腔颗粒污染。建议立即停机清洗腔体并执行月度PM。",
        priority="高",
    ))

    print("\n【测试2：查询EQP-03工单】\n")
    print(list_work_orders("EQP-03"))

    print("\n【测试3：更新工单状态】\n")
    # 取最新工单号测试
    with _get_conn() as conn:
        latest_id = conn.execute("SELECT MAX(id) FROM work_orders").fetchone()[0]
    print(update_work_order_status(latest_id, "处理中", "设备工程师已到场"))

    print("\n【测试4：工单统计】\n")
    print(work_order_statistics())

    print("\n【测试5：输入校验】\n")
    print(create_work_order("EQP-99", "测试", "测试"))        # 非法设备
    print(create_work_order("EQP-03", "", "描述"))            # 空标题
    print(create_work_order("EQP-03", "标题", "描述", "紧急")) # 非法优先级
