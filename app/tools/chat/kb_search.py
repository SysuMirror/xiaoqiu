"""kb_search：知识检索工具（chat 域 L1）。优先统一 RAG 服务，不可用降级本机 qdrant 链路。强制引用溯源。"""
from __future__ import annotations

import logging

from langchain_core.tools import tool

from app.rag import store, unified

log = logging.getLogger(__name__)

_HEADER = "postID\t标题\t内容摘要"
_FOOTER = "引用格式：[标题](https://ssemarket.cn/new/postdetail/<postID>)"


def _title_of(content: str) -> str:
    for line in content.splitlines():
        if line.startswith("标题："):
            return line.removeprefix("标题：").strip()[:60]
    return ""


@tool
async def kb_search(query: str, top_k: int = 5) -> str:
    """在论坛知识库中做语义检索，返回最相关的帖子切片（含排序）。

    回答知识类问题（怎么做什么、经验、教程）时先用此工具检索；
    用户表达"语义上/意思相近/主题相关"的检索意图时（关键词未必原样出现），
    必须用本工具而不是关键词搜索；
    引用结果时必须附上帖子链接 [标题](https://ssemarket.cn/new/postdetail/<postID>)；
    检索无结果时，如实告知用户"知识库中没有找到"，不要编造。

    Args:
        query: 检索 query，用完整中文句子描述要找的内容
        top_k: 返回条数 1-5，默认 5
    """
    k = max(1, min(int(top_k), 5))
    hits: list[dict] = []
    try:
        hits = await unified.search_chunks(query, k)
    except Exception as e:  # noqa: BLE001 —— 统一服务不可用降级本机链路
        log.warning("kb_search unified service down, fallback to local qdrant: %s", e)
        from app.observability import metrics

        metrics.inc("agent_degrade_total", {"kind": "kb_fallback"})
        try:
            local = await store.search(query, k)
        except Exception:  # noqa: BLE001 —— embedding 也不可用时不阻断对话
            return "知识库检索服务暂不可用。可以先用关键词搜索工具查帖子。"
        if not local or all(h["score"] < 0.3 for h in local):
            return "知识库中没有找到相关内容。"
        from app.security.injection import annotate_untrusted

        lines = [_HEADER]
        for h in local:
            snippet = annotate_untrusted(
                h["content"].replace("\t", " ").replace("\n", " ")[:150], "kb_search_local")
            lines.append(f"{h['id'].split(':')[1]}\t{h['title']}\t{snippet}")
        return "\n".join(lines) + "\n" + _FOOTER

    if not hits:
        return "知识库中没有找到相关内容。"
    from app.security.injection import annotate_untrusted

    lines = [_HEADER]
    seen: set[str] = set()
    for h in hits:
        if not h["post_id"] or h["post_id"] in seen:
            continue
        seen.add(h["post_id"])
        title = _title_of(h["content"]) or f"帖子{h['post_id']}"
        snippet = annotate_untrusted(
            h["content"].replace("\t", " ").replace("\n", " ")[:180], "kb_search")
        lines.append(f"{h['post_id']}\t{title}\t{snippet}")
    if len(lines) == 1:
        return "知识库中没有找到相关内容。"
    return "\n".join(lines) + "\n" + _FOOTER
