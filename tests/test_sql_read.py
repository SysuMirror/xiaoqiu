"""sql_read 软墙校验：纯函数单测（DB 硬墙=权限拒绝，不在此测）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools.forum.sql_read import clamp_limit, inject_timeout_hint, validate_sql


def test_ok_select():
    assert validate_sql("SELECT postID FROM v_posts_public") == ""
    assert validate_sql("select 1") == ""
    assert validate_sql("  SELECT * FROM v_pcomments_public;  ") == ""


def test_ok_with_cte():
    assert validate_sql(
        "WITH t AS (SELECT postID FROM v_posts_public) SELECT * FROM t") == ""


def test_reject_empty():
    assert validate_sql("") != ""
    assert validate_sql("   ") != ""


def test_reject_multi_statement():
    assert validate_sql("SELECT 1; DROP TABLE x") != ""
    assert validate_sql("SELECT 1;SELECT 2") != ""


def test_reject_non_select():
    assert validate_sql("UPDATE v_posts_public SET title='x'") != ""
    assert validate_sql("DELETE FROM v_posts_public") != ""
    assert validate_sql("GRANT SELECT ON *.* TO x") != ""
    assert validate_sql("SHOW TABLES") != ""


def test_reject_danger_words_outside_quotes():
    assert validate_sql("SELECT * FROM information_schema.tables") != ""
    assert validate_sql("SELECT LOAD_FILE('/etc/passwd')") != ""


def test_quoted_keywords_not_flagged():
    # LIKE 里出现危险词是合法查询，不得误伤（黑名单只对字面量之外的 SQL 生效）
    assert validate_sql(
        "SELECT postID FROM v_posts_public WHERE title LIKE '%update%'") == ""
    assert validate_sql(
        "SELECT pctext FROM v_pcomments_public WHERE pctext LIKE '%set%'") == ""


def test_clamp_limit():
    assert clamp_limit(5) == 5
    assert clamp_limit(0) == 1
    assert clamp_limit(-3) == 1
    assert clamp_limit(9999) == 50  # settings max_rows
    assert clamp_limit("abc") == 20
    assert clamp_limit(None) == 20


def test_timeout_hint_after_select_keyword():
    out = inject_timeout_hint("SELECT postID FROM v_posts_public")
    assert out.startswith("SELECT /*+ MAX_EXECUTION_TIME(")
    assert out.count("/*+") == 1
    out2 = inject_timeout_hint("select 1")
    assert out2.startswith("select /*+ MAX_EXECUTION_TIME(")
    # WITH 开头不注入（hint 无法合法紧跟语句关键字）
    sql_w = "WITH t AS (SELECT 1) SELECT * FROM t"
    assert inject_timeout_hint(sql_w) == sql_w
