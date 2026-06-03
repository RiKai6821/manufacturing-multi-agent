# -*- coding: utf-8 -*-
"""
多智能体系统评估框架（Agent-level Evaluation）
========================================================================
对应 JD 职责4：「监控 Agent 运行效果（任务完成率、响应时效）」。

区别于 rag_eval.py（只评估RAG检索质量），本框架评估【整个多Agent诊断系统】：
  - 根因命中率：报告是否识别出预期的根本原因
  - 工单生成率：该建工单的场景是否真的建了
  - 数字一致率：报告关键数字是否通过防幻觉核对（与DB一致）
  - 任务完成率：以上全部通过的场景比例
  - 响应时效：每个场景的端到端耗时，及平均值

用法：
  python eval_agent.py            # 跑全部场景
  python eval_agent.py --quick    # 只跑前2个场景（省时省额度，验证用）

产出：控制台报告 + eval_report.json
"""

import os
import sys
import time
import json
import io
import re
from contextlib import redirect_stdout

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.append(os.path.join(os.path.dirname(__file__), "agents"))
sys.path.append(os.path.join(os.path.dirname(__file__), "tools"))

import config  # noqa
from settings import settings
from logger_config import get_logger

logger = get_logger(__name__)

# 评估时关闭实时轨迹打印，让评估输出干净
settings.verbose = False

from main import diagnose            # noqa: E402
from fact_checker import verify_report  # noqa: E402


# ════════════════════════════════════════════════════════════
# 诊断场景测试集（标注预期，用于自动判分）
#   expect_root_keywords : 报告中应出现的根因关键词（全部命中才算根因正确）
#   expect_work_order    : 是否应生成工单
#   expect_facts_ok      : 是否应通过数字一致性核对
# ════════════════════════════════════════════════════════════

# 判分约定：
#   expect_work_order: True=必须建单 / False=必须不建 / None=不检查（行为合理即可）
#   expect_facts_ok:   True=必须数字全一致 / False=不强制（正常设备无突出异常数字）
SCENARIOS = [
    {
        "id": "S1-EQP03-颗粒污染异常",
        "equipment_id": "EQP-03",
        "request": "3号机台 EQP-03 这批晶圆良率掉到 88% 了，帮我分析原因并处理。",
        "expect_root_keywords": ["保养", "颗粒"],   # 核心诊断：必须找出这两个根因要素
        "expect_work_order": True,                  # 明确异常，必须生成工单
        "expect_facts_ok": True,                    # 异常数字突出，必须核对一致
    },
    {
        "id": "S2-EQP05-保养临期咨询",
        "equipment_id": "EQP-05",
        "request": "5号离子注入机 EQP-05 状态怎么样？保养需要安排吗？",
        "expect_root_keywords": ["保养"],           # 应提及保养状态
        "expect_work_order": None,                  # 临期建预防性工单也合理，不强判
        "expect_facts_ok": False,                   # 正常设备，不强制数字核对
    },
    {
        "id": "S3-EQP07-电阻率下降",
        "equipment_id": "EQP-07",
        "request": "7号湿法清洗机 EQP-07 去离子水电阻率最近在下降，帮我看看。",
        "expect_root_keywords": ["电阻率"],         # 应识别电阻率问题
        "expect_work_order": None,
        "expect_facts_ok": False,
    },
    {
        "id": "S4-EQP02-正常设备查询",
        "equipment_id": "EQP-02",
        "request": "2号刻蚀机 EQP-02 最近良率正常吗？",
        "expect_root_keywords": [],                 # 正常设备，只测响应不崩
        "expect_work_order": None,
        "expect_facts_ok": False,
    },
]


# ════════════════════════════════════════════════════════════
# 单场景评估
# ════════════════════════════════════════════════════════════

def _parse_fact_check(equipment_id: str, report: str) -> tuple:
    """调用防幻觉核对，解析 'N/M 项一致'。返回 (一致数, 总数)。"""
    result = verify_report(equipment_id, report)
    m = re.search(r"(\d+)/(\d+)\s*项关键数字", result)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 0, 0


