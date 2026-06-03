# -*- coding: utf-8 -*-
"""
视觉检测 Agent（多模态输入）
========================================================================
用户上传设备/晶圆故障照片 → 用视觉模型（qwen-vl）转成结构化的客观观察文本，
再喂进现有的文本诊断流水线（协调者照常调数据/知识Agent交叉印证）。

设计要点（延续系统的防幻觉理念）：
  视觉模型"看到"的现象只是【假设】，不是结论——它只描述现象、不臆断根因，
  真正的根因仍由后续 Agent 结合数据库客观数据（颗粒计数/良率/报警）验证。
  即"图像观察 → 数据印证 → 才采信"。

依赖：DASHSCOPE_API_KEY；模型名取 settings.vision_model（默认 qwen-vl-max）。
"""

import os
import sys
import base64

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config  # 自动加载 DASHSCOPE_API_KEY
from settings import settings
from logger_config import get_logger
from openai import OpenAI

logger = get_logger(__name__)
client = OpenAI(api_key=os.getenv("DASHSCOPE_API_KEY"),
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")

_VISION_SYSTEM = (
    "你是半导体/制造设备的视觉检测专家。请客观描述用户上传的故障/缺陷照片中"
    "【实际看得到】的现象：缺陷的位置、形态、颜色、分布范围、可疑的缺陷类型。"
    "只描述现象，不要臆断根本原因，更不要编造图中看不到的细节（如具体数值/编号）。"
    "用中文输出条理清晰的结构化观察。")


def inspect_image(image_bytes: bytes, mime: str = "image/jpeg", hint: str = "") -> str:
    """看一张图，返回结构化观察文本。

    Args:
        image_bytes: 图片二进制
        mime:        图片类型，如 image/jpeg、image/png
        hint:        用户的补充说明（可选），如"这是3号刻蚀机的晶圆"
    """
    b64 = base64.b64encode(image_bytes).decode()
    data_url = f"data:{mime};base64,{b64}"
    user_text = hint.strip() or "请描述这张设备/晶圆故障照片中观察到的异常现象。"
    content = [
        {"type": "text", "text": user_text},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    logger.info(f"视觉检测：调用 {settings.vision_model}，提示='{user_text[:30]}'")
    resp = client.chat.completions.create(
        model=settings.vision_model,
        messages=[{"role": "system", "content": _VISION_SYSTEM},
                  {"role": "user", "content": content}],
        temperature=settings.temperature)
    return resp.choices[0].message.content


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not os.getenv("DASHSCOPE_API_KEY"):
        raise SystemExit("请先设置 DASHSCOPE_API_KEY")
    if len(sys.argv) < 2:
        raise SystemExit("用法：python vision_agent.py <图片路径> [补充说明]")
    path = sys.argv[1]
    hint = sys.argv[2] if len(sys.argv) > 2 else ""
    with open(path, "rb") as f:
        img = f.read()
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "jpeg"
    mime = "image/png" if ext == "png" else "image/jpeg"
    print("=== 视觉观察 ===")
    print(inspect_image(img, mime, hint))
