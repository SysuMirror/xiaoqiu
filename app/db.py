"""统一韧性 PG 池：连接类异常自动换血重试（根治 2026-10-11 PG 重启连环 50000）。

背景：进程内十余个独立 asyncpg 池（threads/ledger/moderation/...），PG 重启或
跨网掐线后，池内连接——尤其 is_closed()=False 的半开连接——被反复 acquire，
请求首个 DB 操作即失败报 50000；/health 探针不碰 DB 全绿，只能人肉重建 pod。

三层治理（本模块承担前两层，第三层是 main.py 的 /health/deep）：
1. command_timeout：半开连接上的等待有上界，不再无限挂死；
2. 换血重试：单语句操作遇连接类异常 → expire_connections() 强制整池过期
   （半开连接也会因 generation 失配在下次 acquire 被重建）→ 新连接重放一次；
3. deep_check()：注册表内每池真跑 SELECT 1，供 k8s liveness 池死自愈兜底。

边界：重试只覆盖单语句（execute/executemany/fetch/fetchrow/fetchval）；
transaction()/cursor() 等多语句序列不重放（不安全），异常穿透——retention
等事务路径自身有幂等重跑设计兜底。TimeoutError 不重试：慢查询超时重放有
重复执行风险，让它穿透。
"""
from __future__ import annotations

import asyncio
import logging

import asyncpg

log = logging.getLogger(__name__)

# 连接类异常：换新连接重放是安全的（语句不可能已在服务端生效过）。
STALE_ERRORS = (
    asyncpg.exceptions.PostgresConnectionError,  # 连接不存在/断开/被拒（asyncpg 0.30+ 族）
    asyncpg.exceptions.InvalidCachedStatementError,  # PG 重启后预编译语句失效
    ConnectionError,  # transport 层 ConnectionResetError 等
)

# 已初始化池注册表：deep_check（/health/deep）消费；close 时移除。
_REGISTRY: dict[str, "Pool"] = {}


async def create_pool(
    dsn: str,
    *,
    name: str,
    min_size: int = 1,
    max_size: int = 3,
    command_timeout: float | None = 15.0,
) -> "Pool":
    """创建韧性池并注册。retention 等含慢事务的路径传 command_timeout=None。"""
    pool = await asyncpg.create_pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        # 空闲连接主动回收：把跨网掐线后的陈旧连接存活窗口压到分钟级
        max_inactive_connection_lifetime=300.0,
        command_timeout=command_timeout,
    )
    wrapped = Pool(name, pool)
    _REGISTRY[name] = wrapped
    return wrapped


async def deep_check(timeout: float = 1.0) -> dict[str, str]:
    """/health/deep 后端：注册表内每池真跑 SELECT 1（自带一次换血重试）。

    并发探测 + 单池 1s 预算：k8s 探针渲染 timeoutSeconds=1，总耗时=最慢
    一池而非求和；半开挂死由 wait_for 兜住。
    返回 {池名: "ok" | "fail: 异常类型"}。
    """
    async def one(name: str, pool: "Pool") -> tuple[str, str]:
        try:
            await asyncio.wait_for(pool.fetchval("SELECT 1"), timeout)
            return name, "ok"
        except Exception as e:  # noqa: BLE001 —— 深检要的是全量结果不是首个异常
            return name, f"fail: {type(e).__name__}"

    pairs = await asyncio.gather(*(one(n, p) for n, p in list(_REGISTRY.items())))
    return dict(pairs)


def registry_names() -> list[str]:
    return list(_REGISTRY)


async def _release_quiet(proxy, owner: "Pool") -> None:
    """归还连接：吞常规异常；卡死（reset 无响应）时 terminate 强制释放。"""
    if proxy is None:
        return
    try:
        await asyncio.wait_for(proxy.release(), 10)
    except Exception:  # noqa: BLE001 —— 归还失败不掩盖业务异常
        try:
            proxy.terminate()
        except Exception:  # noqa: BLE001
            log.warning("conn terminate failed (pool=%s)", owner.name)


