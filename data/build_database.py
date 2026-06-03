# -*- coding: utf-8 -*-
"""
========================================================================
阶段 1：构建模拟制造数据库（多智能体系统的数据地基）
========================================================================
用 SQLite 构建一个仿真的半导体工厂数据库，让后续的"数据查询 Agent"
能查询真实的数据库，而不是读硬编码的列表——这是工程真实度的关键。

数据库设计（5 张表，模拟 MES/ERP 系统）：
  yield_records      良率记录（哪个机台、哪批、良率多少）
  equipment          设备主数据与状态（含保养到期信息）
  process_parameters 工艺参数记录（是否在规格范围内）
  alarms             设备报警记录
  work_orders        异常处理工单（行动 Agent 会往这里写）

★ 埋入的"根因线索"（让 Agent 能真的查出问题）：
  EQP-03 保养已超期 → 反应腔颗粒污染 → 颗粒数超标报警 → 工艺参数漂移
  → 最新批次良率从 ~95% 掉到 88%
  其它设备数据正常，作为对比基线。

扩充内容（v2.0）
  - 设备从5台扩充到8台（新增CMP机、湿法清洗机、检测机）
  - 良率记录从14天扩充到60天
  - 工艺参数从3个扩充到每台设备5~6个，覆盖各工序特征参数
  - 报警记录从4条扩充到25条，覆盖多种报警类型和设备
  - 新增历史工单记录（6条已完成工单），体现系统完整闭环

运行：python build_database.py   （无需联网、无需 API Key）
"""

import sys
import sqlite3
import random
import datetime
import os

sys.stdout.reconfigure(encoding='utf-8', errors='replace')  # Windows GBK 终端兼容

# 复用全局基准日期，避免与 settings.today 漂移（settings 无 API Key 依赖，可安全导入）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from settings import settings

random.seed(42)
DB_PATH = os.path.join(os.path.dirname(__file__), "factory.db")
TODAY = settings.today   # 系统基准日期，统一从 settings 读取

# ── 8台设备（EQP-03 是当前问题设备）──
EQUIPMENT = [
    ("EQP-01", "1号光刻机",      "光刻", "运行中"),
    ("EQP-02", "2号刻蚀机",      "刻蚀", "运行中"),
    ("EQP-03", "3号刻蚀机",      "刻蚀", "运行中"),   # ← 当前问题设备
    ("EQP-04", "4号薄膜沉积机",  "薄膜", "运行中"),
    ("EQP-05", "5号离子注入机",  "注入", "运行中"),
    ("EQP-06", "6号CMP抛光机",   "CMP",  "运行中"),
    ("EQP-07", "7号湿法清洗机",  "清洗", "运行中"),
    ("EQP-08", "8号缺陷检测机",  "检测", "运行中"),
]

