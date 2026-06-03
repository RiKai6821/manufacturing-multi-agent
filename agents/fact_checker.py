# -*- coding: utf-8 -*-
"""
========================================================================
阶段 4 - 防幻觉校验器（Fact Checker）
========================================================================
不信任模型的"自觉"，而是用代码去查证：把诊断报告里出现的关键事实
（良率、颗粒数、保养超期天数等）与数据库真实值比对，自动标记不一致。

这是防幻觉的"最后一道防线"——即使模型编造了数字，这里也能抓出来。
对应 JD：「优化 Agent 输出准确性」。

设计思路：
- 从数据库取出该设备的"权威事实"（ground truth）
- 在报告文本里用正则找出模型提到的对应数字
- 逐项比对，一致✅、不一致⚠️、报告未提及➖
"""

import sqlite3
import os
import sys
import re
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from settings import settings

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "factory.db")


def get_ground_truth(equipment_id: str) -> dict:
    """从数据库取出该设备的权威事实，作为核对基准。"""
    conn = sqlite3.connect(DB_PATH)
    gt = {}
    try:
        # 最新批次良率
        row = conn.execute("SELECT yield_rate FROM yield_records WHERE equipment_id=? ORDER BY date DESC LIMIT 1",
                           (equipment_id,)).fetchone()
        if row:
            gt["latest_yield"] = row[0]
        # 颗粒计数（最新）
        row = conn.execute("SELECT value FROM process_parameters WHERE equipment_id=? AND param_name LIKE '颗粒%' ORDER BY date DESC LIMIT 1",
                           (equipment_id,)).fetchone()
        if row:
            gt["particle_count"] = row[0]
        # 保养超期天数
        row = conn.execute("SELECT next_maintenance FROM equipment WHERE equipment_id=?", (equipment_id,)).fetchone()
        if row:
            overdue = (settings.today - datetime.date.fromisoformat(row[0])).days
            gt["maintenance_overdue_days"] = overdue
    finally:
        conn.close()
    return gt


def _find_numbers(text):
    """提取文本中所有数字（含小数）。
    先剔除日期、工单号/设备号/SOP编号等标识符里的数字，
    避免这些与真实指标无关的数字造成巧合"命中"误判。"""
    cleaned = re.sub(r"\d{4}-\d{2}-\d{2}", " ", text)              # 日期 2026-06-02
    cleaned = re.sub(r"[A-Za-z]+-[A-Za-z]*-?\d+", " ", cleaned)    # WO-0011 / EQP-03 / SOP-ETCH-007
    return [float(x) for x in re.findall(r"\d+\.?\d*", cleaned)]


def verify_report(equipment_id: str, report: str) -> str:
    """核对报告中的关键数字与数据库真实值。返回核对结果文本。"""
    gt = get_ground_truth(equipment_id)
    nums_in_report = set(_find_numbers(report))

    checks = []
    def chk(label, truth, unit=""):
        # 判断报告里有没有出现这个真实值（容忍 0.1 误差）
        hit = any(abs(n - truth) < 0.15 for n in nums_in_report)
        mark = "✅ 一致" if hit else "⚠️ 报告中未出现该真实值（可能被改写或遗漏）"
        checks.append(f"  · {label}：数据库真实值 = {truth}{unit} → {mark}")

    lines = [f"🔍 防幻觉事实核对（设备 {equipment_id}）", "─" * 60]
    if "latest_yield" in gt:
        chk("最新批次良率", gt["latest_yield"], "%")
    if "particle_count" in gt:
        chk("最新颗粒计数", gt["particle_count"], "个/片")
    if "maintenance_overdue_days" in gt:
        chk("保养超期天数", gt["maintenance_overdue_days"], "天")
    lines.extend(checks)

    # 额外提示：报告若出现了明显不在事实集里的"具体编号/人名"，提醒人工复核
    suspicious = []
    for pat, name in [(r"SOP-[A-Z]+-\d+", "SOP编号"), (r"O型密封圈|氟化铝|HF/HNO", "具体化学/部件名词")]:
        if re.search(pat, report):
            suspicious.append(name)
    if suspicious:
        lines.append("─" * 60)
        lines.append(f"  ⚠️ 报告中出现了知识库未必包含的具体细节（{', '.join(suspicious)}），")
        lines.append(f"     这些可能是模型补充的常识，也可能是幻觉，建议人工复核。")

    lines.append("─" * 60)
    n_ok = sum(1 for c in checks if "✅" in c)
    lines.append(f"  核对结论：{n_ok}/{len(checks)} 项关键数字与数据库一致。")
    return "\n".join(lines)


if __name__ == "__main__":
    # 自测：用一段含错误数字的假报告测核对器
    fake = "EQP-03良率降至88%，颗粒计数132个/片，保养超期12天，参照SOP-ETCH-007处理。"
    print("真实事实：", get_ground_truth("EQP-03"))
    print()
    print(verify_report("EQP-03", fake))
