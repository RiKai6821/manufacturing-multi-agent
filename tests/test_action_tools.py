# -*- coding: utf-8 -*-
"""action_tools 单元测试：工单创建/查询/状态更新 + 输入校验。"""
import action_tools


class TestCreateWorkOrder:
    def test_create_success(self):
        r = action_tools.create_work_order(
            "EQP-03", "测试工单", "测试根因与处理建议", "高")
        assert "工单已生成" in r
        assert "WO-" in r

    def test_invalid_equipment(self):
        r = action_tools.create_work_order("EQP-99", "标题", "描述")
        assert "不存在" in r

    def test_empty_title_rejected(self):
        r = action_tools.create_work_order("EQP-03", "", "描述")
        assert "标题不能为空" in r

    def test_invalid_priority_rejected(self):
        r = action_tools.create_work_order("EQP-03", "标题", "描述", "紧急")
        assert "不合法" in r


class TestListAndUpdate:
    def test_list_returns_orders(self):
        action_tools.create_work_order("EQP-03", "查询测试", "描述", "中")
        r = action_tools.list_work_orders("EQP-03")
        assert "WO-" in r

    def test_invalid_status_filter(self):
        r = action_tools.list_work_orders("EQP-03", status="不存在状态")
        assert "不合法" in r

    def test_update_status_closes_loop(self):
        """建单→更新状态，形成运维闭环。"""
        create = action_tools.create_work_order("EQP-03", "闭环测试", "描述", "低")
        import re
        wo_id = int(re.search(r"WO-(\d+)", create).group(1))
        r = action_tools.update_work_order_status(wo_id, "处理中", "测试备注")
        assert "状态已更新" in r
        assert "处理中" in r

    def test_update_nonexistent(self):
        r = action_tools.update_work_order_status(99999, "已完成")
        assert "不存在" in r