class Pool:
    """asyncpg.Pool 的韧性包装：单语句换血重试 + 深检注册。

    用法与 asyncpg.Pool 一致：便利方法（fetch/fetchrow/fetchval/execute/
    executemany）与 acquire() 上下文均可用；acquire 出的是 ConnProxy，
    其单语句调用同样带换血重试，事务/游标透传原生对象。
    """

    def __init__(self, name: str, pool: asyncpg.Pool):
        self.name = name
        self._pool = pool

    # ---- 单语句便利方法（换血重试） ----
    async def _stmt(self, method: str, sql: str, args: tuple, timeout=None):
        try:
            return await getattr(self._pool, method)(sql, *args, timeout=timeout)
        except STALE_ERRORS:
            await self._reap()
            return await getattr(self._pool, method)(sql, *args, timeout=timeout)

    async def execute(self, sql, *args, timeout=None):
        return await self._stmt("execute", sql, args, timeout)

    async def executemany(self, sql, args, *, timeout=None):
        # asyncpg 0.30+ executemany(sql, args Iterable)；重试语义同单语句
        return await self._stmt("executemany", sql, (args,), timeout)

    async def fetch(self, sql, *args, timeout=None):
        return await self._stmt("fetch", sql, args, timeout)

    async def fetchrow(self, sql, *args, timeout=None):
        return await self._stmt("fetchrow", sql, args, timeout)

    async def fetchval(self, sql, *args, timeout=None):
        return await self._stmt("fetchval", sql, args, timeout)

    async def _reap(self) -> None:
        """整池过期：半开连接（is_closed=False）也会因 generation 失配被强制重建。"""
        try:
            await self._pool.expire_connections()
        except Exception:  # noqa: BLE001 —— 换血失败不掩盖原异常，重试照常进行
            log.warning("expire_connections failed (pool=%s)", self.name)

    # ---- 生命周期 ----
    def acquire(self) -> "ConnProxy":
        """与 asyncpg 协议一致：async with pool.acquire() / conn = await pool.acquire() 均可。"""
        return ConnProxy(self)

    async def close(self) -> None:
        _REGISTRY.pop(self.name, None)
        await self._pool.close()

    def terminate(self) -> None:
        _REGISTRY.pop(self.name, None)
        self._pool.terminate()

    def __getattr__(self, item):
        # 其余（check/expire_connections/set_connect_args/...）透传原生池
        pool = self.__dict__.get("_pool")
        if pool is None:
            raise AttributeError(item)
        return getattr(pool, item)


class ConnProxy:
    """acquire() 出的连接代理：单语句换血重试；事务/游标透传原生对象。

    换血：release 归还坏连接（reset 失败时 asyncpg 会 terminate 它并抛 IO
    异常，吞掉）→ 整池过期 → 重新 acquire 拿全新连接。
    """

    def __init__(self, owner: Pool):
        self._owner = owner
        self._proxy = None  # asyncpg PoolConnectionProxy

    # await pool.acquire() 直取形态（async with 之外手动管理时用）
    def __await__(self):
        return self.__aenter__().__await__()

    async def release(self) -> None:
        """手动 acquire 形态的归还；async with 形态自动归还。

        归还带超时兜底：release 内部对死连接做 reset 时无超时（asyncpg 的
        timeout=None 路径），半死连接会无限等响应——超时即 terminate 强制
        释放 holder，防池容量泄漏（i49 实测）。
        """
        proxy, self._proxy = self._proxy, None
        await _release_quiet(proxy, self._owner)

    async def __aenter__(self) -> "ConnProxy":
        self._proxy = await self._owner._pool.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.release()

    async def _swap(self) -> None:
        """坏连接换血：归还旧的 → 整池过期 → 取新连接。"""
        proxy, self._proxy = self._proxy, None
        await _release_quiet(proxy, self._owner)
        await self._owner._reap()
        self._proxy = await self._owner._pool.acquire()

    async def _stmt(self, method: str, sql: str, args: tuple, timeout=None):
        if self._proxy is None:
            raise RuntimeError("ConnProxy 需在 async with pool.acquire() 内使用")
        try:
            return await getattr(self._proxy, method)(sql, *args, timeout=timeout)
        except STALE_ERRORS:
            await self._swap()
            return await getattr(self._proxy, method)(sql, *args, timeout=timeout)

    async def execute(self, sql, *args, timeout=None):
        return await self._stmt("execute", sql, args, timeout)

    async def executemany(self, sql, args, *, timeout=None):
        return await self._stmt("executemany", sql, (args,), timeout)

    async def fetch(self, sql, *args, timeout=None):
        return await self._stmt("fetch", sql, args, timeout)

    async def fetchrow(self, sql, *args, timeout=None):
        return await self._stmt("fetchrow", sql, args, timeout)

    async def fetchval(self, sql, *args, timeout=None):
        return await self._stmt("fetchval", sql, args, timeout)

    def __getattr__(self, item):
        # transaction()/cursor()/prepare()/is_closed/... 透传原生连接（不重试）
        proxy = self.__dict__.get("_proxy")
        if proxy is None:
            raise AttributeError(item)
        return getattr(proxy, item)
