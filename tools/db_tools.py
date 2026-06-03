# -*- coding: utf-8 -*-
"""
工具模块 1：数据查询工具（对接模拟 MES 系统）
工程化改造 v2.0：
  - 上下文管理器统一连接，自动关闭+异常回滚
  - 输入校验：设备编号/参数范围非法直接返回友好报错
  - 结构化日志替代 print
  - 新增工具：横向对比、良率统计、即将到期保养、报警统计
  - 工具返回长度截断，防止塞进 messages 撑爆 context
"""

import sqlite3
import os
import datetime
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from settings import settings
from logger_config import get_logger

logger = get_logger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "factory.db")
TODAY = settings.today   # 系统基准日期，统一从 settings 读取


# ════════════════════════════════════════════════════════════
# 内部工具
# ════════════════════════════════════════════════════════════

@contextmanager
def _get_conn():
    """统一连接管理：自动关闭，异常自动记录。"""
    conn = sqlite3.connect(DB_PATH, timeout=settings.db_timeout)
    try:
        yield conn
        conn.commit()
    except sqlite3.OperationalError as e:
        logger.error(f"数据库操作失败: {e}")
        raise
    finally:
        conn.close()


def _validate_equipment(equipment_id: str) -> str | None:
    """校验设备编号，非法返回错误字符串，合法返回 None。"""
    if equipment_id not in settings.valid_equipment:
        return (f"错误：设备编号 '{equipment_id}' 不存在。"
                f"有效设备：{sorted(settings.valid_equipment)}")
    return None


def _truncate(text: str, max_len: int = None) -> str:
    """截断过长的工具返回值，防止塞爆 context window。"""
    limit = max_len or settings.max_tool_result_length
    if len(text) > limit:
        return text[:limit] + f"\n（内容过长已截断，共{len(text)}字）"
    return text


# ════════════════════════════════════════════════════════════
# 工具1：查某设备近期良率趋势
# ════════════════════════════════════════════════════════════

def query_yield_trend(equipment_id: str, days: int = 7) -> str:
    """查询指定设备最近若干批次的良率记录及趋势。"""
    if err := _validate_equipment(equipment_id):
        return err
    if not 1 <= days <= 90:
        return "错误：查询天数须在 1~90 之间。"

    logger.debug(f"query_yield_trend: {equipment_id}, days={days}")
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT date, lot_id, yield_rate FROM yield_records "
                "WHERE equipment_id=? ORDER BY date DESC LIMIT ?",
                (equipment_id, days),
            ).fetchall()
    except sqlite3.OperationalError:
        return f"数据库查询失败（错误码：DB-001），请联系系统管理员。"

    if not rows:
        return f"未查到设备 {equipment_id} 的良率记录。"

    lines = [f"设备 {equipment_id} 最近 {len(rows)} 批良率（从新到旧）："]
    for date, lot, yr in rows:
        lines.append(f"  {date}  批次{lot}  良率 {yr}%")

    latest, oldest = rows[0][2], rows[-1][2]
    drop = oldest - latest
    if drop >= 5:
        lines.append(f"  → ⚠️ 良率下降约 {drop:.1f} 个百分点，存在明显异常，建议立即排查。")
    elif drop >= 3:
        lines.append(f"  → 良率较前期下降约 {drop:.1f} 个百分点，需关注趋势。")
    else:
        lines.append(f"  → 良率波动在正常范围内（降幅 {drop:.1f} 个百分点）。")

    return _truncate("\n".join(lines))


# ════════════════════════════════════════════════════════════
# 工具2：查设备保养状态
# ════════════════════════════════════════════════════════════