# ── 各设备工艺参数定义 (param_name, spec_lower, spec_upper) ──
PARAMS = {
    "EQP-01": [                              # 光刻机
        ("曝光量(mJ/cm²)",    38.0, 42.0),
        ("套刻精度X(nm)",      0.0,  5.0),
        ("套刻精度Y(nm)",      0.0,  5.0),
        ("光刻胶厚度(nm)",   290.0,310.0),
        ("洁净室温度(度C)",    22.5, 23.5),
    ],
    "EQP-02": [                              # 2号刻蚀机（正常）
        ("刻蚀温度(度C)",       60.0, 70.0),
        ("刻蚀气压(mTorr)",   10.0, 30.0),
        ("颗粒计数(个/片)",    0.0, 50.0),
        ("RF功率上极(W)",    500.0,600.0),
        ("刻蚀速率(Å/min)", 950.0,1050.0),
    ],
    "EQP-03": [                              # 3号刻蚀机（问题设备）
        ("刻蚀温度(度C)",       60.0, 70.0),
        ("刻蚀气压(mTorr)",   10.0, 30.0),
        ("颗粒计数(个/片)",    0.0, 50.0),
        ("RF功率上极(W)",    500.0,600.0),
        ("刻蚀速率(Å/min)", 950.0,1050.0),
    ],
    "EQP-04": [                              # 薄膜沉积机
        ("沉积温度(度C)",      380.0,420.0),
        ("腔体真空度(1e-7Torr)", 1.0, 5.0),
        ("薄膜均匀性(%)",      0.0,  2.0),
        ("沉积速率(nm/min)",  48.0, 52.0),
        ("靶材使用量(%)",      0.0, 70.0),
    ],
    "EQP-05": [                              # 离子注入机
        ("注入能量(keV)",     98.0,102.0),
        ("束线真空度(1e-6Torr)", 0.1, 1.0),
        ("剂量均匀性(%)",      0.0,  1.5),
        ("束流稳定性(%)",      0.0,  2.0),
        ("法拉第杯清洁天数",   0.0, 30.0),
    ],
    "EQP-06": [                              # CMP抛光机
        ("去除量(nm)",       117.0,123.0),
        ("抛光均匀性(%)",      0.0,  5.0),
        ("抛光压力(kPa)",     28.0, 32.0),
        ("研磨盘转速(rpm)",   88.0, 92.0),
        ("研磨液温度(度C)",    19.0, 21.0),
    ],
    "EQP-07": [                              # 湿法清洗机
        ("SC1溶液温度(度C)",   70.0, 80.0),
        ("清洗后颗粒(个/片)",  0.0, 30.0),
        ("去离子水电阻率(MΩ·cm)", 17.0, 18.2),
        ("SC1溶液pH值",        9.5, 11.0),
        ("清洗时间(min)",      9.5, 10.5),
    ],
    "EQP-08": [                              # 缺陷检测机
        ("检测灵敏度(nm)",    28.0, 32.0),
        ("扫描速度(cm²/s)",   4.5,  5.5),
        ("激光功率(mW)",      98.0,102.0),
        ("检测台平整度(μm)",   0.0,  0.5),
        ("环境振动(μm/s²)",   0.0,  2.0),
    ],
}


