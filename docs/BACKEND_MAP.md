# 后端架构地图（代码生成，勿手改）

> 来源：backend_map.yaml（gen_backend_map.py 从 routes.go/main.go/cache/config.go 生成）。
> 图：/ops/view；机器可读：/ops/topology；单模块上下文：/ops/context?module=<id>。

## 请求的旅程

客户端 → nginx → 中间件管道 → 业务模块 → 分层 → 数据/外部依赖

## 中间件管道（顺序即注册序）

- **RequestID** 
- **AttachRequestIDToSpan** 
- **PrometheusMetrics** 
- **LoggerToFile** 
- **CORSMiddleware** 
- **Auth(Strict/Optional/OAuth) + BloomFilter + 登录限流** · 路由组级挂载（routes.go：auth=Strict+Bloom / noauth=Optional / oauth=OAuth）

## 业务模块

| 模块 | 域 | 代码 | 路由数 | 代表性路由（→ 处理函数） |
|---|---|---|---|---|
| api | 工作台/平台 | `api/api.go` | 1 | `POST /api/auth/apiTest` → ApiTest |
| chat | 社区 | `controller/chat.go` | 5 | `GET /websocket/auth/chat` → ChatHandler<br>`GET /api/auth/leaveChatView` → LeaveChatView<br>`GET /api/auth/getChatHistory` → GetChatHistory |
| commentController | 评论 | `controller/commentController.go` | 10 | `POST api/auth/showPcomments` → GetComments<br>`POST api/auth/postPcomment` → PostPcomment<br>`POST api/auth/postCcomment` → PostCcomment |
| fileController | 文件 | `controller/fileController.go` | 6 | `POST /api/auth/getFileList` → GetFileList<br>`POST /api/auth/fileDelete` → DeleteFile<br>`POST /api/auth/folderDelete` → DeleteFolder |
| health | 平台与系统 | `controller/health.go` | 2 | `GET /health` → Health<br>`GET /ready` → Ready |
| internal_mail | 平台与系统 | `controller/internal_mail.go` | 1 | `POST /internal/mail` → InternalMail |
| lottery_controller | 运营活动 | `controller/lottery_controller.go` | 4 | `POST /api/auth/lottery/create` → LotteryCreate<br>`POST /api/auth/lottery/detail` → LotteryDetail<br>`POST /api/auth/lottery/participate` → LotteryParticipate |
| managerController | 管理端 | `controller/managerController.go` | 1 | `POST /api/auth/adminLogin` → AdminLogin |
| musicController | 音乐 | `controller/musicController.go` | 7 | `POST /api/auth/uploadMusic` → UploadMusic<br>`POST /api/auth/publishMusic` → PublishMusic<br>`POST /api/auth/getMusicList` → GetMusicList |
| noticeController | 公告 | `controller/noticeController.go` | 3 | `GET /api/auth/getNotice` → GetNotice<br>`GET /api/auth/getNoticeNum` → GetNoticeNum<br>`PATCH api/auth/readNotice/:noticeID` → ReadNotice |
| postController | 帖子 | `controller/postController.go` | 22 | `POST /api/auth/post` → Post<br>`POST /api/auth/browse` → Browse<br>`POST /api/auth/pinPost` → PinPost |
| quest_controller | 运营活动 | `controller/quest_controller.go` | 6 | `POST /api/auth/quest/list` → QuestList<br>`POST /api/auth/quest/detail` → QuestDetail<br>`POST /api/auth/quest/create` → QuestCreate |
| rating_controller | 运营活动 | `controller/rating_controller.go` | 12 | `POST /api/auth/rating/category/list` → RatingCategoryList<br>`POST /api/auth/rating/category/create` → RatingCategoryCreate<br>`POST /api/auth/rating/post/list` → RatingPostList |
| resumeController | 简历 | `controller/resumeController.go` | 8 | `POST /api/auth/resume/upload` → ResumeUpload<br>`POST /api/auth/resume/list` → ResumeList<br>`POST /api/auth/resume/detail` → ResumeDetail |
| shopController | 运营活动 | `controller/shopController.go` | 7 | `POST /api/auth/postProduct` → PostProduct<br>`POST /api/auth/getProducts` → GetProducts<br>`POST /api/auth/getProductDetail` → GetProductDetail |
| ssoController | 用户 | `controller/ssoController.go` | 8 | `GET /api/sso/login` → SsoLogin<br>`GET /api/sso/callback` → SsoCallback<br>`POST /api/sso/exchange` → SsoExchange |
| userController | 用户 | `controller/userController.go` | 21 | `POST /api/auth/connect/check` → CheckOAuth2App<br>`POST /api/auth/connect/authorize` → AuthorizeOAuth2App<br>`POST /api/auth/connect/token` → GetTokenbyOAuth2 |
| user_api_token | 用户 | `controller/user_api_token.go` | 5 | `GET /api/auth/token/scopes` → ListAPITokenScopes<br>`POST /api/auth/token/create` → CreateAPIToken<br>`GET /api/auth/token/list` → ListAPITokens |
| voteController | 运营活动 | `controller/voteController.go` | 6 | `POST /api/auth/searchPostsByTitle` → SearchPostsByTitle<br>`POST /api/auth/submitVote` → SubmitVote<br>`GET /api/auth/voteRanking` → GetVoteRanking |
| workbench_controller | 工作台/平台 | `controller/workbench_controller.go` | 1 | `GET /api/auth/workbench/apps` → WorkbenchAppPublicList |

