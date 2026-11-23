-- xiaoqiu 只读账号 + 公开投影视图（2026-10-09 已在生产执行,回滚=DROP USER xiaoqiu_ro@'%'; DROP VIEW 两个）
-- 密码占位:真值在服务器 /root/SSE_Market/ops_tokens.env 的 FORUM_DB_RO_*
-- 原则:零基表权限;身份列不出视图;私密帖在视图层强制过滤;匿名帖作者不可关联
CREATE USER IF NOT EXISTS 'xiaoqiu_ro'@'%' IDENTIFIED BY '<见 ops_tokens.env FORUM_DB_RO_PASSWORD>';

-- 帖子公开投影: userID 不出视图;匿名帖作者固定"匿名";私密帖整行不可见
CREATE OR REPLACE VIEW ssemarket.v_posts_public AS
SELECT
  p.postID,
  p.`partition`,
  p.title,
  p.ptext,
  p.post_time,
  p.browse_num,
  p.like_num,
  p.dislike_num,
  p.comment_num,
  p.vote_num,
  p.heat,
  p.tag,
  p.post_kind,
  p.is_high_quality,
  p.is_pinned,
  p.is_anonymous,
  p.photos,
  CASE WHEN p.is_anonymous = 1 THEN '匿名'
       WHEN p.userID IS NULL THEN '-'
       ELSE SHA2(CONCAT(p.userID), 256) END AS author_alias
FROM ssemarket.posts p
WHERE IFNULL(p.is_private, 0) = 0;

-- 评论公开投影: join 帖子过滤私密帖的评论;评论者一律哈希别名(与现行工具一致)
CREATE OR REPLACE VIEW ssemarket.v_pcomments_public AS
SELECT
  c.pcommentID,
  c.ptargetID,
  c.pctext,
  c.time,
  c.like_num,
  c.deny_num,
  SHA2(CONCAT(c.userID), 256) AS author_alias
FROM ssemarket.pcomments c
JOIN ssemarket.posts p ON p.postID = c.ptargetID
WHERE IFNULL(p.is_private, 0) = 0;

GRANT SELECT ON ssemarket.v_posts_public TO 'xiaoqiu_ro'@'%';
GRANT SELECT ON ssemarket.v_pcomments_public TO 'xiaoqiu_ro'@'%';
