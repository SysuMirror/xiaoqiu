"""通用只读 SQL 查询工具：模型自写 SQL，查 xiaoqiu_ro 账号的两个公开投影视图。

安全分层（谁的边界归谁管）：
- DB 层（硬墙，真正的边界）：xiaoqiu_ro 零基表权限，仅 v_posts_public /
  v_pcomments_public 两个视图的 SELECT。userID 不在视图投影里、is_private 过滤
  在视图内强制、匿名帖作者在视图内固定为「匿名」——模型写任何 SQL 都绕不开
  （DDL 见 scripts/xiaoqiu_ro_db_20261009.sql）。
- 工具层（软墙，只为了报错友好和省上下文）：单语句、SELECT/WITH 开头、
  危险词黑名单（已剥字符串字面量防误伤）、行数预算截断、MAX_EXECUTION_TIME
  注入、UGC 注入扫描。这层全部被绕过也只会得到 MySQL 的 permission denied。

行数用 fetchmany(limit+1) 探测截断；发生截断时连接内有未读结果包，直接
force_close 杀连接而不还池（还池会污染下一个复用者）。
"""
from __future__ import annotations

import asyncio
import re

import aiomysql
from langchain_core.tools import tool

from app.config import get_config
from app.tools.forum.sql_templates import _to_tsv, _ugc_footer

_pool_ro: aiomysql.Pool | None = None
_ro_lock: asyncio.Lock | None = None

_SELECT_RE = re.compile(r"^(select|with)\b", re.IGNORECASE)
# 软墙黑名单：对剥掉字符串字面量后的 SQL 生效，防 LIKE '%update%' 之类误伤
_FORBIDDEN_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|rename|grant|revoke|"
    r"set|call|load_file|load|outfile|infile|handler|shutdown|kill|flush|do|"
    r"information_schema|performance_schema|mysql\.)\b",
    re.IGNORECASE,
)
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


def validate_sql(sql: str) -> str:
    """返回错误文案；空串=通过。纯函数，便于单测。"""
    s = (sql or "").strip()
    if not s:
        return "错误：SQL 为空"
    body = s.rstrip(";").strip()
    if ";" in body:
        return "错误：一次只允许一条语句"
    if not _SELECT_RE.match(body):
        return "错误：只支持 SELECT 只读查询（目标视图：v_posts_public / v_pcomments_public）"
    if _FORBIDDEN_RE.search(_QUOTED_RE.sub("''", body)):
        return "错误：查询包含不允许的关键字（只能对两个公开视图做只读查询）"
    return ""


def clamp_limit(limit: int) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = 20
    return max(1, min(n, get_config().forum_db.max_rows))


def inject_timeout_hint(sql: str) -> str:
    """把 MAX_EXECUTION_TIME hint 放到 SELECT 关键字之后（MySQL 要求 hint 紧跟
    语句关键字，写在前面的 /*+ */ 只是普通注释不生效）。WITH 开头的查询不注入
    （稀少路径，靠视图小表 + 行数预算控风险）。"""
    ms = get_config().forum_db.statement_timeout_seconds * 1000
    return re.sub(
        r"^(\s*)(select)\b",
        rf"\1\2 /*+ MAX_EXECUTION_TIME({ms}) */",
        sql,
        count=1,
        flags=re.IGNORECASE,
    )


async def _get_pool() -> aiomysql.Pool:
    global _pool_ro, _ro_lock
    if _pool_ro is None:
        if _ro_lock is None:
            _ro_lock = asyncio.Lock()
        async with _ro_lock:
            if _pool_ro is None:
                cfg = get_config()
                sec = cfg.forum_db_secrets
                if not (sec.ro_user and sec.ro_password):
                    raise RuntimeError("论坛只读账号未配置（FORUM_DB_RO_*）")
                _pool_ro = await aiomysql.create_pool(
                    host=cfg.forum_db.host,
                    port=cfg.forum_db.port,
                    user=sec.ro_user,
                    password=sec.ro_password,
                    db=cfg.forum_db.database,
                    minsize=1,
                    maxsize=2,
                    autocommit=True,
                    connect_timeout=5,
                )
    return _pool_ro


async def _run(sql: str, n: int) -> tuple[list[str], list[list], bool]:
    """执行并取回最多 n+1 行探测截断。返回 (cols, rows, truncated)。"""
    pool = await _get_pool()
    conn = await pool.acquire()
    truncated = False
    try:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = list(await cur.fetchmany(n + 1))
            truncated = len(rows) > n
    finally:
        if truncated:
            conn.close()  # 未读结果包不能还池；aiomysql 无 abort，close() 即杀传输层
        pool.release(conn)
    if truncated:
        rows = rows[:n]
    return cols, rows, truncated


@tool
async def sql_read(sql: str, limit: int = 20) -> str:
    """对论坛公开数据执行只读 SQL 查询，自己写 SQL，只能查两个视图：
    v_posts_public（帖子公开投影：postID/`partition`/title/ptext/post_time/
    browse_num/like_num/dislike_num/comment_num/vote_num/heat/tag/post_kind/
    is_high_quality/is_pinned/is_anonymous/photos/author_alias）
    v_pcomments_public（评论公开投影：pcommentID/ptargetID/pctext/time/
    like_num/deny_num/author_alias）
    支持任意 SELECT：时间范围、组合条件、排序、GROUP BY 统计都行。
    author_alias 是脱敏哈希别名，匿名帖固定为「匿名」，不要尝试还原身份；
    私密帖不在视图里，查不到要如实说。大结果集会被截断，用时间条件分页取全。

    Args:
        sql: 一条 SELECT 查询语句（只查上述两个视图，partition 是保留字要加反引号）
        limit: 最多返回行数 1-50，默认 20
    """
    err = validate_sql(sql)
    if err:
        return err
    n = clamp_limit(limit)
    exec_sql = inject_timeout_hint(sql.strip().rstrip(";").strip())
    try:
        cols, rows, truncated = await _run(exec_sql, n)
    except aiomysql.Error as e:
        return (f"SQL 执行失败：{str(e)[:200]}。请修正后重试"
                "（只能查 v_posts_public / v_pcomments_public 两个视图）")
    except RuntimeError as e:
        return f"只读查询暂不可用：{e}"
    except Exception as e:  # noqa: BLE001
        return f"SQL 执行失败：{str(e)[:200]}"
    if not rows:
        return "查询成功，共 0 行（私密帖不在公开视图里，查不到属正常）"
    out = _to_tsv(cols, rows) + _ugc_footer(rows, tuple(range(len(cols))))
    cut = "，已达单次行数上限被截断——取全量请用 WHERE post_time > '本批最晚时间' 分页继续查" if truncated else ""
    return f"{out}\n（{len(rows)} 行{cut}）"


SQL_READ_TOOLS = [sql_read]
