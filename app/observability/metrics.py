"""轻量指标：手写 Prometheus 文本格式（零依赖，单实例 MVP 够用）。

计数器：runs/llm/embed/kb/tool 调用与失败
仪表盘配套：Grafana 抓 http://<host>:8100/metrics（仅内网，nginx 不路由）
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict

TYPE_HINT: dict[str, str] = {
    "guard_mail_total": "counter",
    "agent_llm_calls_total_value": "gauge",  # ledger 投影非单调重置安全
}
_lock = threading.Lock()
_counters: dict[str, float] = defaultdict(float)   # name{label=k,v} -> value
_gauges: dict[str, float] = defaultdict(float)
_created: dict[str, float] = {}                     # name -> ts
# R12 开放标准：HELP 文档注册表（Prometheus 文本格式规范第二要素）
HELP: dict[str, str] = {
    "agent_llm_calls_total_value": "论坛 agent LLM 总调用数（pool ledger 投影）",
    "agent_llm_tokens_prompt": "论坛 agent LLM prompt token 累计",
    "agent_llm_tokens_completion": "论坛 agent LLM completion token 累计",
    "agent_llm_run_tokens_total": "逐 run 累计 token（v3 成本归因，按模型）",
    "agent_first_token_seconds_total": "首 token 时延累计秒（v3 SLO，按模型）",
    "agent_first_token_count": "首 token 观测次数（v3 SLO，按模型）",
    "agent_forum_writes_total": "论坛写侧工具执行计数（success/rejected/failed，按工具）",
    "agent_injection_hits_total": "UGC/检索内容注入特征命中计数（按来源）",
    "agent_degrade_total": "降级计数（kb_fallback 等，按类别）",
    "agent_llm_breaker_open": "LLM 端点断路器状态（1=open）",
    "agent_runs_active": "活跃 run 数（论坛 agent）",
    "guard_mail_total": "守卫邮件发送计数（sent/failed）",
}


def inc(name: str, labels: dict[str, str] | None = None, amount: float = 1) -> None:
    key = name if not labels else name + "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
    with _lock:
        _counters[key] += amount
        _created.setdefault(key, time.time())


def gauge(name: str, value: float, labels: dict[str, str] | None = None) -> None:
    key = name if not labels else name + "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
    with _lock:
        _gauges[name if not labels else key] = value
        # label 化的 gauge 存进同 dict，导出时按 name 归组
        _gauges[key] = value


def observe_seconds(name: str, labels: dict[str, str] | None = None) -> _Timer:
    return _Timer(name, labels)


class _Timer:
    def __init__(self, name: str, labels: dict[str, str] | None):
        self._name, self._labels, self._t0 = name, labels, time.monotonic()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        secs = time.monotonic() - self._t0
        # 简化为计数 + 累计秒（histogram 留给 prometheus_client 时代）
        inc(self._name + "_seconds_total", self._labels, secs)
        inc(self._name + "_count", self._labels, 1)
        return False


def expose() -> str:
    lines: list[str] = []
    with _lock:
        all_keys = list(_counters.keys()) + list(_gauges.keys())
        names = sorted({k.split("{")[0] for k in all_keys})
        for n in names:
            if n in HELP:
                lines.append(f"# HELP {n} {HELP[n]}")
        # R12: 注册过的 HELP 即使暂无样本也输出（采集端 schema 稳定）
        joined = "\n".join(lines)
        for n, h in sorted(HELP.items()):
            if f"# HELP {n} " not in joined:
                lines.append(f"# HELP {n} {h}")
                lines.append(f"# TYPE {n} {TYPE_HINT.get(n, 'gauge')}")
            if n in _counters or any(k.startswith(n + "{") for k in _counters):
                lines.append(f"# TYPE {n} counter")
                for k in sorted(_counters):
                    if k == n or k.startswith(n + "{"):
                        lines.append(f"{k} {_counters[k]:.1f}")
            if n in _gauges or any(k.startswith(n + "{") for k in _gauges):
                lines.append(f"# TYPE {n} gauge")
                for k in sorted(_gauges):
                    if k == n or k.startswith(n + "{"):
                        lines.append(f"{k} {_gauges[k]:.1f}")
    return "\n".join(lines) + "\n"
