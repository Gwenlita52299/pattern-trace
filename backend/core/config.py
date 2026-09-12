"""Pydantic Settings — backend-api-spec §1 core/config."""
from pydantic import field_validator
from pydantic_settings import BaseSettings

# 已知占位密钥（小写精确匹配）——出现即拒绝（issue #27）
_PLACEHOLDER_JWT_SECRETS = {
    "dev-secret", "change-me", "changeit", "secret", "password",
    "test-secret-for-ci-only", "migration-placeholder",
}


class Settings(BaseSettings):
    database_url: str = "postgresql://pt:pt@localhost:5432/patterntrace"
    redis_url: str = "redis://localhost:6379/0"

    # issue #27：JWT_SECRET 必填 + 强度校验，缺失/占位/弱熵一律启动失败——
    # 弱密钥意味着任何知道默认值的人可伪造任意用户/管理员的 JWT
    jwt_secret: str

    @field_validator("jwt_secret")
    @classmethod
    def _reject_weak_jwt_secret(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError(
                "JWT_SECRET must be at least 32 characters "
                f"(got {len(v)}); generate with: openssl rand -hex 32")
        lowered = v.lower()
        if lowered in _PLACEHOLDER_JWT_SECRETS or any(
                p in lowered for p in ("change-me", "placeholder", "dev-secret")):
            raise ValueError(
                "JWT_SECRET is a known placeholder value; generate a random "
                "secret with: openssl rand -hex 32")
        if len(set(v)) < 10:
            # 熵下限：64 个重复/近重复字符（如同一字符×N、短模式循环）
            # 字典空间过小，与弱密钥无本质区别
            raise ValueError(
                "JWT_SECRET has insufficient entropy (fewer than 10 "
                "distinct characters); use a random secret")
        return v
    access_token_expire_minutes: int = 15
    refresh_token_days: int = 7
    # issue #63：生产默认 deepseek（fail-fast：无 LLM_API_KEY 首次调用即明确报错）；
    # 本地推理走 llama.cpp（compose --profile local-llm），经 openai_compatible 接入
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-chat"
    llm_base_url: str = "https://api.deepseek.com"
    # LLM_API_KEY：云端 provider（DeepSeek/OpenAI 等）的密钥。
    # pydantic-settings 优先级是环境变量 > .env 文件——本地 .env 与
    # docker compose 透传的环境变量两条路都能到这里，providers 不再各自读 os.environ
    llm_api_key: str = ""
    cookie_secure: bool = False         # SEC-02：生产强制 Secure；本地 dev 豁免
    # CORS 显式白名单（CSV）。allow_credentials=True 时禁止 "*"（backend-api-spec），
    # 默认放行本地前端；生产经 CORS_ORIGINS 注入正式域名
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # 首个 admin 账号（backend-api-spec §users），空则跳过 seed
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""

    # rate limit / cost control (spec §3 analyze)
    anon_rate_per_min: int = 10
    anon_daily_quota: int = 500

    # 登录爆破防护（SEC-05）：失败按 IP 与账号双维度计数（issue #23），
    # 任一维度超限即 429；成功登录清零
    login_fail_limit: int = 10
    login_fail_window: int = 60
    # refresh 等认证路径的 IP 维度限流
    auth_rate_per_min: int = 30

    # ingest（阶段2）— bybit_rust 基线数据根目录（含 results/ 与 data/）
    lazarus_data_dir: str = "/Users/gwenlita/Documents/bybit_rust/golden/python"
    # 正样本子图数据源切换：golden = bybit_rust 基线 fixture（单文件
    # subgraph_*.parquet）；cluster_k7 = 真实聚类子图（相对 repo 根的
    # cluster_seed_dir 下按簇分目录，各含 nodes/edges/seeds parquet）
    lazarus_subgraph_source: str = "cluster_k7"
    cluster_seed_dir: str = "ingest/seed/patterns/cluster_k7"
    ingest_synth_positives: int = 1500   # playbook 语料规模；0 关闭
    ingest_synth_seed: int = 42          # 生成器随机种子（确定性/幂等）
    negative_ratio: int = 3              # 负:正（IG-05 允许 [2.5, 3.5]）
    embedding_provider: str = "stub"
    # openai_compat（OpenRouter / SiliconFlow 等）：LFM2.5-Embedding-350M
    # 输出 1024 维（DB vector 列与 HNSW 索引已随 0008 迁移对齐）
    embedding_model: str = "liquid/lfm-2.5-embedding-350m"
    embedding_dim: int = 1024
    embedding_base_url: str = ""         # 如 https://openrouter.ai/api/v1
    embedding_api_key: str = ""
    embedding_batch_size: int = 100
    embedding_cache_dir: str = ".cache/embeddings"
    embedding_fault_every: int = 0       # >0 时每 N 次 provider 调用模拟故障（IG-15）

    # retrieval（阶段3）— 混合召回权重仅服务端配置（spec §5，禁止请求传入）
    w_struct: float = 0.7
    w_semantic: float = 0.3
    retrieval_recall_limit: int = 20     # 混合召回 Top-N（进入 WL 精排的候选数）
    retrieval_top_k: int = 4             # 精排后返回数（spec：Top-3~5）
    wl_iterations: int = 3               # 带属性 WL 子树核迭代轮数
    retrieval_ann_mode: bool = False     # RT-07 P2：两路 HNSW + RRF（规模化预留）

    # 阶段4 — LLM 判断与编排
    graph_data_mode: str = "fixture"     # fixture | live（live 走 esplora_api_url）
    esplora_api_url: str = "https://mempool.space/api"
    # issue #25：live 模式地址历史分页硬上限（页 × Esplora 页大小 25）
    esplora_max_pages: int = 40
    demo_seeds: str = ""                 # 匿名白名单地址 CSV；空则用 fixture 内 seed
    zombie_timeout_seconds: int = 120    # 进行中任务超时回收阈值（BE-40）

    @property
    def demo_seeds_list(self) -> list[str]:
        return [a.strip() for a in self.demo_seeds.split(",") if a.strip()]

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # extra="ignore"：.env 里允许存在 Settings 未声明的变量（如 LLM_API_KEY，
    # 由 providers 经 os.environ 直读，见 docker-compose.yml 注释），
    # 否则 pydantic-settings 默认 forbidden 会让按文档配置的环境直接启动失败
    model_config = {"env_file": ".env", "extra": "ignore"}


_settings: Settings | None = None


def get_settings() -> Settings:
    """全局单例。issue #27：不再注入任何 JWT_SECRET 弱默认——
    缺失/占位/弱密钥时 Settings 校验直接抛错，进程拒绝启动。"""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    global _settings
    _settings = None
