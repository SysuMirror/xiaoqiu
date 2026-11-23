"""Go 后端地址动态解析：容器重建丢失回环端口发布时的兜底（docker.sock inspect）。

背景（2026-09-24 生产发现）：Go `server` 容器经 video 系列镜像重建后 PortBindings={}
（丢 -p 127.0.0.1:8080:8080），宿主回环直连全断——ops 采集与写侧转发双双静默失败。
根治 = Go 侧重建容器恢复发布；本模块提供 agent 侧无侵入兜底：经 docker.sock 读容器
桥网络 IP，直连 http://<ip>:8080（同宿主路由可达，无需端口发布）。IP 漂移靠 TTL 重解析。
本地/无 docker.sock 环境返回 None，调用方回落配置地址。
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

TTL_SECONDS = 300
_cache: tuple[float, str | None] = (0.0, None)


async def resolve_go_base(container: str = "server", port: int = 8080) -> str | None:
    """解析 Go 容器桥 IP 基址；失败/非 Linux 环境返回 None（调用方回落配置）。"""
    now = time.monotonic()
    if now - _cache[0] < TTL_SECONDS:
        return _cache[1]
    ip = None
    try:
        import httpx

        transport = httpx.AsyncHTTPTransport(uds="/var/run/docker.sock")
        async with httpx.AsyncClient(transport=transport, timeout=3, trust_env=False) as hc:
            resp = await hc.get(f"http://docker/containers/{container}/json")
        if resp.status_code == 200:
            nets = resp.json().get("NetworkSettings", {}).get("Networks", {})
            for net in nets.values():
                if net.get("IPAddress"):
                    ip = str(net["IPAddress"])
                    break
    except Exception:  # noqa: BLE001 —— 无 sock/Windows 一律静默回落
        ip = None
    base = f"http://{ip}:{port}" if ip else None
    globals()["_cache"] = (now, base)
    if base:
        log.info("go base resolved via docker.sock: %s", base)
    return base
