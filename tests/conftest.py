# -*- coding: utf-8 -*-
"""
pytest 公共配置：把项目各层目录加入 import 路径，并确保数据库就绪。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(__file__))   # Multi_Agent/
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "agents"))

import pytest


@pytest.fixture(scope="session", autouse=True)
def ensure_database():
    """测试前确保 factory.db 存在（不存在则构建）。"""
    db_path = os.path.join(ROOT, "data", "factory.db")
    if not os.path.exists(db_path):
        sys.path.insert(0, os.path.join(ROOT, "data"))
        import build_database
        build_database.build()
    yield
