"""论坛读侧扩展工具（v3）：评论 / 我的帖子 / 热帖 / 板块热度。

沿用 sql_templates 的安全基线：模板化参数化 SQL、MAX_EXECUTION_TIME hint、
强制 LIMIT、TSV 输出、userID SHA256 别名；私密帖对所有角色不可见。
sql_my_posts 通过 config 注入身份查用户自己的帖子（含私密状态标记——本人可见自己数据）。
"""
from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.tools.forum.sql_templates import (
    PRIVATE_FILTER,
    _query,
    _to_tsv,
    _user_col_expr,
)

_POST_LINK = "提示：引用帖子请用 [标题](https://ssemarket.cn/new/postdetail/<postID>) 格式"


@tool
async def sql_post_comments(post_id: int, limit: int = 10) -> str:
    """查看指定帖子的评论列表（内容/时间/点赞数），作者以脱敏别名显示。

    Args:
        post_id: 帖子 postID（数字）
        limit: 返回条数 1-30，默认 10
    """
    try:
        post_id = int(post_id)
    except (TypeError, ValueError):
        return "错误：postID 必须是数字"
    if post_id <= 0:
        return "错误：postID 无效"
    n = max(1, min(int(limit), 30))
    sql = (
        "/*T*/ SELECT c.pcommentID, SHA2(CONCAT(c.userID), 256) AS author_alias,"
        " c.pctext, c.time, c.like_num"
        " FROM pcomments c JOIN posts p ON p.postID = c.ptargetID"
        " WHERE c.ptargetID = %s AND p.is_private = 0"
        " ORDER BY c.time DESC LIMIT " + str(n)
    )
    cols, rows = await _query(sql, (post_id,))
    if not rows:
        return f"帖子 {post_id} 暂无评论（或帖子不存在/不可见）"
    from app.tools.forum.sql_templates import _ugc_footer

    return _to_tsv(cols, rows) + _ugc_footer(rows, (2,))


@tool
async def sql_my_posts(limit: int = 10, config: RunnableConfig = None) -> str:
    """查看当前用户自己发过的帖子（含私密状态），用于"我发过哪些帖子"类问题。

    Args:
        limit: 返回条数 1-20，默认 10
    """
    from hashlib import sha256

    cfg_ctx = (config or {}).get("configurable") or {}
    user_id = str(cfg_ctx.get("user_id", ""))
    raw = user_id[1:] if user_id[:1] in ("u", "a") else ""
    if not raw.isdigit():
        return "错误：无法识别用户身份"
    uid = int(raw)
    n = max(1, min(int(limit), 20))
    alias = sha256(str(uid).encode()).hexdigest()[:12]
    sql = (
        "/*T*/ SELECT p.postID, p.title, p.post_time, p.browse_num, p.like_num,"
        " p.comment_num, IFNULL(p.is_private, 0) AS is_private"
        " FROM posts p WHERE p.userID = %s"
        " ORDER BY p.post_time DESC LIMIT " + str(n)
    )
    cols, rows = await _query(sql, (uid,))
    if not rows:
        return "你在论坛还没有发过帖子"
    out = _to_tsv(cols, rows)
    return f"（你的作者别名：{alias}）\n" + out


@tool
async def sql_hot_posts(limit: int = 10, days: int = 7) -> str:
    """查看近期热门帖子（按热度 heat 排序，默认最近 7 天）。

    Args:
        limit: 返回条数 1-20，默认 10
        days: 时间窗口天数 1-30，默认 7
    """
    n = max(1, min(int(limit), 20))
    d = max(1, min(int(days), 30))
    sql = (
        "/*T*/ SELECT p.postID, p.title, "
        + _user_col_expr("p")
        + " AS author_alias, p.post_time, p.browse_num, p.like_num,"
        " ROUND(p.heat) AS heat"
        " FROM posts p WHERE 1=1" + PRIVATE_FILTER +
        f" AND p.post_time >= NOW() - INTERVAL {int(d)} DAY"
        " ORDER BY p.heat DESC LIMIT " + str(n)
    )
    cols, rows = await _query(sql, ())
    if not rows:
        return "该时间窗口内暂无帖子"
    from app.tools.forum.sql_templates import _ugc_footer

    return _to_tsv(cols, rows) + _ugc_footer(rows, (1,)) + "\n" + _POST_LINK


@tool
async def sql_board_stats() -> str:
    """查看论坛各分区的帖子数量、总浏览量与平均热度（板块热度概览）。"""
    sql = (
        "/*T*/ SELECT p.`partition`, COUNT(*) AS posts, IFNULL(SUM(p.browse_num),0) AS total_browse,"
        " ROUND(IFNULL(AVG(p.heat),0),1) AS avg_heat"
        " FROM posts p WHERE 1=1" + PRIVATE_FILTER +
        " GROUP BY p.`partition` ORDER BY posts DESC"
    )
    cols, rows = await _query(sql, ())
    if not rows:
        return "暂无数据"
    return _to_tsv(cols, rows)


FORUM_READ_TOOLS = [sql_post_comments, sql_my_posts, sql_hot_posts, sql_board_stats]
