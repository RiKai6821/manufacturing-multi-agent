# -*- coding: utf-8 -*-
"""db_tools 单元测试：查询正确性 + 输入校验（无需调用大模型API）。"""
import db_tools


class TestYieldTrend:
    def test_eqp03_yield_drop_detected(self):
        """EQP-03 良率骤降应被检出并标注异常。"""
        r = db_tools.query_yield_trend("EQP-03", days=7)
        assert "88.0" in r              # 最新良率
        assert "异常" in r or "下降" in r  # 趋势判断

    def test_invalid_equipment_rejected(self):
        """非法设备号应返回友好错误，不抛异常。"""
        r = db_tools.query_yield_trend("EQP-99")
        assert "不存在" in r

    def test_invalid_days_rejected(self):
        """超范围天数应被拦截。"""
        r = db_tools.query_yield_trend("EQP-03", days=999)
        assert "1~90" in r or "错误" in r


class TestMaintenance:
    def test_eqp03_overdue_red_alert(self):
        """EQP-03 保养超期15天应判红色预警。"""
        r = db_tools.query_equipment_maintenance("EQP-03")
        assert "超期" in r
        assert "红色" in r

    def test_normal_equipment_not_overdue(self):
        """正常设备不应报超期红色预警。"""
        r = db_tools.query_equipment_maintenance("EQP-01")
        assert "红色预警" not in r


class TestProcessParameters:
    def test_eqp03_particle_exceeds(self):
        """EQP-03 颗粒计数138应标超标。"""
        r = db_tools.query_process_parameters("EQP-03")
        assert "138" in r
        assert "超标" in r


class TestAlarms:
    def test_eqp03_has_unresolved_alarms(self):
        """EQP-03 应有未解决报警。"""
        r = db_tools.query_alarms("EQP-03")
        assert "颗粒污染" in r
        assert "严重" in r


class TestCrossComparison:
    def test_etch_comparison(self):
        """刻蚀类横向对比应同时包含 EQP-02 和 EQP-03。"""
        r = db_tools.query_cross_equipment_comparison("刻蚀", days=7)
        assert "EQP-03" in r and "EQP-02" in r

    def test_invalid_type_rejected(self):
        r = db_tools.query_cross_equipment_comparison("不存在的类型")
        assert "不合法" in r
