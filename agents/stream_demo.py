# -*- coding: utf-8 -*-
"""
流式输出示例（响应时效优化：改善"感知延迟"）
========================================================================
诊断报告较长，一次性等全部生成完再返回，用户要干等几十秒。
流式输出（streaming）让 token 边生成边显示，用户立刻看到进度，
首字延迟（TTFT）从"等全文"降到"等第一个字"，体验大幅提升。

本示例演示：把最终报告生成改为流式。集成到 main.py 时，
只需在协调者最后一轮（无tool_calls、要出报告时）改用 stream=True。

运行：python stream_demo.py
"""

import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config  # noqa
from settings import settings
from openai import OpenAI

client = OpenAI(api_key=os.getenv("DASHSCOPE_API_KEY"),
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")


def stream_report(prompt: str):
    """流式生成并实时打印，返回首字延迟和总耗时。"""
    t0 = time.time()
    ttft = None   # time to first token
    full = []

    stream = client.chat.completions.create(
        model=settings.chat_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=settings.temperature,
        stream=True,                      # ← 关键：开启流式
    )
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            if ttft is None:
                ttft = time.time() - t0   # 记录首字延迟
            print(delta, end="", flush=True)
            full.append(delta)

    total = time.time() - t0
    return ttft, total, "".join(full)


if __name__ == "__main__":
    if not os.getenv("DASHSCOPE_API_KEY"):
        raise SystemExit("请先设置 DASHSCOPE_API_KEY")

    prompt = ("根据以下诊断数据生成一份简洁的设备异常诊断报告："
              "EQP-03良率骤降至88%，颗粒计数138超标，保养超期15天，根因为保养超期导致腔体颗粒污染。")

    print("=" * 60)
    print("流式输出演示（注意：文字会逐字出现，而非一次性弹出）")
    print("=" * 60 + "\n")
    ttft, total, _ = stream_report(prompt)
    print("\n\n" + "=" * 60)
    print(f"⏱️  首字延迟(TTFT)：{ttft:.2f}s   总耗时：{total:.2f}s")
    print("对比非流式：用户需等待整个 总耗时 才能看到任何内容；")
    print(f"流式下用户 {ttft:.2f}s 就开始看到报告，感知延迟大幅降低。")
    print("=" * 60)
