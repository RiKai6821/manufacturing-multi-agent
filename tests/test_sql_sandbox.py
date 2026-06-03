# -*- coding: utf-8 -*-
"""sql_sandbox 安全护栏测试：只读 SELECT 白名单 + 物理只读 + 真实查询（无需大模型API）。"""
import sqlite3
import pytest
import sql_sandbox


class TestValidate:
    def test_select_ok(self):
        assert sql_sandbox.validate("SELECT * FROM equipment")[0]

    def test_with_cte_ok(self):
        assert sql_sandbox.validate("WITH x AS (SELECT 1 AS a) SELECT a FROM x")[0]

    def test_delete_blocked(self):
        assert not sql_sandbox.validate("DELETE FROM equipment")[0]

    def test_update_blocked(self):
        assert not sql_sandbox.validate("UPDATE equipment SET status='x'")[0]

    def test_drop_blocked(self):
        assert not sql_sandbox.validate("DROP TABLE equipment")[0]

    def test_pragma_blocked(self):
        assert not sql_sandbox.validate("PRAGMA table_info(equipment)")[0]

    def test_multi_statement_blocked(self):
        assert not sql_sandbox.validate("SELECT 1; DROP TABLE equipment")[0]

    def test_comment_cannot_hide_injection(self):
        """注释剥离后若仍是多语句/写操作，必须拦截。"""
        assert not sql_sandbox.validate("SELECT 1 /* x */ ; DELETE FROM equipment")[0]

    def test_empty_blocked(self):
        assert not sql_sandbox.validate("   ")[0]

    def test_keyword_inside_string_literal_allowed(self):
        """字符串字面量里的危险词/分号不应被误杀（剥离字面量后再扫描）。"""
        ok, _ = sql_sandbox.validate(
            "SELECT * FROM alarms WHERE message LIKE '%delete; drop%'")
        assert ok

    def test_injection_outside_literal_still_blocked(self):
        """真正语句结构里的写操作仍须拦截（确认剥离字面量没削弱护栏）。"""
        assert not sql_sandbox.validate(
            "SELECT name FROM equipment WHERE name='x'; DELETE FROM equipment")[0]


class TestRunSelect:
    def test_real_query_returns_rows(self):
        cols, rows = sql_sandbox.run_select(
            "SELECT equipment_id, name FROM equipment ORDER BY equipment_id LIMIT 3")
        assert cols == ["equipment_id", "name"]
        assert 0 < len(rows) <= 3

    def test_param_binding(self):
        cols, rows = sql_sandbox.run_select(
            "SELECT name FROM equipment WHERE equipment_id = :eid",
            {"eid": "EQP-03"})
        assert rows and "刻蚀" in rows[0][0]

    def test_row_cap_enforced(self):
        cols, rows = sql_sandbox.run_select(
            "SELECT * FROM yield_records", max_rows=5)
        assert len(rows) <= 5

    def test_invalid_sql_raises(self):
        with pytest.raises(ValueError):
            sql_sandbox.run_select("DELETE FROM equipment")


class TestPhysicalReadOnly:
    def test_readonly_connection_blocks_write(self):
        """即使绕过校验，只读连接本身也必须物理拒绝写入。"""
        conn = sql_sandbox._connect_readonly()
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("UPDATE equipment SET status='HACKED' WHERE equipment_id='EQP-01'")
                conn.commit()
        finally:
            conn.close()


class TestSchema:
    def test_schema_lists_core_tables(self):
        schema = sql_sandbox.get_schema()
        for t in ("equipment", "yield_records", "process_parameters", "alarms", "work_orders"):
            assert t in schema