def eval_one(sc: dict) -> dict:
    """跑一个场景，返回评分明细。"""
    t0 = time.time()
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):       # 抑制 diagnose 的详细打印
            report = diagnose(sc["request"], sc["equipment_id"])
    except Exception as e:
        logger.error(f"{sc['id']} 诊断异常: {e}")
        return {"id": sc["id"], "error": str(e), "passed": False}
    elapsed = round(time.time() - t0, 1)

    # 1. 根因命中：所有预期关键词都出现在报告里
    hit_kws = [kw for kw in sc["expect_root_keywords"] if kw in report]
    root_ok = (len(hit_kws) == len(sc["expect_root_keywords"]))

    # 2. 工单决策：None=不检查；True=必须建单；False=必须不建单
    has_wo = bool(re.search(r"WO-\d+", report))
    if sc["expect_work_order"] is None:
        wo_ok = True
    else:
        wo_ok = (has_wo == sc["expect_work_order"])

    # 3. 数字一致性：仅异常场景强制（正常设备无突出异常数字，跳过）
    consistent, total = _parse_fact_check(sc["equipment_id"], report)
    facts_ok = (consistent == total and total > 0) if sc["expect_facts_ok"] else True

    passed = root_ok and wo_ok and facts_ok
    return {
        "id": sc["id"],
        "elapsed_s": elapsed,
        "root_ok": root_ok,
        "root_hit": f"{len(hit_kws)}/{len(sc['expect_root_keywords'])}",
        "work_order_ok": wo_ok,
        "facts": f"{consistent}/{total}",
        "facts_ok": facts_ok,
        "passed": passed,
        "report_len": len(report),
    }


# ════════════════════════════════════════════════════════════
# 批量评估 + 汇总
# ════════════════════════════════════════════════════════════

def run_eval(scenarios):
    print("=" * 70)
    print(f"多智能体系统评估开始（共 {len(scenarios)} 个场景）")
    print("=" * 70)

    results = []
    for sc in scenarios:
        print(f"\n▶ 评估场景 {sc['id']} …", flush=True)
        r = eval_one(sc)
        results.append(r)
        if "error" in r:
            print(f"   ❌ 异常：{r['error']}")
        else:
            mark = "✅通过" if r["passed"] else "❌未通过"
            print(f"   {mark} | 耗时{r['elapsed_s']}s | 根因{r['root_hit']} | "
                  f"工单{'✓' if r['work_order_ok'] else '✗'} | 数字{r['facts']}")

    # ── 汇总指标 ──
    ok = [r for r in results if r.get("passed")]
    valid = [r for r in results if "error" not in r]
    times = [r["elapsed_s"] for r in valid]

    print("\n" + "=" * 70)
    print("📊 评估汇总（对应 JD：任务完成率、响应时效）")
    print("=" * 70)
    print(f"  任务完成率：{len(ok)}/{len(scenarios)} = {len(ok)/len(scenarios):.0%}")
    if valid:
        root_pass = sum(1 for r in valid if r["root_ok"])
        wo_pass = sum(1 for r in valid if r["work_order_ok"])
        facts_pass = sum(1 for r in valid if r["facts_ok"])
        print(f"  根因命中率：{root_pass}/{len(valid)} = {root_pass/len(valid):.0%}")
        print(f"  工单决策正确率：{wo_pass}/{len(valid)} = {wo_pass/len(valid):.0%}")
        print(f"  数字一致率：{facts_pass}/{len(valid)} = {facts_pass/len(valid):.0%}")
        print(f"  平均响应时效：{sum(times)/len(times):.1f}s（最快{min(times)}s / 最慢{max(times)}s）")

    # 导出
    out = {"summary": {
        "total": len(scenarios),
        "passed": len(ok),
        "pass_rate": round(len(ok)/len(scenarios), 3),
        "avg_latency_s": round(sum(times)/len(times), 1) if times else 0,
    }, "details": results}
    out_path = os.path.join(os.path.dirname(__file__), "eval_report.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n报告已导出：{out_path}")
    return out


if __name__ == "__main__":
    if not os.getenv("DASHSCOPE_API_KEY"):
        raise SystemExit("请先设置 DASHSCOPE_API_KEY")
    scenarios = SCENARIOS[:2] if "--quick" in sys.argv else SCENARIOS
    run_eval(scenarios)
