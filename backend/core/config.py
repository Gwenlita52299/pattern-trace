"""Pydantic Settings — backend-api-spec §1 core/config."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://pt:pt@localhost:5432/patterntrace"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str
    access_token_expire_minutes: int = 15
    refresh_token_days: int = 7
    llm_provider: str = "ollama"
    llm_model: str = "qwen3:30b-a3b"
    llm_base_url: str = "http://localhost:11434"
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

    # ingest（阶段2）— bybit_rust 基线数据根目录（含 results/ 与 data/）
    lazarus_data_dir: str = "/Users/gwenlita/Documents/bybit_rust/golden/python"
    ingest_synth_positives: int = 1500   # playbook 语料规模；0 关闭
    ingest_synth_seed: int = 42          # 生成器随机种子（确定性/幂等）
    negative_ratio: int = 3              # 负:正（IG-05 允许 [2.5, 3.5]）
    embedding_provider: str = "stub"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
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
    demo_seeds: str = ""                 # 匿名白名单地址 CSV；空则用 fixture 内 seed
    zombie_timeout_seconds: int = 120    # 进行中任务超时回收阈值（BE-40）

    @property
    def demo_seeds_list(self) -> list[str]:
        return [a.strip() for a in self.demo_seeds.split(",") if a.strip()]

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    model_config = {"env_file": ".env"}


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        import os

        os.environ.setdefault("JWT_SECRET", "dev-secret")
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    global _settings
    _settings = None
