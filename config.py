# -*- coding: utf-8 -*-
"""
配置加载：读取 DASHSCOPE_API_KEY。
优先级：已设置的环境变量 > 同目录 .env 文件。
本文件不包含任何真实密钥，可安全提交到仓库。
本地开发：复制 .env.example 为 .env 并填入你的真实 key（.env 已被 gitignore）。
"""
import os
from pathlib import Path

_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
