# -*- coding: utf-8 -*-
"""memory 单元测试：会话记忆 + 诊断持久化 + 经验召回（无需大模型API）。"""
import os
import tempfile
from memory import SessionMemory, DiagnosisMemory


class TestSessionMemory:
    def test_multi_turn_context(self):
        s = SessionMemory()
        s.add_user("EQP-03良率掉了")
        s.add_assistant("根因是保养超期")
        s.add_user("那EQP-05呢")
        ctx = s.get_context()
        assert len(ctx) == 3
        assert ctx[-1]["content"] == "那EQP-05呢"

    def test_sliding_window(self):
        """超过窗口应只保留最近若干轮。"""
        s = SessionMemory(max_turns=2)   # 最多2轮=4条
        for i in range(10):
            s.add_user(f"问题{i}")
            s.add_assistant(f"回答{i}")
        ctx = s.get_context()
        assert len(ctx) <= 4

    def test_session_id_generated(self):
        s = SessionMemory()
        assert s.session_id.startswith("sess-")


class TestDiagnosisMemory:
    def _fresh_mem(self):
        """每个测试用独立临时库，互不干扰。"""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        return DiagnosisMemory(db_path=tmp.name)

    def test_save_and_keyword_extraction(self):
        mem = self._fresh_mem()
        rec_id = mem.save("EQP-03", "良率88%",
                          "根因分析：保养超期导致颗粒污染，颗粒计数138超标。")
        assert rec_id == 1

    def test_recall_by_equipment(self):
        mem = self._fresh_mem()
        mem.save("EQP-03", "q", "根因分析：保养超期颗粒污染。")
        rows = mem.recall_by_equipment("EQP-03")
        assert len(rows) == 1

    def test_recall_similar_by_keywords(self):
        """相同症状关键词应能跨设备召回。"""
        mem = self._fresh_mem()
        mem.save("EQP-02", "q", "根因分析：保养超期导致颗粒污染。")
        similar = mem.recall_similar("颗粒污染 保养超期", exclude_equipment="EQP-03")
        assert len(similar) >= 1

    def test_recall_prompt_empty_when_no_history(self):
        mem = self._fresh_mem()
        prompt = mem.build_recall_prompt("EQP-08", "未知问题")
        assert prompt == ""   # 无历史，返回空