def build():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # ════════════════════════════════════════════════════
    # 建表
    # ════════════════════════════════════════════════════
    c.executescript("""
    CREATE TABLE equipment (
        equipment_id TEXT PRIMARY KEY,
        name TEXT, type TEXT, status TEXT,
        last_maintenance DATE, next_maintenance DATE
    );
    CREATE TABLE yield_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date DATE, equipment_id TEXT, lot_id TEXT,
        wafer_count INTEGER, good_count INTEGER, yield_rate REAL
    );
    CREATE TABLE process_parameters (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date DATE, equipment_id TEXT, param_name TEXT,
        value REAL, spec_lower REAL, spec_upper REAL, in_spec INTEGER
    );
    CREATE TABLE alarms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        datetime TEXT, equipment_id TEXT, alarm_type TEXT,
        level TEXT, message TEXT, resolved INTEGER
    );
    CREATE TABLE work_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT, equipment_id TEXT, title TEXT,
        description TEXT, priority TEXT, status TEXT, created_by TEXT
    );
    """)

    # ════════════════════════════════════════════════════
    # 设备主数据
    # ════════════════════════════════════════════════════
    maintenance_cfg = {
        # eid: (last_days_ago, next_days_from_today)  正数=未来，负数=已超期
        "EQP-01": (18,  12),   # 正常
        "EQP-02": ( 8,  22),   # 正常
        "EQP-03": (45, -15),   # ★ 保养超期15天（根因线索）
        "EQP-04": (12,  18),   # 正常
        "EQP-05": (25,   5),   # 正常，但只剩5天
        "EQP-06": ( 5,  25),   # 正常
        "EQP-07": (20,  10),   # 正常
        "EQP-08": ( 3,  27),   # 正常
    }
    for eid, name, typ, status in EQUIPMENT:
        last_ago, next_delta = maintenance_cfg[eid]
        last_m = TODAY - datetime.timedelta(days=last_ago)
        next_m = TODAY + datetime.timedelta(days=next_delta)
        c.execute("INSERT INTO equipment VALUES (?,?,?,?,?,?)",
                  (eid, name, typ, status, last_m.isoformat(), next_m.isoformat()))

    # ════════════════════════════════════════════════════
    # 良率记录：60天，每台每天一批
    # ════════════════════════════════════════════════════
    # 各设备正常良率范围
    normal_yield = {
        "EQP-01": (96.5, 98.5),   # 光刻机，良率高
        "EQP-02": (95.0, 97.5),   # 刻蚀机（正常）
        "EQP-03": (94.5, 97.0),   # 刻蚀机（问题设备，正常时同EQP-02）
        "EQP-04": (94.0, 96.5),   # 薄膜机
        "EQP-05": (95.5, 97.5),   # 注入机
        "EQP-06": (93.0, 96.0),   # CMP机
        "EQP-07": (97.0, 99.0),   # 清洗机，几乎不影响良率
        "EQP-08": (98.0, 99.5),   # 检测机，只做检测
    }

    for d in range(60, -1, -1):
        date = (TODAY - datetime.timedelta(days=d)).isoformat()
        for eid, *_ in EQUIPMENT:
            wafers = 25
            lo, hi = normal_yield[eid]

            # EQP-03 良率下降曲线（根因线索）
            if eid == "EQP-03":
                if d == 0:
                    yr = 88.0                              # 今天骤降
                elif d == 1:
                    yr = round(random.uniform(91.0, 92.5), 1)  # 昨天开始下滑
                elif d == 2:
                    yr = round(random.uniform(92.5, 93.5), 1)  # 前天苗头
                elif d <= 5:
                    yr = round(random.uniform(93.5, 94.5), 1)  # 近5天轻微下滑
                else:
                    yr = round(random.uniform(lo, hi), 1)       # 正常
            # EQP-02 在30天前有一次短暂波动（历史事件，已恢复）
            elif eid == "EQP-02" and 31 <= d <= 33:
                yr = round(random.uniform(91.5, 93.0), 1)
            # EQP-04 在45天前靶材快耗尽时有均匀性下降（已更换靶材恢复）
            elif eid == "EQP-04" and 44 <= d <= 47:
                yr = round(random.uniform(92.0, 93.5), 1)
            else:
                yr = round(random.uniform(lo, hi), 1)

            good = round(wafers * yr / 100)
            lot = f"LOT-{date.replace('-','')}-{eid[-2:]}"
            c.execute(
                "INSERT INTO yield_records "
                "(date,equipment_id,lot_id,wafer_count,good_count,yield_rate) "
                "VALUES (?,?,?,?,?,?)",
                (date, eid, lot, wafers, good, yr)
            )

    # ════════════════════════════════════════════════════
    # 工艺参数：近30天，每台设备每天记录各自参数
    # ════════════════════════════════════════════════════
    for d in range(30, -1, -1):
        date = (TODAY - datetime.timedelta(days=d)).isoformat()
        for eid, *_ in EQUIPMENT:
            for pname, lo, hi in PARAMS[eid]:
                mid = (lo + hi) / 2
                span = (hi - lo)

                # EQP-03 颗粒计数异常（核心根因线索）
                if eid == "EQP-03" and "颗粒" in pname:
                    if d == 0:
                        val = 138.0; in_spec = 0       # 今天严重超标
                    elif d == 1:
                        val = round(random.uniform(85, 105), 1); in_spec = 0
                    elif d == 2:
                        val = round(random.uniform(65, 85), 1); in_spec = 0
                    elif d <= 5:
                        val = round(random.uniform(40, 65), 1)
                        in_spec = 1 if val <= 50 else 0
                    else:
                        val = round(random.uniform(10, 35), 1); in_spec = 1

                # EQP-03 刻蚀速率在近3天也有轻微下滑（颗粒污染影响速率）
                elif eid == "EQP-03" and "刻蚀速率" in pname:
                    if d <= 2:
                        val = round(random.uniform(lo, lo + span * 0.3), 1)
                        in_spec = 1 if val >= lo else 0
                    else:
                        val = round(random.uniform(mid - span*0.2, mid + span*0.2), 1)
                        in_spec = 1

                # EQP-04 靶材45天前接近耗尽（已更换，现在正常）
                elif eid == "EQP-04" and "靶材" in pname:
                    if d >= 45:
                        val = round(random.uniform(68, 76), 1)  # 当时超标
                        in_spec = 0 if val > 70 else 1
                    elif d >= 30:
                        val = round(random.uniform(10, 25), 1)  # 更换后重新计数
                        in_spec = 1
                    else:
                        val = round(random.uniform(lo + d * 0.8, lo + d * 0.8 + 5), 1)
                        val = min(val, hi - 5)
                        in_spec = 1

                # EQP-05 法拉第杯清洁天数（计数器，超30天须清洁）
                elif eid == "EQP-05" and "法拉第杯" in pname:
                    days_since_clean = 28 - d if d <= 28 else 0
                    val = max(0, min(days_since_clean, 30))
                    in_spec = 1 if val <= 30 else 0

                # EQP-07 去离子水电阻率：近5天轻微下降（树脂需要更换预警）
                elif eid == "EQP-07" and "电阻率" in pname:
                    if d <= 4:
                        val = round(random.uniform(17.1, 17.5), 2)
                        in_spec = 1  # 尚在规格内，但趋势向下
                    else:
                        val = round(random.uniform(17.8, 18.2), 2)
                        in_spec = 1

                # 其他所有参数正常波动
                else:
                    noise = random.uniform(-span * 0.25, span * 0.25)
                    val = round(mid + noise, 2)
                    val = max(lo - span * 0.02, min(hi + span * 0.02, val))
                    in_spec = 1 if lo <= val <= hi else 0

                c.execute(
                    "INSERT INTO process_parameters "
                    "(date,equipment_id,param_name,value,spec_lower,spec_upper,in_spec) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (date, eid, pname, val, lo, hi, in_spec)
                )

    # ════════════════════════════════════════════════════
    # 报警记录（25条，覆盖多设备多类型）
    # ════════════════════════════════════════════════════
    alarms = [
        # EQP-03 颗粒污染系列（未解决，根因线索）
        ("2026-06-01 09:12", "EQP-03", "颗粒污染", "严重",
         "反应腔颗粒计数138个/片，严重超过规格上限50个/片，疑似腔体严重污染，建议立即停机处理", 0),
        ("2026-05-31 14:05", "EQP-03", "颗粒污染", "警告",
         "颗粒计数持续上升，当前96个/片，已超规格上限，请安排腔体检查", 0),
        ("2026-05-30 10:33", "EQP-03", "颗粒污染", "警告",
         "颗粒计数72个/片，超过规格上限50个/片，建议提前安排月度PM", 0),
        ("2026-05-29 08:17", "EQP-03", "保养超期",  "警告",
         "设备月度PM已超期12天，当前超期风险等级：橙色，请在48小时内安排保养", 0),

        # EQP-01 光刻机报警（已解决）
        ("2026-05-28 11:30", "EQP-01", "温度波动",  "提示",
         "洁净室温度短时波动至23.6度C（规格上限23.5度C），持续约8分钟后自动恢复", 1),
        ("2026-05-15 09:45", "EQP-01", "曝光量偏低", "警告",
         "激光输出能量下降2.3%，曝光量偏离目标值，已手动补偿调整，请关注激光管使用寿命", 1),
        ("2026-04-20 14:22", "EQP-01", "套刻超规",  "警告",
         "Y方向套刻精度5.8nm超出规格5nm，已重新校准对准系统，连续3批次恢复正常", 1),

        # EQP-02 刻蚀机报警（已解决）
        ("2026-05-02 16:40", "EQP-02", "气压异常",  "警告",
         "刻蚀气压波动至9.2mTorr（低于规格下限10mTorr），气压控制阀响应异常，已更换控制阀", 1),
        ("2026-04-08 10:15", "EQP-02", "颗粒污染",  "提示",
         "颗粒计数38个/片，接近报警上限30个/片，已提前安排月度PM，清洗后恢复正常", 1),

        # EQP-04 薄膜机报警（已解决）
        ("2026-04-17 13:55", "EQP-04", "靶材耗尽预警", "警告",
         "PVD靶材使用量达到74%，超过更换计划阈值70%，已申请紧急采购，次日完成更换", 1),
        ("2026-03-25 09:30", "EQP-04", "均匀性超规", "警告",
         "薄膜均匀性3.2%超出规格上限2%，经排查为靶材过耗所致，更换靶材后恢复", 1),

        # EQP-05 注入机报警（已解决）
        ("2026-05-25 16:48", "EQP-05", "真空度异常", "提示",
         "注入腔束线真空度短暂偏低至1.8×10⁻⁶Torr（规格上限1.0×10⁻⁶），已自动恢复", 1),
        ("2026-05-10 11:20", "EQP-05", "法拉第杯告警", "提示",
         "法拉第杯距上次清洗已28天，接近清洗周期30天，已安排次日清洗", 1),
        ("2026-04-02 15:33", "EQP-05", "注入均匀性", "警告",
         "剂量均匀性2.1%超出规格上限1.5%，经排查为法拉第杯积累物所致，清洗后恢复", 1),

        # EQP-06 CMP机报警（已解决）
        ("2026-05-20 08:45", "EQP-06", "去除量偏低", "提示",
         "CMP去除量115nm偏低（规格117~123nm），经检查为研磨垫磨损，已更换研磨垫", 1),
        ("2026-04-11 14:10", "EQP-06", "研磨液温度", "提示",
         "研磨液温度21.3度C轻微超出规格上限21.0度C，已调整冷却系统，恢复正常", 1),

        # EQP-07 湿法清洗机报警（部分未解决）
        ("2026-06-01 07:30", "EQP-07", "电阻率下降", "提示",
         "去离子水电阻率连续4天低于17.5MΩ·cm（规格17~18.2），离子交换树脂建议预防性更换", 0),
        ("2026-05-12 09:15", "EQP-07", "SC1液位低",  "提示",
         "SC1清洗液液位低于设定下限，已补充，恢复正常", 1),

        # EQP-08 检测机报警（已解决）
        ("2026-05-18 10:50", "EQP-08", "激光功率偏低", "提示",
         "检测激光功率98.2mW接近规格下限98.0mW，已校准，恢复至100.1mW", 1),
        ("2026-04-25 15:22", "EQP-08", "振动超规",   "提示",
         "检测台环境振动2.3μm/s²轻微超标，经排查为隔振台气压偏低，充气后恢复", 1),

        # 公用工程报警（全线影响，已解决）
        ("2026-05-05 06:20", "EQP-07", "超纯水系统",  "严重",
         "超纯水电阻率骤降至14.7MΩ·cm，离子交换树脂失效，已紧急更换，系统恢复正常历时6小时", 1),
        ("2026-03-15 22:10", "EQP-01", "洁净室温控",  "警告",
         "空调系统故障导致温度升至24.1度C，持续约90分钟，光刻工序暂停，故障修复后恢复", 1),

        # 历史重大报警（已解决，供参考）
        ("2026-02-14 08:00", "EQP-02", "颗粒污染",   "严重",
         "复工后首批次颗粒数312个/片，春节停机期间未充保护气体，腔体沉积物大量脱落，"
         "已深度清洗，耗时48小时恢复", 1),
        ("2026-01-08 13:45", "EQP-04", "腔体真空异常", "严重",
         "PVD腔体真空度无法达到工艺要求（实测8×10⁻⁷Torr，要求<5×10⁻⁷），"
         "经检查为腔体密封O型圈老化，已更换，真空度恢复正常", 1),
    ]

    for a in alarms:
        c.execute(
            "INSERT INTO alarms (datetime,equipment_id,alarm_type,level,message,resolved) "
            "VALUES (?,?,?,?,?,?)", a
        )

    # ════════════════════════════════════════════════════
    # 历史工单记录（6条已完成工单，体现系统运维闭环）
    # ════════════════════════════════════════════════════
    history_orders = [
        ("2026-05-05 08:30", "EQP-07", "超纯水系统恢复处置",
         "超纯水电阻率骤降至14.7MΩ·cm，紧急更换离子交换树脂，系统恢复正常。"
         "受影响批次23批已全部复检，7批正常放行，12批二次清洗后放行，4批报废。",
         "高", "已完成", "系统自动"),
        ("2026-04-17 10:00", "EQP-04", "靶材超期更换处置",
         "PVD靶材使用量74%超过阈值70%，紧急更换靶材，更换后均匀性恢复至1.1%，"
         "良率第2批次即恢复至95.2%，恢复时间约8小时。",
         "中", "已完成", "系统自动"),
        ("2026-04-02 16:00", "EQP-05", "法拉第杯清洁",
         "注入剂量均匀性2.1%超规，清洁法拉第杯（积累磷化合物沉积物），"
         "清洁后均匀性恢复至0.8%，耗时约3小时。",
         "中", "已完成", "系统自动"),
        ("2026-03-15 23:30", "EQP-01", "光刻机洁净室温控故障处置",
         "洁净室温控系统故障导致温度升至24.1度C，持续约90分钟，光刻工序暂停。"
         "设施部修复空调系统后恢复生产，受影响批次Overlay复测，1批次返工，其余正常放行。",
         "高", "已完成", "系统自动"),
        ("2026-02-15 09:00", "EQP-02", "春节复工腔体深度清洗",
         "春节停机后复工首批颗粒数312个/片，停机期间未充保护氮气，腔体沉积物大量脱落。"
         "执行腔体深度清洗8小时，Conditioning 4小时，颗粒恢复至18个/片，"
         "良率第3批次后稳定至96.2%。已制定长假停机规范。",
         "高", "已完成", "系统自动"),
        ("2026-01-09 08:00", "EQP-04", "PVD腔体O型圈更换",
         "PVD腔体真空度无法达标，经检查O型圈老化（使用约5个月，超额定寿命3个月）。"
         "更换全套O型圈，气密性验证合格，真空度恢复至3.2×10⁻⁷Torr，"
         "沉积速率和均匀性验证通过，恢复生产。",
         "高", "已完成", "系统自动"),
    ]

    for wo in history_orders:
        c.execute(
            "INSERT INTO work_orders (created_at,equipment_id,title,description,priority,status,created_by) "
            "VALUES (?,?,?,?,?,?,?)", wo
        )

    conn.commit()

    # ════════════════════════════════════════════════════
    # 打印验证概览
    # ════════════════════════════════════════════════════
    print(f"数据库已生成：{DB_PATH}\n")

    print("【设备保养状态（8台）】")
    for row in c.execute("SELECT equipment_id,name,next_maintenance FROM equipment ORDER BY equipment_id"):
        overdue = " ← ★已超期!" if row[2] < TODAY.isoformat() else ""
        print(f"  {row[0]}  {row[1]:<12}  应保养日:{row[2]}{overdue}")

    print("\n【EQP-03 近7天良率（根因设备）】")
    for row in c.execute(
        "SELECT date,yield_rate FROM yield_records "
        "WHERE equipment_id='EQP-03' ORDER BY date DESC LIMIT 7"
    ):
        flag = " ← ★骤降!" if row[1] < 90 else (" ← 下滑" if row[1] < 94 else "")
        print(f"  {row[0]}  {row[1]}%{flag}")

    print("\n【EQP-03 最新工艺参数（所有参数）】")
    for row in c.execute(
        "SELECT param_name,value,spec_lower,spec_upper,in_spec FROM process_parameters "
        "WHERE equipment_id='EQP-03' AND date=? ORDER BY param_name",
        (TODAY.isoformat(),)
    ):
        flag = "★超标★" if row[4] == 0 else "正常"
        print(f"  {row[0]}: {row[1]}（规格{row[2]}~{row[3]}）→ {flag}")

    print("\n【EQP-03 未解决报警（根因线索）】")
    for row in c.execute(
        "SELECT datetime,level,message FROM alarms "
        "WHERE equipment_id='EQP-03' AND resolved=0 ORDER BY datetime DESC"
    ):
        print(f"  [{row[1]}] {row[0]}")
        print(f"    {row[2][:60]}...")

    print("\n【数据规模统计】")
    for table in ["equipment", "yield_records", "process_parameters", "alarms", "work_orders"]:
        cnt = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:<22}: {cnt} 条记录")

    print("\n【历史已完成工单（6条）】")
    for row in c.execute(
        "SELECT created_at,equipment_id,title FROM work_orders "
        "WHERE status='已完成' ORDER BY created_at DESC"
    ):
        print(f"  {row[0][:10]}  {row[1]}  {row[2]}")

    conn.close()
    print("\n✅ 数据库构建完成！")
    print("   根因线索：EQP-03 保养超期15天 → 颗粒污染138个/片 → 良率骤降至88%")
    print("   8台设备 / 60天良率记录 / 30天工艺参数 / 25条报警 / 6条历史工单")


if __name__ == "__main__":
    build()
