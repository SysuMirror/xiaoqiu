"""工具注册表：单文件纯 dict（ADR：集中可审计），build_graph 按 role 过滤注入。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Risk = Literal["L1", "L2", "L3"]
Role = Literal["user", "admin"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    domain: str  # chat | ops
    risk: Risk
    min_role: Role


# 唯一事实来源。文档中的权限矩阵必须与此一致（tests/test_registry.py 校验）。
TOOL_REGISTRY: dict[str, ToolSpec] = {
    # ---- chat 域（普通用户可用）----
    "sql_post_search": ToolSpec("sql_post_search", "chat", "L1", "user"),
    "sql_latest_posts": ToolSpec("sql_latest_posts", "chat", "L1", "user"),
    "sql_post_stats": ToolSpec("sql_post_stats", "chat", "L1", "user"),
    "sql_post_comments": ToolSpec("sql_post_comments", "chat", "L1", "user"),
    "sql_my_posts": ToolSpec("sql_my_posts", "chat", "L1", "user"),
    "sql_hot_posts": ToolSpec("sql_hot_posts", "chat", "L1", "user"),
    "sql_board_stats": ToolSpec("sql_board_stats", "chat", "L1", "user"),
    # 通用只读 SQL：安全边界在 DB 层（xiaoqiu_ro 仅两个公开视图的 SELECT），
    # 工具层只是友好报错 + 行数预算，L1
    "sql_read": ToolSpec("sql_read", "chat", "L1", "user"),
    "memory_save": ToolSpec("memory_save", "chat", "L1", "user"),
    "list_recent_actions": ToolSpec("list_recent_actions", "chat", "L1", "user"),
    "kb_search": ToolSpec("kb_search", "chat", "L1", "user"),
    "web_search": ToolSpec("web_search", "chat", "L1", "user"),
    # L2 = 对外副作用（发一封反馈邮件），每小时限 1 条 + 台账，无需 HITL 审批
    "submit_feedback": ToolSpec("submit_feedback", "chat", "L2", "user"),
    # 猜拳：副作用是用户自愿的自身封禁（执行对象=JWT 本人），L2 不设审批
    "play_rps": ToolSpec("play_rps", "chat", "L2", "user"),
    # ---- 论坛写侧（v3）：L3 强制人审（用户回复「同意」才执行）；点赞/投票可逆走 L2 ----
    "create_post": ToolSpec("create_post", "chat", "L3", "user"),
    "create_comment": ToolSpec("create_comment", "chat", "L3", "user"),
    "toggle_like_post": ToolSpec("toggle_like_post", "chat", "L2", "user"),
    "toggle_vote_post": ToolSpec("toggle_vote_post", "chat", "L2", "user"),
    # 只保留 chat 域工具；运维动作已收敛到 Skein。
}

ROLE_ALLOWED_DOMAINS: dict[Role, set[str]] = {
    "user": {"chat"},
    "admin": {"chat"},
}

RISK_FORCE_APPROVE = {"L3"}


def tools_for_role(role: Role) -> list[str]:
    """角色可见工具名列表：域白名单 × 最低角色。未授权工具不进入上下文。"""
    domains = ROLE_ALLOWED_DOMAINS[role]
    return [
        spec.name
        for spec in TOOL_REGISTRY.values()
        if spec.domain in domains
        and (role == "admin" or spec.min_role == "user")
    ]
