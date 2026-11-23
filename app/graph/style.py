"""回复风格处理：emoji 过滤（线上反馈：表情过多，要求文字化表达）。"""
from __future__ import annotations

import re

# 常见 emoji 区段：符号/表情/辅助平面/旗帜/变体选择符/钉板符号
EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # 全部 SMP emoji 块（含扩展）
    "\U00002600-\U000027BF"  # 杂项符号 + 钉板符号（✅⚠️❌✂✅ 等）
    "\U0001F1E6-\U0001F1FF"  # 区域指示符（国旗）
    "\U00002B00-\U00002BFF"  # ⬆⭐⭕ 等
    "\U0000FE0F"             # 变体选择符
    "\U000020E3"             # 组合键帽
    "]+",
    flags=re.UNICODE,
)


def strip_emoji(text: str) -> str:
    if not text:
        return text
    cleaned = EMOJI_RE.sub("", text)
    # 清理过滤后残留的多余空格（保留换行结构）
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned
