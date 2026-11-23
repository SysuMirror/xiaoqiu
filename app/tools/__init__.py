"""工具集中装配：所有工具注册进 TOOL_FUNCS，registry 提供权限过滤。"""
from __future__ import annotations

from app.tools.chat.kb_search import kb_search
from app.tools.chat.memory_tools import list_recent_actions, memory_save
from app.tools.chat.play_rps import play_rps
from app.tools.chat.web_search import web_search
from app.tools.forum.read_ext import FORUM_READ_TOOLS
from app.tools.forum.sql_read import SQL_READ_TOOLS
from app.tools.forum.sql_templates import FORUM_TOOLS
from app.tools.forum.write import FORUM_WRITE_TOOLS
from app.tools.notify.feedback import submit_feedback

TOOL_FUNCS = {t.name: t for t in [
    *FORUM_TOOLS, *FORUM_READ_TOOLS, *SQL_READ_TOOLS, *FORUM_WRITE_TOOLS,
    memory_save, list_recent_actions, kb_search, web_search, submit_feedback, play_rps,
]}
