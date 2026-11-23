"""权限矩阵一致性：代码注册表 <-> 文档预期（ARCHITECTURE §7 防漂移测试）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools import TOOL_FUNCS
from app.tools.registry import TOOL_REGISTRY, tools_for_role


def test_registry_covers_all_tools():
    assert set(TOOL_REGISTRY) == set(TOOL_FUNCS), (
        f"registry/tool mismatch: {set(TOOL_REGISTRY) ^ set(TOOL_FUNCS)}"
    )


def test_role_matrix():
    assert set(tools_for_role("user")) == {
        "sql_post_search", "sql_latest_posts", "sql_post_stats",
        "sql_post_comments", "sql_my_posts", "sql_hot_posts", "sql_board_stats",
        "sql_read",
        "memory_save", "list_recent_actions", "kb_search",
        "web_search", "submit_feedback",
        # 猜拳：用户自愿的自身封禁副作用，L2 不设审批
        "play_rps",
        # v3 论坛写侧：用户自操作，L3 强制人审（chat 域）
        "create_post", "create_comment", "toggle_like_post", "toggle_vote_post",
    }
    assert set(tools_for_role("admin")) == set(tools_for_role("user"))


def test_l3_force_approve_semantics():
    """L3 一律触发强制审批。ops 域 L3 仅 admin；chat 域 L3 是用户自操作审批
    （执行对象=JWT 用户本人，审批人=同一用户，不存在越权面）。"""
    for spec in TOOL_REGISTRY.values():
        if spec.risk != "L3":
            continue
        if spec.domain == "ops":
            assert spec.min_role == "admin", f"{spec.name} ops L3 must be admin"
        else:
            assert spec.domain == "chat", f"{spec.name} unexpected domain {spec.domain}"
