# -*- coding: utf-8 -*-
"""
记忆管理模块（Memory Management）
========================================================================
对应 JD 工作职责第一条核心能力：「任务规划、记忆管理、工具调用」。

Agent 的"记忆"分三层，本模块全部实现：

  1. 会话记忆（SessionMemory）—— 短期
     一次多轮对话内的上下文，支持"那EQP-05呢？"这类追问，
     带滑动窗口，防止 context 无限增长。

  2. 诊断历史（DiagnosisMemory）—— 长期持久化
     每次诊断结果落库（独立 memory.db），可按设备/时间检索。
     体现"诊断→沉淀→可追溯"。

  3. 经验召回（recall_*）—— 长期复用（自我进化）
     新诊断开始前，自动召回该设备/相似症状的历史经验注入上下文，
     让系统"记得"这台设备以前出过什么问题、怎么解决的。

★ 架构设计（面试讲点）：
  memory.db 与 factory.db 物理分离——
  factory.db 代表"外部 MES/ERP 系统数据"，
  memory.db 代表"Agent 自身的记忆状态"，
  两者职责不同，分库管理是清晰的工程划分。
"""

import os
import sys
import sqlite3
import datetime
import uuid
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from settings import settings
from logger_config import get_logger

logger = get_logger(__name__)

MEMORY_DB = os.path.join(os.path.dirname(__file__), "..", "data", "memory.db")

# 领域关键词表：用于从报告中抽取症状标签，支撑相似经验召回
DOMAIN_KEYWORDS = [
    "颗粒污染", "颗粒计数", "保养超期", "气压", "刻蚀温度", "靶材", "良率下降",
    "良率骤降", "密封件", "O型圈", "电极", "镀层", "法拉第杯", "来料", "真空度",
    "均匀性", "喷淋头", "气压控制阀", "激光", "套刻", "Overlay", "颗粒超标",
    "停机", "腔体清洗", "工艺参数", "报警", "RF功率", "剂量",
]


# ════════════════════════════════════════════════════════════
# 第 1 层：会话记忆（短期，多轮对话）
# ════════════════════════════════════════════════════════════

class SessionMemory:
    """一次会话内的多轮上下文，带滑动窗口压缩。
    用于交互式诊断：用户可以连续追问，系统记得前文。"""

    def __init__(self, session_id: str = None, max_turns: int = None):
        self.session_id = session_id or f"sess-{uuid.uuid4().hex[:8]}"
        self.max_turns = max_turns or settings.max_tool_results_in_history
        self.turns = []   # [{"role": "user"/"assistant", "content": ...}]

    def add_user(self, content: str):
        self.turns.append({"role": "user", "content": content})

    def add_assistant(self, content: str):
        self.turns.append({"role": "assistant", "content": content})

    def get_context(self) -> list:
        """返回最近 max_turns 轮对话（滑动窗口），供注入到下一轮 messages。"""
        # 一轮 = user + assistant 两条，保留最近 max_turns 轮
        keep = self.max_turns * 2
        return self.turns[-keep:] if len(self.turns) > keep else list(self.turns)

    def get_history_brief(self) -> str:
        """生成历史对话的简短文本摘要，注入到 system 提示里。"""
        ctx = self.get_context()
        if not ctx:
            return ""
        lines = ["【本次会话历史】"]
        for t in ctx:
            who = "用户" if t["role"] == "user" else "助手"
            brief = t["content"][:80] + ("…" if len(t["content"]) > 80 else "")
            lines.append(f"  {who}：{brief}")
        return "\n".join(lines)

    def clear(self):
        self.turns.clear()


# ════════════════════════════════════════════════════════════
# 第 2、3 层：诊断历史持久化 + 经验召回（长期）
# ════════════════════════════════════════════════════════════