def query_equipment_maintenance(equipment_id: str) -> str:
    """查询指定设备的保养状态，判断是否保养超期及风险等级。"""
    if err := _validate_equipment(equipment_id):
        return err

    logger.debug(f"query_equipment_maintenance: {equipment_id}")
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT name, type, status, last_maintenance, next_maintenance "
                "FROM equipment WHERE equipment_id=?",
                (equipment_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        return "数据库查询失败（错误码：DB-002），请联系系统管理员。"

    if not row:
        return f"未查到设备 {equipment_id}。"

    name, typ, status, last_m, next_m = row
    overdue = (TODAY - datetime.date.fromisoformat(next_m)).days

    s = (f"设备 {equipment_id}（{name}，{typ}工序）当前状态：{status}。"
         f"上次保养 {last_m}，应保养日 {next_m}。")

    if overdue >= 15:
        s += (f" 【红色预警】保养已超期 {overdue} 天，颗粒超标概率约45%，"
              f"建议立即停机执行月度PM。")
    elif overdue > 5:
        s += (f" 【橙色预警】保养已超期 {overdue} 天，须在72小时内安排保养，"
              f"并加密颗粒计数监控。")
    elif overdue > 0:
        s += f" 【黄色预警】保养超期 {overdue} 天，请本周内安排保养。"
    elif overdue > -5:
        s += f" 保养状态正常，但距下次保养仅剩 {-overdue} 天，请提前排期。"
    else:
        s += f" 保养状态正常，距下次保养还有 {-overdue} 天。"

    return s


# ════════════════════════════════════════════════════════════
# 工具3：查工艺参数是否超标
# ════════════════════════════════════════════════════════════

def query_process_parameters(equipment_id: str) -> str:
    """查询指定设备最新工艺参数，重点标出超标项及超标幅度。"""
    if err := _validate_equipment(equipment_id):
        return err

    logger.debug(f"query_process_parameters: {equipment_id}")
    try:
        with _get_conn() as conn:
            # 取每个参数的最新一条记录：用相关子查询匹配该参数的最大日期，
            # 显式且可移植（不依赖 SQLite "MAX()+bare column 取同行" 的特有行为）。
            rows = conn.execute(
                "SELECT p.param_name, p.value, p.spec_lower, p.spec_upper, p.in_spec, p.date "
                "FROM process_parameters p "
                "WHERE p.equipment_id=? AND p.date = ("
                "    SELECT MAX(p2.date) FROM process_parameters p2 "
                "    WHERE p2.equipment_id=p.equipment_id AND p2.param_name=p.param_name) "
                "ORDER BY p.param_name",
                (equipment_id,),
            ).fetchall()
    except sqlite3.OperationalError:
        return "数据库查询失败（错误码：DB-003），请联系系统管理员。"

    if not rows:
        return f"未查到设备 {equipment_id} 的工艺参数。"

    lines = [f"设备 {equipment_id} 工艺参数（每项均为最新一次单点读数，非历史多组）："]
    abnormal = []
    for pname, val, lo, hi, in_spec, date in rows:
        if not in_spec:
            exceed = max(val - hi, lo - val)
            pct = exceed / (hi - lo) * 100
            tag = f"★超标★（超出规格范围 {exceed:.1f}，超出比例 {pct:.0f}%）"
            abnormal.append(f"{pname}={val}（上限{hi}）")
        else:
            tag = "正常"
        lines.append(f"  {pname}: {val}（规格 {lo}~{hi}，测量日期 {date}）→ {tag}")

    if abnormal:
        lines.append(f"\n  ⚠️ 超标参数汇总：{'；'.join(abnormal)}")
        lines.append(f"  建议：立即排查超标原因，颗粒计数超标需优先检查腔体保养状态。")
    else:
        lines.append("\n  ✅ 所有工艺参数均在规格范围内。")

    return _truncate("\n".join(lines))


# ════════════════════════════════════════════════════════════
# 工具4：查设备未解决报警
# ════════════════════════════════════════════════════════════

def query_alarms(equipment_id: str) -> str:
    """查询指定设备未解决的报警记录，按严重程度排序。"""
    if err := _validate_equipment(equipment_id):
        return err

    logger.debug(f"query_alarms: {equipment_id}")
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT datetime, level, alarm_type, message FROM alarms "
                "WHERE equipment_id=? AND resolved=0 "
                "ORDER BY CASE level WHEN '严重' THEN 1 WHEN '警告' THEN 2 ELSE 3 END, datetime DESC",
                (equipment_id,),
            ).fetchall()
    except sqlite3.OperationalError:
        return "数据库查询失败（错误码：DB-004），请联系系统管理员。"

    if not rows:
        return f"设备 {equipment_id} 当前无未解决报警。"

    lines = [f"设备 {equipment_id} 未解决报警（共 {len(rows)} 条，按严重程度排序）："]
    severe = sum(1 for r in rows if r[1] == "严重")
    if severe:
        lines.append(f"  ⚠️ 其中严重级别 {severe} 条，需立即处置！")
    for dt, level, atype, msg in rows:
        lines.append(f"  [{level}] {dt}  {atype}：{msg}")

    return _truncate("\n".join(lines))


