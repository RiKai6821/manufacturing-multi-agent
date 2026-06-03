# -*- coding: utf-8 -*-
"""
微调数据集生成器
========================================================================
从企业知识库（历史案例库 + SOP + 工艺规范）自动构造监督微调（SFT）数据集，
用于在阿里云百炼平台微调一个"半导体良率诊断专家"模型。

为什么要微调（面试讲点：微调 vs RAG vs Prompt 的取舍）：
  - RAG：知识经常更新、需要可溯源 → 用检索（本项目知识库走RAG）
  - Prompt：通用能力、少量约束 → 写提示词即可
  - 微调：要把"领域思维方式/输出风格/诊断范式"固化进模型 → 用微调
    例如让模型养成"症状→查保养→查颗粒→比对历史→下根因"的固定诊断范式，
    并稳定输出结构化报告，这类"行为模式"靠微调比靠超长Prompt更稳定、更省token。

输出格式：百炼 SFT 标准 JSONL，每行一个 messages 对话样本。
  {"messages": [{"role":"system",...},{"role":"user",...},{"role":"assistant",...}]}

运行：python generate_dataset.py
产出：finetune/sft_dataset.jsonl
"""

import os
import re
import json

KB_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "knowledge")
OUT_PATH = os.path.join(os.path.dirname(__file__), "sft_dataset.jsonl")

SYSTEM_PROMPT = (
    "你是半导体晶圆制造的良率异常诊断专家。收到设备异常描述后，"
    "按'确认数据→查报警→查参数→核对保养→比对历史案例→形成根因'的范式分析，"
    "只依据事实推断，输出包含根因和处理建议的简洁结论。")


# ════════════════════════════════════════════════════════════
# 来源1：历史案例库 → 症状到根因的诊断样本
# ════════════════════════════════════════════════════════════

def parse_cases(text: str) -> list:
    """按'案例编号'切分，抽取每个案例的关键字段。"""
    samples = []
    # 以"案例编号"为分隔切块
    blocks = re.split(r"案例编号：", text)
    for blk in blocks[1:]:
        def grab(field, nxt_fields):
            """抽取 field 到下一个字段之间的内容。"""
            pattern = field + r"[：:]\s*(.+?)(?=" + "|".join(nxt_fields) + r"|$)"
            m = re.search(pattern, blk, re.S)
            return m.group(1).strip() if m else ""

        equip = grab("涉及设备", ["根因类型", "异常描述", "优先级"])
        symptom = grab("异常描述", ["排查过程"])
        root = grab("根因结论", ["处理方式"])
        fix = grab("处理方式", ["恢复时间", "经验总结"])

        if symptom and root:
            # 构造用户问题（症状）和专家回答（根因+处理）
            user = f"设备情况：{symptom[:200]}\n请分析根本原因并给出处理建议。"
            assistant_parts = [f"【根因分析】{root[:300]}"]
            if fix:
                assistant_parts.append(f"【处理建议】{fix[:300]}")
            assistant = "\n".join(assistant_parts)
            samples.append((user, assistant))
    return samples


# ════════════════════════════════════════════════════════════
# 来源2：SOP/工艺规范 → 知识问答样本（手工设计高质量问答模板）
# ════════════════════════════════════════════════════════════

