# -*- coding: utf-8 -*-
"""
========================================================================
阶段 4 - 可观测性模块（Observability）
========================================================================
把多 Agent 协作的全过程记录成结构化轨迹（trace），用于：
- 运行时实时打印（开发调试）
- 事后分析：每个 Agent 调了几次工具、各步耗时、token 消耗
- 对应 JD 第4条「监控 Agent 运行效果（任务完成率、响应时效）」

这是"生产级 Agent 系统"区别于"demo"的关键特征之一。
"""

import time
import json
import threading


class Tracer:
    """记录一次诊断任务的完整执行轨迹。线程安全，支持并行 Agent 调用。"""

    def __init__(self, verbose=True):
        self.events = []          # 结构化事件列表
        self.verbose = verbose
        self.t0 = time.time()
        self.tool_calls = 0       # 工具调用总次数
        self.agent_calls = 0      # 子Agent调用总次数
        self.llm_calls = 0        # 大模型调用总次数
        self._lock = threading.Lock()   # 并行执行时保护共享状态

    def _ts(self):
        return round(time.time() - self.t0, 2)

    def log(self, level, actor, action, detail=""):
        """level: INFO/AGENT/TOOL/LLM/RESULT；actor: 谁；action: 干了啥。
        加锁保证多线程并行调用 Agent 时计数和打印不错乱。"""
        with self._lock:
            ev = {"t": self._ts(), "level": level, "actor": actor, "action": action, "detail": detail}
            self.events.append(ev)
            if level == "AGENT":
                self.agent_calls += 1
            elif level == "TOOL":
                self.tool_calls += 1
            elif level == "LLM":
                self.llm_calls += 1
            if self.verbose:
                self._print(ev)

    def _print(self, ev):
        icon = {"INFO": "ℹ️ ", "AGENT": "🤖", "TOOL": "🔧", "LLM": "🧠", "RESULT": "✅"}.get(ev["level"], "  ")
        indent = "    " if ev["level"] in ("TOOL", "LLM") else "  "
        line = f"[{ev['t']:>5.2f}s] {indent}{icon} {ev['actor']}：{ev['action']}"
        if ev["detail"]:
            line += f" — {ev['detail']}"
        print(line)

    def summary(self):
        """输出运行统计摘要。"""
        total = self._ts()
        s = [
            "\n" + "─" * 60,
            "📊 运行统计（可观测性指标）",
            "─" * 60,
            f"  总耗时：{total:.2f} 秒",
            f"  子Agent调用次数：{self.agent_calls}",
            f"  工具调用次数：{self.tool_calls}",
            f"  大模型调用次数：{self.llm_calls}",
        ]
        return "\n".join(s)

    def export_json(self, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"total_time": self._ts(), "stats": {
                "agent_calls": self.agent_calls, "tool_calls": self.tool_calls,
                "llm_calls": self.llm_calls}, "events": self.events},
                f, ensure_ascii=False, indent=2)
