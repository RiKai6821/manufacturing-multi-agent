# -*- coding: utf-8 -*-
"""
统一日志配置
所有模块通过 get_logger(__name__) 获取 logger，格式和级别统一管理。
"""
import logging
import sys
import os


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """
    获取统一格式的 logger。
    name 传 __name__ 即可，日志前缀会显示模块路径。
    """
    logger = logging.getLogger(name)

    # 防止重复添加 handler（模块被多次 import 时）
    if logger.handlers:
        return logger

    logger.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s [%(levelname)-5s] %(name)s - %(message)s",
        datefmt="%H:%M:%S"
    ))
    logger.addHandler(handler)
    logger.propagate = False   # 不向根 logger 传播，防止重复打印
    return logger


# 文件日志（可选，生产环境开启）
def get_file_logger(name: str, log_path: str = "agent_run.log") -> logging.Logger:
    logger = get_logger(name)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s [%(levelname)-5s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(file_handler)
    return logger