# ════════════════════════════════════════════════════════════
# 工具5（新）：横向对比同类型设备良率
# ════════════════════════════════════════════════════════════

def query_cross_equipment_comparison(equipment_type: str, days: int = 7) -> str:
    """横向对比同类型所有设备的近期平均良率，判断问题是否设备专属。
    equipment_type 可为：光刻 / 刻蚀 / 薄膜 / 注入 / CMP / 清洗 / 检测
    """
    valid_types = {"光刻", "刻蚀", "薄膜", "注入", "CMP", "清洗", "检测"}
    if equipment_type not in valid_types:
        return f"错误：设备类型 '{equipment_type}' 不合法。有效类型：{sorted(valid_types)}"
    if not 1 <= days <= 30:
        return "错误：对比天数须在 1~30 之间。"

    logger.debug(f"query_cross_equipment_comparison: type={equipment_type}, days={days}")
    cutoff = (TODAY - datetime.timedelta(days=days)).isoformat()
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT e.equipment_id, e.name, AVG(y.yield_rate), MIN(y.yield_rate), COUNT(*) "
                "FROM equipment e JOIN yield_records y ON e.equipment_id = y.equipment_id "
                "WHERE e.type=? AND y.date >= ? "
                "GROUP BY e.equipment_id ORDER BY AVG(y.yield_rate) ASC",
                (equipment_type, cutoff),
            ).fetchall()
    except sqlite3.OperationalError:
        return "数据库查询失败（错误码：DB-005），请联系系统管理员。"

    if not rows:
        return f"未查到 {equipment_type} 类型设备的良率数据。"

    lines = [f"{equipment_type}类设备近{days}天良率横向对比："]
    avg_all = sum(r[2] for r in rows) / len(rows)
    for eid, name, avg_yr, min_yr, cnt in rows:
        flag = " ← ⚠️ 低于全组平均" if avg_yr < avg_all - 2 else ""
        lines.append(f"  {eid}（{name}）：平均{avg_yr:.1f}%  最低{min_yr:.1f}%  共{cnt}批{flag}")
    lines.append(f"\n  全组平均良率：{avg_all:.1f}%")
    lines.append(f"  诊断提示：若仅一台设备低于平均值2%以上，为设备专属问题；若多台同时偏低，需排查来料或公用工程。")

    return _truncate("\n".join(lines))


# ════════════════════════════════════════════════════════════
# 工具6（新）：查询即将到期的保养计划
# ════════════════════════════════════════════════════════════

def query_upcoming_maintenance(days_ahead: int = 7) -> str:
    """查询未来N天内即将到期或已超期的设备，用于预防性维护规划。"""
    if not 1 <= days_ahead <= 30:
        return "错误：查询天数须在 1~30 之间。"

    logger.debug(f"query_upcoming_maintenance: days_ahead={days_ahead}")
    deadline = (TODAY + datetime.timedelta(days=days_ahead)).isoformat()

    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT equipment_id, name, type, next_maintenance "
                "FROM equipment WHERE next_maintenance <= ? ORDER BY next_maintenance ASC",
                (deadline,),
            ).fetchall()
    except sqlite3.OperationalError:
        return "数据库查询失败（错误码：DB-006），请联系系统管理员。"

    if not rows:
        return f"未来 {days_ahead} 天内无设备保养到期。"

    lines = [f"未来 {days_ahead} 天内保养到期设备（共 {len(rows)} 台）："]
    for eid, name, typ, next_m in rows:
        overdue = (TODAY - datetime.date.fromisoformat(next_m)).days
        if overdue > 0:
            status = f"【已超期 {overdue} 天】⚠️"
        elif overdue > -3:
            status = f"【{-overdue} 天后到期】🔴"
        else:
            status = f"【{-overdue} 天后到期】🟡"
        lines.append(f"  {eid}（{name}，{typ}工序）应保养日：{next_m}  {status}")

    return _truncate("\n".join(lines))


