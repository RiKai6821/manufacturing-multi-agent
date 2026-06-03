# -*- coding: utf-8 -*-
"""
微调模型调用与对比脚本
========================================================================
对比"基础模型"与"微调后模型"在诊断任务上的输出差异。

用法：
  微调前（建立基线）：python use_finetuned.py
  微调后：把下面 FINETUNED_MODEL 改成你的微调模型ID，再次运行对比

说明：
  微调模型的调用方式和普通模型完全一样，只是 model 参数换成微调后的ID。
  这正体现了"配置中心化"——切换模型零代码改动。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config  # 自动加载 DASHSCOPE_API_KEY
from openai import OpenAI

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

client = OpenAI(api_key=os.getenv("DASHSCOPE_API_KEY"),
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")

BASE_MODEL = "qwen-plus"
# ↓↓↓ 微调完成后，把这里换成你的微调模型ID（形如 qwen-plus-ft-xxxxxxxx）↓↓↓
FINETUNED_MODEL = None   # None 表示尚未微调，只跑基础模型

SYSTEM = (
    "你是半导体晶圆制造的良率异常诊断专家。收到设备异常描述后，"
    "按'确认数据→查报警→查参数→核对保养→比对历史案例→形成根因'的范式分析，"
    "只依据事实推断，输出包含根因和处理建议的简洁结论。")

# 一个测试用的设备症状（训练集里没有的新案例，测泛化）
TEST_CASE = ("某离子注入机近3批次良率从95%降至92%，注入剂量均匀性从0.8%升到2.6%，"
             "工艺参数正常，保养未超期，法拉第杯已40天未清洗。请分析根因并给处理建议。")


def ask(model: str, question: str) -> str:
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": question}],
        temperature=0,
    )
    return resp.choices[0].message.content


if __name__ == "__main__":
    if not os.getenv("DASHSCOPE_API_KEY"):
        raise SystemExit("请先设置 DASHSCOPE_API_KEY")

    print("=" * 70)
    print("测试症状：", TEST_CASE)
    print("=" * 70)

    print("\n【基础模型 qwen-plus 的回答】\n")
    print(ask(BASE_MODEL, TEST_CASE))

    if FINETUNED_MODEL:
        print("\n" + "=" * 70)
        print(f"\n【微调模型 {FINETUNED_MODEL} 的回答】\n")
        print(ask(FINETUNED_MODEL, TEST_CASE))
        print("\n对比观察：微调模型应更贴合诊断范式、术语更专业、输出格式更稳定。")
    else:
        print("\n（FINETUNED_MODEL 尚未设置。完成百炼微调后，"
              "把模型ID填入本文件 FINETUNED_MODEL 即可对比。）")
