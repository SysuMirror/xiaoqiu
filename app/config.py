"""配置加载：config/settings.yaml 为行为配置唯一来源，env 只放 secrets。"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent


class EndpointCfg(BaseModel):
    name: str
    base_url: str
    model: str
    api_key_env: str
    extra_body: dict = {}


class LlmCfg(BaseModel):
    endpoints: list[EndpointCfg]
    request_timeout_seconds: int = 60
    retry_attempts: int = 3
    breaker_failure_threshold: int = 3
    breaker_cooldown_seconds: int = 60


class LimitsCfg(BaseModel):
    rate_limit_rps: float = 2
    max_concurrent_runs_per_user: int = 2
    run_token_budget: int = 60000
    sse_heartbeat_seconds: int = 15
    sse_disconnect_grace_seconds: int = 90


class DatabaseCfg(BaseModel):
    dsn_env: str = "AGENT_PG_DSN"


class ForumDbCfg(BaseModel):
    host: str
    port: int
    user_env: str
    password_env: str
    ro_user_env: str = "FORUM_DB_RO_USER"
    ro_password_env: str = "FORUM_DB_RO_PASSWORD"
    database: str
    statement_timeout_seconds: int = 3
    max_rows: int = 50


class ForumDbSecrets(BaseModel):
    user: str = ""
    password: str = ""
    ro_user: str = ""
    ro_password: str = ""


class MailCfg(BaseModel):
    mode: str = "log"  # log | smtp
    smtp_host: str = ""
    smtp_port: int = 465
    user_env: str = "SMTP_USER"
    pass_env: str = "SMTP_PASS"
    from_name: str = "小秋 · SSE Market 助手"
    recipients: dict[str, str] = {}


class RagCfg(BaseModel):
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key_env: str = "QDRANT_API_KEY"
    collection: str = "g3dfc5d61_ssemarket_rag"
    embed_base_url: str = "http://127.0.0.1:13012"
    embed_dim: int = 1024
    # 统一 RAG 服务（组内 group-rag；kb/search 只取切片）。生产就地说置 127.0.0.1:13019 走 frp 直通
    service_url: str = "https://rag.ssemarket.cn"
    service_enabled: bool = True
    service_timeout_seconds: float = 8.0
    service_category: str = "market"   # market 唯一有数据源；iwiki/college/turing/third_party 待接入
    # 用户记忆向量库（语义召回）；集合沿用组前缀规范
    memory_collection: str = "g3dfc5d61_xiaoqiu_memory"
    memory_recall_k: int = 5
    memory_recall_min_score: float = 0.35


class WebSearchCfg(BaseModel):
    enabled: bool = False           # 填入搜索 API key 后置 true
    provider: str = "baidu"         # baidu(千帆,免费1500次/月按天发放) | tavily(免费1000次/月)
    base_url: str = "https://qianfan.baidubce.com"
    api_key_env: str = "QIANFAN_API_KEY"
    timeout_seconds: int = 15
    per_user_daily: int = 10
    global_daily: int = 50          # 对齐千帆免费档（1500/月 按天发放）
    max_results: int = 5


class GoBackendCfg(BaseModel):
    """Go 后端容器名：docker.sock 动态解析基址的兜底。"""
    container_name: str = "server"


class OpsGuardCfg(BaseModel):
    """邮件出口：经 Go /internal/mail，凭据只在 Go 侧。"""
    email_to: str = "2215020897@qq.com"
    mail_url: str = "http://127.0.0.1:8080/internal/mail"
    mail_token_env: str = "INTERNAL_TOKEN"
    mail_enabled: bool = True


class OpsLlmEndpoint(BaseModel):
    """ops 面单模型腿：base_url/api_key_env 缺省时回落 ops_llm 顶层默认（智谱直连）。"""
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    extra_body: dict = Field(default_factory=dict)


class OpsLlmCfg(BaseModel):
    """chat 辅助模型（会话标题/记忆抽取）。models 条目=模型名或带独立端点的 dict。"""
    base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    api_key_env: str = "OPS_LLM_API_KEY"
    models: list[str | OpsLlmEndpoint] = ["glm-5.3-flash", "glm-4.5-flash"]
    timeout_seconds: int = 45
    max_tokens: int = 1024

    def resolved(self) -> list[OpsLlmEndpoint]:
        return [m if isinstance(m, OpsLlmEndpoint) else OpsLlmEndpoint(model=m)
                for m in self.models]

    def as_endpoints(self) -> list["EndpointCfg"]:
        """模型链 → 通用端点列表：喂 ModelPool，ops 面复用 chat 面同一套
        池化回落/熔断（同语言禁两份实现）。腿上缺省 base_url/key 回落顶层默认。"""
        return [EndpointCfg(
            name=m.model,
            base_url=(m.base_url or self.base_url).rstrip("/"),
            model=m.model,
            api_key_env=m.api_key_env or self.api_key_env,
            extra_body=m.extra_body,
        ) for m in self.resolved()]


class SchedulerCfg(BaseModel):
    digest_hour: int = 9
    digest_minute: int = 0


class ModerationCfg(BaseModel):
    """辱骂防御：词库粗筛+LLM 复核，24h 内 strike_threshold 次自动禁言+作废 refresh token。"""
    enabled: bool = True
    mute_hours: int = 12
    strike_window_hours: int = 24
    strike_threshold: int = 2
    max_bans_per_day: int = 10


class ForumWriteCfg(BaseModel):
    """论坛写侧（v3）：用户 JWT 转发 Go 写 API，L3 人审 + 每日限频 + 内容审核。
    enabled=False 时工具上下文不可用（图仍注入但工具一律拒绝执行）。"""
    enabled: bool = False
    base_url: str = "http://127.0.0.1:8080"   # 生产 Go 回环；本地不连
    max_title_chars: int = 30                  # Go controller.Post 同款限制
    max_content_chars: int = 5000
    partitions: list[str] = [
        "生活日常", "主页", "学习交流", "技术分享", "课程专区", "实习就业",
        "社团活动", "宿舍生活", "组团捞人", "期末资料", "其他",
    ]
    daily_post_limit: int = 3
    daily_comment_limit: int = 10
    daily_like_limit: int = 30
    daily_vote_limit: int = 30


class RetentionCfg(BaseModel):
    """数据生命周期（v3）：软删会话/历史 run/台账的保留期清理（每日任务，幂等）。"""
    enabled: bool = True
    deleted_threads_days: int = 30   # 软删会话 N 天后物理清除（含 checkpoints）
    runs_days: int = 90
    actions_days: int = 365


class OpenApiCfg(BaseModel):
    """能力输出（v3）：X-API-Key 鉴权的开放检索 API，供其他项目调用。"""
    enabled: bool = True
    per_minute: int = 30
    daily_default: int = 1000


class AutoMemoryCfg(BaseModel):
    """自动记忆抽取（v3）：run 结束后后台 LLM 抽取稳定事实写入记忆库。"""
    enabled: bool = True
    cooldown_minutes: int = 30       # 每用户两次抽取最小间隔
    min_answer_chars: int = 80       # 答案太短不抽
    max_items: int = 2


class AdminCfg(BaseModel):
    """admin 加固（v3）：非空时，主站 admin token 的 UserID/AdminID 必须在白名单。"""
    uid_allowlist: list[int] = []


class AppConfig(BaseModel):
    env: str = "dev"
    log_level: str = "INFO"
    timezone: str = "Asia/Shanghai"
    http_host: str = "0.0.0.0"
    http_port: int = 8100
    limits: LimitsCfg = Field(default_factory=LimitsCfg)
    llm: LlmCfg
    database: DatabaseCfg = Field(default_factory=DatabaseCfg)
    forum_db: ForumDbCfg
    mail: MailCfg = Field(default_factory=MailCfg)
    websearch: WebSearchCfg = Field(default_factory=WebSearchCfg)
    go_backend: GoBackendCfg = Field(default_factory=GoBackendCfg)
    ops_llm: OpsLlmCfg = Field(default_factory=OpsLlmCfg)
    ops_guard: OpsGuardCfg = Field(default_factory=OpsGuardCfg)
    scheduler: SchedulerCfg = Field(default_factory=SchedulerCfg)
    moderation: ModerationCfg = Field(default_factory=ModerationCfg)
    forum_write: ForumWriteCfg = Field(default_factory=ForumWriteCfg)
    retention: RetentionCfg = Field(default_factory=RetentionCfg)
    openapi: OpenApiCfg = Field(default_factory=OpenApiCfg)
    auto_memory: AutoMemoryCfg = Field(default_factory=AutoMemoryCfg)
    admin: AdminCfg = Field(default_factory=AdminCfg)
    rag: RagCfg = Field(default_factory=RagCfg)
    forum_db_secrets: ForumDbSecrets = Field(default_factory=ForumDbSecrets)

    @property
    def pg_dsn(self) -> str:
        return os.environ[self.database.dsn_env]

    @property
    def primary_endpoint(self) -> EndpointCfg:
        return self.llm.endpoints[0]


def _load_yaml() -> dict:
    with open(ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache
def get_config() -> AppConfig:
    raw = _load_yaml()
    app_raw = raw.get("app", {})
    http_raw = raw.get("http", {})
    forum_raw = dict(raw.get("forum_db", {}))
    secrets = ForumDbSecrets(
        user=os.environ.get(forum_raw.get("user_env", ""), ""),
        password=os.environ.get(forum_raw.get("password_env", ""), ""),
        ro_user=os.environ.get(
            forum_raw.get("ro_user_env", "FORUM_DB_RO_USER"), ""),
        ro_password=os.environ.get(
            forum_raw.get("ro_password_env", "FORUM_DB_RO_PASSWORD"), ""),
    )
    return AppConfig(
        env=app_raw.get("env", "dev"),
        log_level=app_raw.get("log_level", "INFO"),
        timezone=app_raw.get("timezone", "Asia/Shanghai"),
        http_host=http_raw.get("host", "0.0.0.0"),
        # XIAOQIU_PORT：分面部署端口覆盖（chat 面上 8110 等）；不设则 settings.yaml 为准
        http_port=int(os.environ.get("XIAOQIU_PORT") or http_raw.get("port", 8100)),
        limits=LimitsCfg(**raw.get("limits", {})),
        llm=LlmCfg(**raw.get("llm", {})),
        database=DatabaseCfg(**raw.get("database", {})),
        forum_db=ForumDbCfg(**forum_raw),
        ops_guard=OpsGuardCfg(**raw.get("ops_guard", {})),
        mail=MailCfg(**raw.get("mail", {})),
        websearch=WebSearchCfg(**raw.get("websearch", {})),
        go_backend=GoBackendCfg(**raw.get("go_backend", {})),
        ops_llm=OpsLlmCfg(**raw.get("ops_llm", {})),
        scheduler=SchedulerCfg(**raw.get("scheduler", {})),
        moderation=ModerationCfg(**raw.get("moderation", {})),
        forum_write=ForumWriteCfg(**raw.get("forum_write", {})),
        retention=RetentionCfg(**raw.get("retention", {})),
        openapi=OpenApiCfg(**raw.get("openapi", {})),
        auto_memory=AutoMemoryCfg(**raw.get("auto_memory", {})),
        admin=AdminCfg(**raw.get("admin", {})),
        rag=RagCfg(**raw.get("rag", {})),
        forum_db_secrets=secrets,
    )