# ════════════════════════════════════════════════════════════
# 工具7（新）：报警统计分析
# ════════════════════════════════════════════════════════════

def query_alarm_statistics(equipment_id: str, days: int = 30) -> str:
    """统计指定设备近N天的报警频率和类型分布，识别高频问题。"""
    if err := _validate_equipment(equipment_id):
        return err
    if not 1 <= days <= 180:
        return "错误：统计天数须在 1~180 之间。"

    logger.debug(f"query_alarm_statistics: {equipment_id}, days={days}")
    cutoff = (TODAY - datetime.timedelta(days=days)).isoformat()

    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT alarm_type, level, COUNT(*) as cnt "
                "FROM alarms WHERE equipment_id=? AND datetime >= ? "
                "GROUP BY alarm_type, level ORDER BY cnt DESC",
                (equipment_id, cutoff),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM alarms WHERE equipment_id=? AND datetime >= ?",
                (equipment_id, cutoff),
            ).fetchone()[0]
    except sqlite3.OperationalError:
        return "数据库查询失败（错误码：DB-007），请联系系统管理员。"

    if not rows:
        return f"设备 {equipment_id} 近 {days} 天无报警记录。"

    lines = [f"设备 {equipment_id} 近 {days} 天报警统计（共 {total} 条）："]
    for atype, level, cnt in rows:
        pct = cnt / total * 100
        lines.append(f"  [{level}] {atype}：{cnt} 次（占比 {pct:.0f}%）")

    top = rows[0]
    lines.append(f"\n  高频报警：{top[0]}（{top[1]}级），共 {top[2]} 次，建议重点排查。")

    return _truncate("\n".join(lines))


# ════════════════════════════════════════════════════════════
# 工具8：设备清单（权威名册，回答"有哪些设备"用，杜绝编造）
# ════════════════════════════════════════════════════════════

def list_equipment() -> str:
    """返回所有设备的真实清单（编号/名称/工序类型），来源于数据库 equipment 表。"""
    logger.debug("list_equipment")
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT equipment_id, name, type, status FROM equipment ORDER BY equipment_id"
            ).fetchall()
    except sqlite3.OperationalError:
        return "设备清单查询失败（错误码：DB-008），请联系系统管理员。"

    if not rows:
        return "未查到任何设备。"

    lines = [f"全部设备（共 {len(rows)} 台）："]
    for eid, name, typ, status in rows:
        lines.append(f"  {eid}：{name}（{typ}工序，{status}）")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════
# 本地自测
# ════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("=" * 60, "\n【测试 EQP-03（问题设备）】\n", "=" * 60)
    print(query_yield_trend("EQP-03", days=7))
    print()
    print(query_equipment_maintenance("EQP-03"))
    print()
    print(query_process_parameters("EQP-03"))
    print()
    print(query_alarms("EQP-03"))
    print()

    print("=" * 60, "\n【新工具测试】\n", "=" * 60)
    print(query_cross_equipment_comparison("刻蚀", days=7))
    print()
    print(query_upcoming_maintenance(days_ahead=10))
    print()
    print(query_alarm_statistics("EQP-03", days=30))
    print()

    print("=" * 60, "\n【输入校验测试】\n", "=" * 60)
    print(query_yield_trend("EQP-99"))        # 非法设备号
    print(query_yield_trend("EQP-03", 999))   # 非法天数