## 分层与依赖

- **service 层** · 业务逻辑；缓存调用集中于此
- **repository 层** · gorm 数据访问唯一出口
- **model / domain** · 实体定义
- **cache（L1 内存 + L2 Redis）** · Cache-Aside；L1 逻辑过期可选，L2 Redis 物理 TTL(含 jitter)
- **mq（Redis 延迟队列）** · 帖子归档延迟 1h：ZSet 时间索引 + Hash payload；失败重试 3 次进死信
- nginx 反代（infra）
- Go 后端容器（infra）
- 论坛 MySQL（store）
- Redis（store）· 双角色：缓存 L2 + 归档延迟队列；不可用时缓存退化回源 MySQL、归档延迟
- 腾讯云 COS（external）· 音乐文件双写备份（本地盘为主）
- 归档消费端 server-task（external）· 独立 Python 服务（sse_market_toiwiki_server-task）：30s 消费 Redis 延迟队列
- 小秋（raghook）（external）· 发帖/删帖/转私密 webhook

## 缓存策略

| Profile | 保护域 | L1 | L2 | 逻辑过期 |
|---|---|---|---|---|
| post_detail | 帖子 | 5min | 30min | 是 |
| comment_list | 评论 | 3min | 15min | 否 |
| home_browse | 首页 | 1min | 5min | 否 |
| public_feed | 首页 | 1min | 5min | 是 |
| user_info | 用户 | 10min | 1h | 否 |
| hot_post_list | 首页 | 5min | 30min | 否 |

## 后台任务

- **heat.StartHeat** · 热度计算（定时） · 心跳：未接入
- **lottery.StartAutoDraw** · 抽奖自动开奖 · 心跳：未接入
- **pin.StartClearExpired** · 置顶过期清理 · 心跳：未接入

## 数据资产

| 存储 | 内容 | 备份 |
|---|---|---|
| MySQL ssemarket | 论坛核心表：posts/pcomments/users 等 | mysqldump 定时（main.go 内置备份逻辑） |
| 本地盘 sse_market_server_resource | 音乐文件/封面（主存储）+ uploads | bind-mount 目录，容器重建不丢 |
| 腾讯云 COS src/music/** | 音乐双写备份（2026-09 上线） | 对象存储版本化 |
| Agent PostgreSQL | 小秋状态：checkpoint/记忆/台账/遥测 | 每日 pg_dump |
| Redis | 缓存(L2)+归档延迟队列 | 可重建（非资产，标注） |