class DiagnosisMemory:
    """诊断历史长期记忆：落库 + 按设备/相似症状召回。"""

    def __init__(self, db_path: str = MEMORY_DB):
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=settings.db_timeout)
        try:
            yield conn
            conn.commit()
        except sqlite3.OperationalError as e:
            conn.rollback()
            logger.error(f"记忆库操作失败: {e}")
            raise
        finally:
            conn.close()

    def _init_db(self):
        """首次使用自动建表。"""
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS diagnosis_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT,
                    session_id TEXT,
                    equipment_id TEXT,
                    user_request TEXT,
                    root_cause TEXT,
                    keywords TEXT,
                    report TEXT
                )
            """)
        logger.debug(f"记忆库就绪: {self.db_path}")

    # ── 抽取症状关键词（确定性，不额外调LLM）──
    @staticmethod
    def _extract_keywords(text: str) -> list:
        return [kw for kw in DOMAIN_KEYWORDS if kw in text]

    # ── 抽取根因摘要（从报告的"根因"段落，启发式）──
    @staticmethod
    def _extract_root_cause(report: str) -> str:
        for marker in ["根因分析", "根本原因", "根因结论", "根因"]:
            idx = report.find(marker)
            if idx != -1:
                seg = report[idx: idx + 200].replace("\n", " ")
                return seg
        return report[:150].replace("\n", " ")

    # ── 保存一次诊断 ──
    def save(self, equipment_id: str, user_request: str, report: str,
             session_id: str = "") -> int:
        keywords = self._extract_keywords(report)
        root_cause = self._extract_root_cause(report)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO diagnosis_records "
                "(created_at, session_id, equipment_id, user_request, root_cause, keywords, report) "
                "VALUES (?,?,?,?,?,?,?)",
                (now, session_id, equipment_id, user_request,
                 root_cause, ",".join(keywords), report),
            )
            rec_id = cur.lastrowid
        logger.info(f"诊断已存入记忆库: REC-{rec_id:04d} ({equipment_id}, 关键词:{keywords})")
        return rec_id

    # ── 召回：该设备的历史诊断 ──
    def recall_by_equipment(self, equipment_id: str, limit: int = 3) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT created_at, root_cause, keywords FROM diagnosis_records "
                "WHERE equipment_id=? ORDER BY id DESC LIMIT ?",
                (equipment_id, limit),
            ).fetchall()
        return rows

    # ── 召回：相似症状的历史诊断（按共享关键词数排序）──
    def recall_similar(self, symptom_text: str, exclude_equipment: str = None,
                       limit: int = 3) -> list:
        target_kws = set(self._extract_keywords(symptom_text))
        if not target_kws:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT equipment_id, created_at, root_cause, keywords FROM diagnosis_records"
            ).fetchall()

        scored = []
        for eid, created, root_cause, kw_str in rows:
            if exclude_equipment and eid == exclude_equipment:
                continue
            kws = set(kw_str.split(",")) if kw_str else set()
            overlap = len(target_kws & kws)
            if overlap > 0:
                scored.append((overlap, eid, created, root_cause))
        scored.sort(key=lambda x: -x[0])
        return scored[:limit]

    # ── 生成召回提示文本（注入到协调者上下文）──
    def build_recall_prompt(self, equipment_id: str, symptom_text: str) -> str:
        same = self.recall_by_equipment(equipment_id, limit=2)
        similar = self.recall_similar(symptom_text, exclude_equipment=equipment_id, limit=2)

        if not same and not similar:
            return ""   # 无历史记忆，首次诊断该设备

        lines = ["【记忆召回：以下是系统过往诊断经验，供参考，但仍须以本次实际数据为准】"]
        if same:
            lines.append(f"· 本设备（{equipment_id}）历史诊断：")
            for created, root_cause, kws in same:
                lines.append(f"    [{created}] {root_cause}")
        if similar:
            lines.append("· 其他设备的相似症状案例：")
            for overlap, eid, created, root_cause in similar:
                lines.append(f"    [{eid} @ {created}] {root_cause}（{overlap}个症状吻合）")
        return "\n".join(lines)

    # ── 统计 ──
    def stats(self) -> str:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM diagnosis_records").fetchone()[0]
            by_equip = conn.execute(
                "SELECT equipment_id, COUNT(*) FROM diagnosis_records "
                "GROUP BY equipment_id ORDER BY COUNT(*) DESC"
            ).fetchall()
        if total == 0:
            return "记忆库为空，尚无历史诊断记录。"
        lines = [f"记忆库统计（共 {total} 条诊断记录）：", "  按设备分布："]
        for eid, cnt in by_equip:
            lines.append(f"    {eid}：{cnt} 次")
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════
# 本地自测
# ════════════════════════════════════════════════════════════
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("【测试 1：会话记忆（多轮）】\n")
    sess = SessionMemory()
    sess.add_user("EQP-03良率掉到88%了")
    sess.add_assistant("根因是保养超期导致颗粒污染，已生成工单。")
    sess.add_user("那EQP-05呢？")
    print(f"会话ID：{sess.session_id}")
    print(sess.get_history_brief())

    print("\n【测试 2：诊断历史持久化】\n")
    mem = DiagnosisMemory()
    rec_id = mem.save(
        equipment_id="EQP-03",
        user_request="EQP-03良率掉到88%",
        report="根因分析：保养超期15天导致反应腔颗粒污染，颗粒计数138超标，良率骤降至88%。已生成工单WO-0011。",
        session_id=sess.session_id,
    )
    print(f"已保存记录 REC-{rec_id:04d}")

    # 再存一条相似的（不同设备）
    mem.save(
        equipment_id="EQP-02",
        user_request="EQP-02颗粒超标",
        report="根因分析：保养超期导致颗粒污染，颗粒计数156超标，停机清洗腔体后恢复。",
    )

    print("\n【测试 3：经验召回】\n")
    print(mem.build_recall_prompt("EQP-03", "良率下降，颗粒污染，保养超期"))

    print("\n【测试 4：记忆库统计】\n")
    print(mem.stats())