KNOWLEDGE_QA = [
    ("颗粒计数达到多少必须立即停机？",
     "颗粒计数超过规格上限2倍（即100个/片以上）属红色停机阈值，须立即停机、禁止投片，"
     "设备工程师1小时内到场，执行腔体拆解清洗，按月度PM标准验证后方可复产。"),
    ("刻蚀机保养超期多少天属于高风险？",
     "保养超期15天以上属红色预警，颗粒超标概率约45%，应立即停机执行月度PM；"
     "超期7~15天为橙色预警，须72小时内安排保养。"),
    ("良率异常排查的标准步骤是什么？",
     "按六步法：1.确认良率数据与异常范围；2.查看设备报警记录；3.检查工艺参数；"
     "4.核对设备保养状态；5.比对历史案例；6.综合形成根因并处置。"),
    ("怎么区分颗粒污染型和参数漂移型良率下降？",
     "颗粒污染型：Die Map随机分布、颗粒计数超标、常伴保养超期、降幅较大；"
     "参数漂移型：Die Map呈均匀性问题、颗粒正常、特定参数超规、降幅较小。"),
    ("怎么判断良率下降是设备问题还是来料问题？",
     "设备问题：仅本台设备异常、与设备状态变化吻合、可重复；"
     "来料问题：多台设备同批次同时异常、与特定来料批次时间吻合、换批次后消失。"),
    ("批次良率异常后如何决定放行还是报废？",
     "参数超规<10%且电学测试合格可放行；超规10~30%需返工评估；"
     "超规>30%或良率严重不达标且无法返工则报废，须质量主管签字确认。"),
    ("靶材使用量到多少需要更换？",
     "使用量达70%列入更换计划，禁止超过75%继续使用；超期使用会导致溅射均匀性下降、"
     "薄膜厚度不均，引发良率缓慢下滑。"),
    ("法拉第杯多久清洗一次？不清洗会怎样？",
     "法拉第杯清洗周期为30天。超期会积累注入杂质沉积物，导致束流测量失准、"
     "剂量均匀性超规，进而影响注入深度和良率。"),
]


# ════════════════════════════════════════════════════════════
# 来源3：诊断范式样本（教模型养成结构化诊断习惯）
# ════════════════════════════════════════════════════════════

PARADIGM_QA = [
    ("3号刻蚀机良率从96%掉到88%，颗粒计数138超标，保养超期15天，怎么办？",
     "【根因分析】保养超期15天导致反应腔内壁污染物累积，颗粒计数138个/片严重超标"
     "（超规格上限176%），晶圆表面缺陷增多，造成良率从96%骤降至88%。此为典型"
     "A类腔体污染型异常，与历史案例规律一致。\n"
     "【处理建议】1.立即停机；2.拆解清洗反应腔体；3.执行完整月度PM并更换密封件；"
     "4.Conditioning后颗粒计数验证<30个/片方可复产；5.连续3批次良率达标确认恢复。\n"
     "【优先级】高，建议生成紧急工单。"),
    ("某薄膜机良率缓慢下滑，颗粒正常但均匀性变差，可能是什么原因？",
     "【根因分析】颗粒正常排除腔体污染，均匀性变差指向C类耗材老化。最可能是靶材"
     "过度消耗（使用量超70%）导致溅射不均，或喷淋头孔位堵塞导致进气不均。\n"
     "【处理建议】优先检查靶材使用量记录，超70%则更换靶材并做3批次工艺验证；"
     "若靶材正常则检查喷淋头堵塞情况。\n【优先级】中。"),
]


def build_dataset():
    samples = []

    # 来源1：历史案例
    case_path = os.path.join(KB_DIR, "历史案例库.txt")
    if os.path.exists(case_path):
        with open(case_path, encoding="utf-8") as f:
            case_samples = parse_cases(f.read())
        samples.extend(case_samples)
        print(f"从历史案例库抽取 {len(case_samples)} 条样本")

    # 来源2 + 3：知识问答 + 诊断范式
    for user, assistant in KNOWLEDGE_QA + PARADIGM_QA:
        samples.append((user, assistant))
    print(f"知识问答 + 诊断范式 {len(KNOWLEDGE_QA) + len(PARADIGM_QA)} 条样本")

    # 写出 JSONL（百炼 SFT 格式）
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        for user, assistant in samples:
            record = {"messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
                {"role": "assistant", "content": assistant},
            ]}
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"\n✅ 数据集已生成：{OUT_PATH}")
    print(f"   共 {len(samples)} 条训练样本")
    print(f"   格式：百炼 SFT 标准 JSONL（messages 三元组）")
    print(f"   建议：训练集至少几十条，本数据集可直接上传百炼控制台微调")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    build_dataset()
