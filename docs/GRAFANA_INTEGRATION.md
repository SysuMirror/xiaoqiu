# 小秋 Ops · Grafana 接入指南（R12 · 开放标准互操作）

agent 的 `/metrics` 端点输出 Prometheus 文本格式（含 HELP/TYPE），可被任何
Prometheus 兼容采集器消费。本指南给出最小接入路径与面板导入 JSON。

## 采集接入（Prometheus 或 VictoriaMetrics 均可）

```yaml
# prometheus.yml（或 vmagent -promscrape.config）
scrape_configs:
  - job_name: sse_agent_ops          # agent 自身指标（chat/告警/自检）
    metrics_path: /metrics
    static_configs:
      - targets: ['<agent-host>:8100']   # 服务器本机：127.0.0.1:8100
  - job_name: sse_go_backend         # Go 后端 RED（Bearer 守卫）
    metrics_path: /metrics
    authorization:
      credentials: <GO_METRICS_TOKEN>    # /root/SSE_Market/ops_tokens.env
    static_configs:
      - targets: ['127.0.0.1:8080']
```

注意：agent 8100 与 Go 8080 的 metrics 均仅本机回环可达（生产部署纪律），
采集器需与 agent 同机，或经 SSH 隧道转发。

## 核心 PromQL 速查

| 面板 | 查询 |
|---|---|
| 面板 AI 对话速率 | `rate(ops_chat_requests_total[5m])` |
| AI 对话 P50 延迟（近似均值） | `rate(ops_chat_seconds_total[5m]) / rate(ops_chat_requests_total[5m])` |
| LLM 断路器 | `max(agent_llm_breaker_open)` |
| 守卫事件 | `increase(ops_incidents_total[1h])` |
| 主机内存 | `host_mem_available_pct` |
| 主机磁盘 | `host_disk_used_pct` |
| Go 接口速率 | `sum by (route) (rate(http_request_duration_seconds_count[5m]))` |
| Go 接口错误率 | `sum by (route) (rate(http_request_duration_seconds_count{status=~"5.."}[5m])) / sum by (route) (rate(http_request_duration_seconds_count[5m]))` |
| 调度任务停跳告警 | `ops_job_stale_seconds > 180` |

## 最小面板 JSON（导入 Grafana → Dashboards → Import）

```json
{
  "title": "小秋 Ops 总览",
  "refresh": "30s",
  "panels": [
    {"type": "timeseries", "title": "AI 对话速率", "targets": [{"expr": "sum(rate(ops_chat_requests_total[5m])) by (endpoint)"}]},
    {"type": "stat", "title": "LLM 断路器", "targets": [{"expr": "max(agent_llm_breaker_open)"}], "fieldConfig": {"defaults": {"thresholds": {"steps": [{"color": "green", "value": null}, {"color": "red", "value": 1}]}}}},
    {"type": "stat", "title": "主机磁盘%", "targets": [{"expr": "host_disk_used_pct"}]},
    {"type": "timeseries", "title": "Go 接口速率 TOP", "targets": [{"expr": "topk(10, sum by (route) (rate(http_request_duration_seconds_count[5m])))"}]},
    {"type": "timeseries", "title": "Go 5xx 错误率", "targets": [{"expr": "sum(rate(http_request_duration_seconds_count{status=~\"5..\"}[5m])) by (route)"}]}
  ]
}
```

## 边界说明

- agent /metrics 仅本机回环（生产部署纪律：nginx 只路由 /api/v1/* 与 /ops/*）
- ops_chat_requests_total 等 counter 命名已带 `_total` 后缀（Prometheus 规范）
- histogram（p95/p99）暂未实现（Timer 简化为 count+sum）——升级路径：
  prometheus_client 库替换 expose()（单文件替换点，见 metrics.py）
