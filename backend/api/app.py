"""FastAPI app factory — backend-api-spec。

阶段0：JWT access + refresh rotation、健康检查、受保护接口。
阶段4：analyze 异步判断管线（D5 状态机落库）、judgments/subgraph/patterns
查询端点、匿名演示白名单与配额、readyz 依赖检查。
"""
from __future__ import annotations

import hashlib
import math
import time
import uuid
from contextvars import ContextVar

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.errors import ProblemError, install_error_handlers, problem_response
from ..core.security import (
    PasswordHasher,
    RefreshTokenStore,
    create_token,
    decode_token,
)
from ..models.base import Case, CaseAddress, Judgment, Report


# ---- schemas ----
class LoginRequest(BaseModel):
    email: str
    password: str = Field(max_length=72)


class RefreshRequest(BaseModel):
    refresh_token: str


class AnalyzeRequest(BaseModel):
    address: str = Field(min_length=14, max_length=62)
    hops: int = 3
    time_window_days: int = Field(default=90, ge=7, le=365)
    # issue #78：E2E/mock 专用 mock 场景选择。仅 LLM_PROVIDER=mock 时生效；
    # 真实 provider 下严格 422——不构成生产故障注入接口
    mock_scenario: str | None = Field(default=None, max_length=100)

    @field_validator("hops")
    @classmethod
    def _hops_range(cls, v: int) -> int:
        # D6：与 BFS 三队列一致；自定义消息使 422 detail 明确可读（BE-10）
        if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 3:
            raise ValueError("hops must be between 1 and 3")
        return v


# ---- 阶段5 请求 schema（必须在模块层：FastAPI 解析注解依赖模块 globals，
#      闭包内定义的 BaseModel 会因 PEP 563 字符串注解无法求值）----
class CaseCreateRequest(BaseModel):
    title: str = Field(max_length=200)
    description: str = ""


class CasePatchRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    description: str | None = None
    status: str | None = None  # open → investigating → closed 单向


class CaseAddressesRequest(BaseModel):
    addresses: list[str] = Field(min_length=1, max_length=50)


class UserCreateRequest(BaseModel):
    email: str = Field(max_length=255)
    password: str
    role: str = Field(default="investigator",
                      pattern="^(investigator|admin)$")

    @field_validator("password")
    @classmethod
    def _password_bytes(cls, v: str) -> str:
        # BE-37：bcrypt 截断限制按「字节」校验而非字符
        if not (8 <= len(v.encode("utf-8")) <= 72):
            raise ValueError("password must be 8-72 bytes (utf-8)")
        return v


# ---- stores (judgments/cases/reports 已迁移 DB；_users_db 仅作无 DB 环境降级) ----
_users_db: dict[str, dict] = {}
_refresh_store = RefreshTokenStore()
_idempotency_keys: dict[str, tuple[str, str, float]] = {}  # key -> (body_hash, case_id, expires)
_current_request: ContextVar = ContextVar("request", default=None)
_DB_ENGINE = None


# ---- 用户存储：DB 优先，无 DB 环境降级内存（阶段0 测试兼容）----
def _save_user(email: str, hashed_password: str, role: str,
               *, user_id: str | None = None) -> None:
    """upsert 语义：重复 seed 覆盖密码/角色（测试可重入）。"""
    uid = user_id or str(uuid.uuid4())
    _users_db[email] = {
        "id": uid, "email": email,
        "hashed_password": hashed_password,
        "role": role, "is_active": True,
    }
    try:
        from sqlalchemy import text

        with Session(get_db_engine()) as session:
            session.execute(text(
                "INSERT INTO users (id, email, hashed_password, role, is_active) "
                "VALUES (:id, :email, :pw, :role, true) "
                "ON CONFLICT (email) DO UPDATE SET "
                "hashed_password = EXCLUDED.hashed_password, "
                "role = EXCLUDED.role, is_active = true"),
                {"id": uid, "email": email, "pw": hashed_password,
                 "role": role})
            session.commit()
    except Exception as exc:  # noqa: BLE001 — 无 DB 时内存即权威来源
        print(f"[users] db unavailable, user stored in memory only: "
              f"{exc.__class__.__name__}")


def _lookup_user(email: str) -> dict | None:
    """角色判定以存储为准而非 JWT claim（SEC-04 场景1）。"""
    try:
        from sqlalchemy import text

        with Session(get_db_engine()) as session:
            row = session.execute(text(
                "SELECT id, email, hashed_password, role, is_active "
                "FROM users WHERE email = :email"), {"email": email}).mappings().first()
        if row is None:
            return None
        return dict(row)
    except Exception:  # noqa: BLE001
        return _users_db.get(email)


def seed_user(email: str, password: str, role: str = "investigator") -> None:
    hasher = PasswordHasher()
    _save_user(email, hasher.hash(password), role)


def get_db_engine():
    global _DB_ENGINE
    if _DB_ENGINE is None:
        from sqlalchemy import create_engine

        s = get_settings()
        _DB_ENGINE = create_engine(
            s.database_url.replace("postgresql://", "postgresql+psycopg://"),
            pool_pre_ping=True)
    return _DB_ENGINE


def close_db_engine() -> None:
    global _DB_ENGINE
    if _DB_ENGINE is not None:
        _DB_ENGINE.dispose()
        _DB_ENGINE = None


def bootstrap_admin_from_env() -> None:
    """首个 admin 由环境变量 seed — backend-api-spec §users。

    未配置时跳过；幂等：已存在的邮箱不覆盖（DB 优先判定）。
    """
    settings = get_settings()
    email = settings.bootstrap_admin_email
    password = settings.bootstrap_admin_password
    if email and password and _lookup_user(email) is None:
        seed_user(email, password, role="admin")


def verify_embedding_model_lock_on_startup(settings=None) -> None:
    """RT-04 fail-fast：知识库向量模型 ≠ 配置模型时启动即报错退出。

    DB 不可达（单测/CI 无 Postgres）降级为告警不阻断——锁校验只在
    能连上库时有意义；模型不一致是配置错误，必须首查前暴露。
    """
    from sqlalchemy import text

    settings = settings or get_settings()
    try:
        engine = get_db_engine()
    except Exception as exc:  # noqa: BLE001 — 连接失败仅告警
        print(f"[startup] embedding model lock check skipped (db unreachable): {exc}")
        return
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT DISTINCT embedding_model FROM patterns "
                "WHERE embedding_model <> ''")).scalars().all()
    except Exception as exc:  # noqa: BLE001 — 表不存在（未迁移）等同空库
        print(f"[startup] embedding model lock check skipped: {exc}")
        return
    mismatched = sorted(set(rows) - {settings.embedding_model})
    if mismatched:
        raise RuntimeError(
            f"embedding model version lock violated: DB has {mismatched}, "
            f"configured {settings.embedding_model!r}. "
            "换模型需全量重建并迁移（ingest-spec §5）")


# ---- 匿名成本控制（spec §3）----
def _check_anon_limits(ip: str, settings) -> tuple[str | None, int]:
    """返回 (error_code, retry_after)；error_code None 表示放行。

    issue #23：经 RedisRateLimiter 原子计数，多实例共享；Redis 不可达时
    限流器内部降级进程内（fail-open，见 rate_limit 模块 docstring）。
    """
    from ..core.rate_limit import get_rate_limiter

    limiter = get_rate_limiter()
    allowed, retry_after = limiter.hit(
        "anon_rate", ip, limit=settings.anon_rate_per_min, window=60)
    if not allowed:
        return "RATE_LIMITED", retry_after
    allowed, retry_after = limiter.hit(
        "anon_quota", f"{time.strftime('%Y%m%d')}:{ip}",
        limit=settings.anon_daily_quota, window=86400)
    if not allowed:
        return "QUOTA_EXCEEDED", max(retry_after, 86400)
    return None, 0


# ---- 地址活跃度预检（issue #79）----
def _precheck_address_activity(address: str, settings) -> None:
    """高活跃地址建图前拦截：chain tx_count 超 ADDRESS_TX_COUNT_LIMIT 即 422。

    - 仅 live 模式执行（fixture 是确定性演示图，无真实活跃度概念）
    - 预检本身失败（Esplora 不可达）放行而非拒绝：分页截断（issue #25）
      与 job timeout 兜底仍是第二道防线，预检不应成为分析的单点故障
    - 拒绝与预检失败均 print 留痕；4xx 响应由审计中间件按
      action=analyze / action_result=failure 落 audit_logs
    """
    if settings.graph_data_mode != "live":
        return
    from ..graph_builder.data_source import LiveEsploraProvider, _sync_redis

    provider = LiveEsploraProvider(settings.esplora_api_url,
                                   redis_client=_sync_redis())
    try:
        stats = provider.address_stats(address)
    except Exception as exc:  # noqa: BLE001 — fail-open，见 docstring
        print(f"[precheck] address stats unavailable "
              f"({exc.__class__.__name__}); allowing {address[:12]}…")
        return
    tx_count = (stats or {}).get("tx_count")
    if tx_count is None:
        return
    if tx_count > settings.address_tx_count_limit:
        print(f"[precheck] rejected {address[:12]}…: tx_count={tx_count} "
              f"> limit={settings.address_tx_count_limit}")
        raise ProblemError(
            422,
            f"address has {tx_count} historical transactions, exceeding the "
            f"analysis limit ({settings.address_tx_count_limit}); building "
            "a subgraph for this address would generate excessive upstream "
            "requests",
            "ADDRESS_TOO_ACTIVE")


def _demo_seeds(settings) -> list[str]:
    seeds = settings.demo_seeds_list
    if seeds:
        return seeds
    from ..graph_builder.data_source import FixtureTxProvider

    return FixtureTxProvider.load().seed_addresses


# ---- 审计日志（阶段5 · CM-10 / BE-07/32）----
def _audit_action(method: str, path: str) -> tuple[str, str | None] | None:
    """返回 (action, resource_id)；非业务写路径返回 None 不记录。"""
    if path.startswith("/api/v1/auth/login"):
        return "login", None
    if path.startswith("/api/v1/auth/logout"):
        return "logout", None
    if path == "/api/v1/addresses/analyze":
        return "analyze", None
    if path == "/api/v1/cases" and method == "POST":
        return "create_case", None
    if path.startswith("/api/v1/cases/") and method == "PATCH":
        return "update_case", path.split("/")[4]
    if path.endswith("/addresses") and method == "POST" \
            and path.startswith("/api/v1/cases/"):
        return "associate_address", path.split("/")[4]
    if "/addresses/" in path and method == "DELETE":
        return "remove_address", path.split("/")[4]
    if path.endswith("/reports") and method == "POST":
        return "export_report", path.split("/")[4]
    if path == "/api/v1/users" and method == "POST":
        return "create_user", None
    if path.startswith("/api/v1/users/") and method == "PATCH":
        return "update_user", None
    return None


def _audit_user_id(request) -> str | None:
    payload = decode_token(
        request.headers.get("authorization", "").removeprefix("Bearer ").strip(),
        get_settings().jwt_secret)
    email = (payload or {}).get("sub")
    if not email:
        return None
    user = _lookup_user(email)
    return (user or {}).get("id") or email


def write_audit(method: str, path: str, *, status: int, request_id: str,
                latency_ms: int, ip: str, user_agent: str,
                user_id: str | None = None,
                detail: dict | None = None) -> None:
    action_info = _audit_action(method, path)
    if action_info is None:
        return  # 非业务路径（healthz 等）不产生审计噪声
    resource_type = (
        "case" if action_info[0].endswith("case")
        or action_info[0] in ("associate_address", "remove_address")
        else None)
    _insert_audit(action=action_info[0], resource_type=resource_type,
                  resource_id=action_info[1], method=method, path=path,
                  status=status, request_id=request_id,
                  latency_ms=latency_ms, ip=ip, user_agent=user_agent,
                  user_id=user_id, detail=detail)


def write_access_denied(request, resource_type: str, resource_id: str,
                        user_id: str | None) -> None:
    """SEC-01：越权访问拒绝必须留痕——显式插入，不走写操作路由映射门。"""
    _insert_audit(
        action="access_denied", resource_type=resource_type,
        resource_id=resource_id, method=request.method, path=request.url.path,
        status=403, request_id=str(uuid.uuid4()), latency_ms=0,
        ip=request.client.host if request.client else "-",
        user_agent=request.headers.get("user-agent", ""),
        user_id=user_id,
        detail={"denied_resource": f"{resource_type}:{resource_id}"})


def _insert_audit(*, action: str, resource_type: str | None,
                  resource_id: str | None, method: str, path: str,
                  status: int, request_id: str, latency_ms: int, ip: str,
                  user_agent: str, user_id: str | None,
                  detail: dict | None) -> None:
    try:
        from ..models.base import AuditLog

        with Session(get_db_engine()) as session:
            session.add(AuditLog(
                user_id=user_id, request_id=request_id,
                http_method=method, http_path=path[:500],
                response_status=status, action=action,
                resource_type=resource_type, resource_id=resource_id,
                action_result="success" if status < 400 else "failure",
                detail=detail or {}, latency_ms=latency_ms,
                ip=ip[:64], user_agent=user_agent[:300]))
            session.commit()
    except Exception as exc:  # noqa: BLE001 — 审计失败不阻断业务（打印留痕）
        print(f"[audit] write failed ({exc.__class__.__name__}); "
              f"{method} {path} -> {status}")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="PatternTrace API", version="0.3.0")
    install_error_handlers(app)
    bootstrap_admin_from_env()
    verify_embedding_model_lock_on_startup(settings)  # RT-04 fail-fast
    hasher = PasswordHasher()
    access_ttl = settings.access_token_expire_minutes * 60
    refresh_ttl = settings.refresh_token_days * 86400
    # rotation 状态落 Redis（跨进程/多实例共享）；不可达时 store 内部降级进程内
    global _refresh_store
    _refresh_store = RefreshTokenStore(redis_url=settings.redis_url,
                                       ttl_seconds=refresh_ttl)

    @app.middleware("http")
    async def csrf_and_audit(request: Request, call_next):
        """SEC-03：写请求强制 X-Requested-With（服务端主动校验，非依赖前端自觉）；
        同时为业务写操作落审计日志（CM-10）。"""
        request_id = str(uuid.uuid4())
        started = time.perf_counter()

        if request.method not in ("GET", "HEAD", "OPTIONS") \
                and request.headers.get("x-requested-with") != "XMLHttpRequest":
            return problem_response(
                403, "Forbidden",
                "CSRF check failed: X-Requested-With header required",
                request.url.path, "CSRF_CHECK_FAILED")

        _current_request.set(request)  # 越权留痕（SEC-01）需要请求上下文
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id

        if request.method in ("POST", "PATCH", "DELETE"):
            try:
                write_audit(
                    request.method, request.url.path,
                    status=response.status_code, request_id=request_id,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    ip=request.client.host if request.client else "-",
                    user_agent=request.headers.get("user-agent", ""),
                    user_id=_audit_user_id(request))
            except Exception as exc:  # noqa: BLE001 — 审计永不阻断主流程
                print(f"[audit] middleware error: {exc.__class__.__name__}")
        return response

    def _decode_access(authorization: str) -> dict | None:
        token = authorization.removeprefix("Bearer ").strip()
        payload = decode_token(token, settings.jwt_secret)
        # issue #67：只接受显式 typ=access；否则 7 天有效期的 refresh token
        # 可当 Bearer 用，绕过 access token 15 分钟有效期与撤销机制
        if payload is None or payload.get("typ") != "access":
            return None
        return payload

    def require_role(*roles: str):
        def checker(authorization: str = Header(default="")):
            payload = _decode_access(authorization)
            if payload is None:
                raise ProblemError(401, "Not authenticated", "UNAUTHORIZED")
            user = _lookup_user(payload.get("sub", ""))
            if user is None or not user.get("is_active", True) \
                    or user["role"] not in roles:
                raise ProblemError(403, "Forbidden", "FORBIDDEN")
            return user
        return checker

    def optional_user(authorization: str = Header(default="")) -> dict | None:
        payload = _decode_access(authorization)
        if payload is None:
            return None
        user = _lookup_user(payload.get("sub", ""))
        # issue #69：停用/已删用户按匿名处理，否则 /addresses/analyze 会
        # 把停用用户当已登录，绕过演示白名单与匿名配额
        if user is None or not user.get("is_active", True):
            return None
        return user

    # ---- health ----
    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/api/v1/demo/addresses")
    def demo_addresses():
        # 匿名可访问：首页演示 chips 数据源（白名单本身是公开信息）
        return {"addresses": _demo_seeds(settings)}

    @app.get("/readyz")
    def readyz():
        failures: list[str] = []
        try:
            from sqlalchemy import text

            with get_db_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"db unreachable: {exc.__class__.__name__}")
        try:
            import redis

            redis.Redis.from_url(
                settings.redis_url, socket_connect_timeout=1).ping()
        except Exception as exc:  # noqa: BLE001
            failures.append(f"redis unreachable: {exc.__class__.__name__}")
        if failures:
            raise ProblemError(503, "; ".join(failures),
                               "DEPENDENCY_UNAVAILABLE")
        return {"status": "ready"}

    # ---- auth ----
    @app.post("/api/v1/auth/login")
    def login(body: LoginRequest, request: Request):
        from ..core.rate_limit import get_rate_limiter

        ip = request.client.host if request.client else "-"
        limiter = get_rate_limiter()
        # SEC-05 登录爆破防护（issue #23）：先查历史失败计数再验证密码，
        # IP 或账号任一维度超限即 429。不预增：只有真实失败才计数；
        # 响应不区分计数维度与账号存在性，防枚举
        for bucket, identity in (("login_ip", ip),
                                 ("login_email", body.email)):
            allowed, retry_after = limiter.check(
                bucket, identity, settings.login_fail_limit)
            if not allowed:
                raise ProblemError(
                    429, "too many failed login attempts; try again later",
                    "RATE_LIMITED",
                    headers={"Retry-After": str(retry_after)})

        user = _lookup_user(body.email)
        if user is None or not hasher.verify(body.password,
                                             user["hashed_password"]):
            # 不区分邮箱不存在/密码错误，防枚举（BE-03）
            limiter.hit("login_ip", ip,
                        limit=settings.login_fail_limit,
                        window=settings.login_fail_window)
            limiter.hit("login_email", body.email,
                        limit=settings.login_fail_limit,
                        window=settings.login_fail_window)
            raise ProblemError(401, "Invalid credentials",
                               "INVALID_CREDENTIALS")

        # 成功登录清零双维度计数：正常用户不应被同 IP 历史失败拖累
        limiter.reset("login_ip", ip)
        limiter.reset("login_email", body.email)

        # issue #69：停用账号即使密码正确也拒绝，复用 INVALID_CREDENTIALS
        # 保持防枚举语义（与「密码错误」不可区分）
        if not user.get("is_active", True):
            raise ProblemError(401, "Invalid credentials",
                               "INVALID_CREDENTIALS")

        family_id = str(uuid.uuid4())
        refresh_jti = str(uuid.uuid4())
        access_token = create_token(
            {"sub": body.email, "jti": str(uuid.uuid4()), "typ": "access"},
            settings.jwt_secret, access_ttl,
        )
        refresh_token = create_token(
            {"sub": body.email, "jti": refresh_jti,
             "family": family_id, "typ": "refresh"},
            settings.jwt_secret, refresh_ttl,
        )
        _refresh_store.issue(family_id, refresh_jti)

        from fastapi.responses import JSONResponse

        # D4：refresh token 只经 HttpOnly Cookie 下发，绝不进响应 body
        resp = JSONResponse(content={
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": access_ttl,
            "user": {"id": user.get("id"), "email": body.email,
                     "role": user["role"]},
        })
        # SEC-02：HttpOnly + 作用域限定 Path；生产（Secure）必须 SameSite=None
        # 否则前后端分域部署时 refresh Cookie 不会被携带
        resp.set_cookie(
            "refresh_token", refresh_token, max_age=refresh_ttl,
            httponly=True,
            samesite="none" if settings.cookie_secure else "lax",
            path="/api/v1/auth", secure=settings.cookie_secure)
        return resp

    @app.post("/api/v1/auth/refresh")
    def refresh(request: Request, body: RefreshRequest | None = None):
        from ..core.rate_limit import get_rate_limiter

        # issue #23：认证路径统一限流（token 猜测/重放洪水的低成本防线）。
        # check+hit 先于解码：无效 token 的洪水请求同样计入窗口
        ip = request.client.host if request.client else "-"
        limiter = get_rate_limiter()
        allowed, retry_after = limiter.check(
            "auth_ip", ip, settings.auth_rate_per_min)
        if not allowed:
            raise ProblemError(
                429, "too many requests; try again later", "RATE_LIMITED",
                headers={"Retry-After": str(retry_after)})
        limiter.hit("auth_ip", ip, limit=settings.auth_rate_per_min,
                    window=60)

        # 显式 body token 优先（客户端主动断言，reuse detection 依赖它）；
        # 浏览器场景无 body，回落到 HttpOnly Cookie
        raw_body_token = (body.refresh_token if body else "") or ""
        token = raw_body_token or request.cookies.get("refresh_token", "")
        payload = decode_token(token, settings.jwt_secret)
        if payload is None or payload.get("typ") != "refresh":
            raise ProblemError(401, "Invalid refresh token", "UNAUTHORIZED")

        family_id = payload.get("family", "")
        old_jti = payload.get("jti", "")
        new_refresh_jti = str(uuid.uuid4())
        email = payload.get("sub", "")

        # issue #69：refresh 凭 sub 重读用户，停用/已删账号的存量 refresh
        # token 一律拒绝（否则停用后仍可续期会话）。置于 rotate 之前，
        # 不消耗 rotation 状态
        user = _lookup_user(email)
        if user is None or not user.get("is_active", True):
            raise ProblemError(401, "Invalid refresh token", "UNAUTHORIZED")

        if not _refresh_store.rotate(family_id, old_jti, new_refresh_jti):
            raise ProblemError(401, "Token reuse detected; please re-login",
                               "TOKEN_REUSE")

        access_token = create_token(
            {"sub": email, "jti": str(uuid.uuid4()), "typ": "access"},
            settings.jwt_secret, access_ttl,
        )
        new_refresh_token = create_token(
            {"sub": email, "jti": new_refresh_jti,
             "family": family_id, "typ": "refresh"},
            settings.jwt_secret, refresh_ttl,
        )

        from fastapi.responses import JSONResponse

        # D4：轮换后的新 refresh token 同样只经 HttpOnly Cookie 下发
        # email：issue #43，前端刷新后凭它恢复导航栏用户中心展示态
        resp = JSONResponse(content={
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": access_ttl,
            "email": email,
        })
        resp.set_cookie(
            "refresh_token", new_refresh_token, max_age=refresh_ttl,
            httponly=True,
            samesite="none" if settings.cookie_secure else "lax",
            path="/api/v1/auth", secure=settings.cookie_secure)
        return resp

    @app.post("/api/v1/auth/logout")
    def logout(request: Request, authorization: str = Header(default="")):
        cookie_payload = decode_token(
            request.cookies.get("refresh_token") or "", settings.jwt_secret)
        family_id = (cookie_payload or {}).get("family")
        if family_id:
            _refresh_store.revoke_family(family_id)

        from fastapi.responses import JSONResponse

        resp = JSONResponse(content={"detail": "logged out"}, status_code=204)
        resp.delete_cookie("refresh_token", path="/api/v1/auth")
        return resp

    # ---- 分析管线 ----
    @app.post("/api/v1/addresses/analyze")
    async def analyze(
        body: AnalyzeRequest,
        request: Request,
        user: dict | None = Depends(optional_user),
    ):
        from ..core.btc_address import validate_btc_address

        err = validate_btc_address(body.address)
        if err:
            raise ProblemError(422, err, "VALIDATION_ERROR")

        # issue #78：mock 场景是测试专用通道——非 mock provider 一律拒绝，
        # 保证生产/live 形态不存在「按请求注入 LLM 故障」的接口
        if body.mock_scenario and settings.llm_provider != "mock":
            raise ProblemError(
                422, "mock_scenario requires LLM_PROVIDER=mock",
                "MOCK_SCENARIO_REQUIRES_MOCK")

        if user is None:
            # 免登录仅允许演示白名单（成本控制，BE-09）
            if body.address not in _demo_seeds(settings):
                raise ProblemError(
                    403,
                    "anonymous analysis restricted to demo addresses; "
                    "login to analyze arbitrary addresses",
                    "DEMO_ADDRESS_REQUIRED")
            ip = request.client.host if request.client else "127.0.0.1"
            code, retry_after = _check_anon_limits(ip, settings)
            if code:
                raise ProblemError(
                    429, "daily anon quota exceeded" if code == "QUOTA_EXCEEDED"
                    else "rate limit exceeded",
                    code, headers={"Retry-After": str(retry_after)})

        from ..models.base import Judgment
        from ..services.orchestration import reclaim_zombies
        from ..services.task_queue import dispatch_analysis

        # issue #79：高活跃地址预检（单次 /address/:addr/stats 请求）——
        # 建图前拦截，避免高活跃地址进入 BFS 后产生数万次 Esplora 请求
        # 占满 worker；阻塞调用放线程池，不卡事件循环
        from fastapi.concurrency import run_in_threadpool

        await run_in_threadpool(_precheck_address_activity,
                                body.address, settings)

        # 顺带回收僵尸任务（BE-40）：无专用定时进程时的低成本替代
        with Session(get_db_engine()) as session:
            reclaim_zombies(session)

        jid = str(uuid.uuid4())
        created = True
        try:
            with Session(get_db_engine()) as session:
                session.add(Judgment(
                    id=jid, address=body.address, hops=body.hops,
                    time_window_days=body.time_window_days,
                    mock_scenario=body.mock_scenario,
                    # created_by FK 指向 users.id（与 reports 一致），存 email 会 500
                    created_by=(user or {}).get("id")))
                session.commit()
        except IntegrityError:
            # partial unique index 命中：复用进行中任务（BE-12/46 幂等）
            with Session(get_db_engine()) as session:
                existing = session.execute(
                    select(Judgment)
                    .where(Judgment.address == body.address,
                           Judgment.hops == body.hops,
                           Judgment.time_window_days == body.time_window_days,
                           Judgment.status.in_(("queued", "processing")))
                    .limit(1)).scalar_one_or_none()
            if existing is None:
                raise
            jid, created = existing.id, False

        # issue #22：经 Arq 持久化队列投递，API 重启不再丢失排队中的任务；
        # Redis 不可达时 task_queue 内部降级为进程内任务（单实例部署形态）
        await dispatch_analysis(jid)

        status_now = "queued"
        if not created:
            with Session(get_db_engine()) as session:
                row = session.get(Judgment, jid)
                status_now = row.status if row else "queued"
        return JSONResponse(
            status_code=202 if created else 200,
            content={"judgment_id": jid, "status": status_now,
                     "poll_url": f"/api/v1/judgments/{jid}"},
        )

    @app.get("/api/v1/judgments/{judgment_id}/retrieval-explanation")
    def get_retrieval_explanation(judgment_id: str):
        """issue #77：检索解释快照（匹配依据）——权限同 GET judgments（匿名只读）。

        从 judgment_events 的 stage:retrieval_done 行读取**落库时**的自包含
        快照，不实时查 patterns/检索配置——历史 Judgment 的解释不受后续
        模式编辑或配置变化影响（验收要求）。

        pending 行（尚未完成检索）返回 409 语义的空态：status=processing +
        stages 进度，前端展示"分析进行中"而非错误；failed 行无快照时同样
        明确返回 empty_reason。
        """
        from ..models.base import Judgment, JudgmentEvent

        with Session(get_db_engine()) as session:
            row = session.get(Judgment, judgment_id)
            if row is None:
                raise ProblemError(404, "Judgment not found", "NOT_FOUND")
            events = session.execute(
                select(JudgmentEvent)
                .where(JudgmentEvent.judgment_id == judgment_id,
                       JudgmentEvent.to_status == "stage:retrieval_done")
                .order_by(JudgmentEvent.id.desc())
                .limit(1)
            ).scalars().all()
        snapshot = (events[0].detail if events else None) or None
        if snapshot is None:
            # 快照未生成：明确区分「进行中」与「无检索记录」
            empty_reason = ("analysis_in_progress"
                            if row.status in ("queued", "processing")
                            else "retrieval_not_recorded")
            return {
                "judgment_id": judgment_id,
                "status": row.status,
                "algorithm_version": None,
                "params": None,
                "recall": None,
                "candidates": [],
                "dropped_by_top_k": 0,
                "empty_reason": empty_reason,
            }
        payload = dict(snapshot)
        payload["judgment_id"] = judgment_id
        payload["status"] = row.status
        payload.setdefault("empty_reason", None)
        return payload

    @app.get("/api/v1/judgments/{judgment_id}")
    def get_judgment(judgment_id: str):
        # 权限矩阵：GET judgments/subgraph/patterns 匿名只读放行
        from ..models.base import Judgment

        with Session(get_db_engine()) as session:
            row = session.get(Judgment, judgment_id)
        if row is None:
            raise ProblemError(404, "Judgment not found", "NOT_FOUND")
        return _judgment_payload(row)

    @app.get("/api/v1/addresses/{address}/subgraph")
    def get_subgraph(
        address: str,
        judgment_id: str | None = Query(default=None),
    ):
        from ..models.base import Judgment

        with Session(get_db_engine()) as session:
            stmt = select(Judgment).where(Judgment.address == address,
                                          Judgment.status == "completed")
            if judgment_id:
                stmt = stmt.where(Judgment.id == judgment_id)
            else:
                stmt = stmt.order_by(Judgment.created_at.desc()).limit(1)
            row = session.execute(stmt).scalar_one_or_none()
        if row is None or not row.subgraph_snapshot:
            raise ProblemError(404, "no completed analysis for this address",
                               "NOT_FOUND")
        snap = row.subgraph_snapshot
        return {"nodes": snap.get("nodes", []), "edges": snap.get("edges", [])}

    @app.get("/api/v1/patterns")
    def list_patterns(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        evidence_grade: str | None = Query(default=None, pattern="^[ABS]$"),
        provenance: str | None = Query(
            default=None, pattern="^(confirmed|synthetic|negative)$"),
        search: str | None = Query(default=None),
    ):
        from ..models.knowledge import Pattern

        with Session(get_db_engine()) as session:
            stmt = select(Pattern)
            count_stmt = select(func.count(Pattern.id))
            if evidence_grade:
                stmt = stmt.where(Pattern.evidence_grade == evidence_grade)
                count_stmt = count_stmt.where(
                    Pattern.evidence_grade == evidence_grade)
            if provenance:
                stmt = stmt.where(Pattern.provenance == provenance)
                count_stmt = count_stmt.where(Pattern.provenance == provenance)
            if search:
                like = f"%{search}%"
                cond = Pattern.name.ilike(like)
                stmt = stmt.where(cond)
                count_stmt = count_stmt.where(cond)
            total = session.execute(count_stmt).scalar_one()
            rows = session.execute(
                stmt.order_by(Pattern.created_at, Pattern.id)
                .offset((page - 1) * page_size).limit(page_size)).scalars().all()

        return {
            "items": [_pattern_summary(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": math.ceil(total / page_size) if total else 0,
        }

    @app.get("/api/v1/patterns/{pattern_id}")
    def get_pattern(
        pattern_id: str,
        max_nodes: int = Query(default=300, ge=1, le=1000),
    ):
        """issue #84：单条模式详情（含 canonical 子图，供结构预览/对比）。

        权限同 patterns 列表（公开只读）。KB 里存在 2,000+ 节点的大闭包
        （p90≈2,081），全量回传既拖慢前端布局也无可视化价值——按
        first_layer 升序（种子与近层优先）截断到 max_nodes，返回
        graph_truncated 与总量供前端明确提示。
        """
        from ..models.knowledge import Pattern

        with Session(get_db_engine()) as session:
            row = session.get(Pattern, pattern_id)
        if row is None:
            raise ProblemError(404, "Pattern not found", "NOT_FOUND")

        canon = row.canonical_subgraph or {}
        nodes = list(canon.get("nodes") or [])
        edges = list(canon.get("edges") or [])
        total_nodes, total_edges = len(nodes), len(edges)
        truncated = total_nodes > max_nodes
        if truncated:
            nodes, edges = _bfs_truncate(nodes, edges, max_nodes)

        return {
            **_pattern_summary(row),
            "description": row.description or "",
            "node_count": total_nodes,
            "edge_count": total_edges,
            "graph_truncated": truncated,
            "displayed_node_count": len(nodes),
            "canonical_subgraph": {"nodes": nodes, "edges": edges},
        }

    # ---- 案件管理（阶段5 · 业务闭环）----
    def _load_owned_case(session, user: dict, case_id: str):
        """加载案件并做水平越权隔离：非本人案件一律 404 不泄露存在性（SEC-01）。"""
        from ..models.base import Case

        case = session.get(Case, case_id)
        if case is None:
            raise ProblemError(404, "Case not found", "NOT_FOUND")
        if user["role"] != "admin" and case.owner_id != str(user.get("id")):
            request = _current_request.get()
            if request is not None:
                write_access_denied(request, "case", case_id,
                                    str(user.get("id")))
            raise ProblemError(404, "Case not found", "NOT_FOUND")
        return case

    # 请求 schema（CaseCreateRequest 等）统一在模块层定义——见文件头部说明
    CASE_STATUS_ORDER = {"open": 0, "investigating": 1, "closed": 2}

    @app.post("/api/v1/cases", status_code=201)
    def create_case(
        body: CaseCreateRequest,
        idempotency_key: str = Header(default=""),
        user: dict = Depends(require_role("investigator", "admin")),
    ):
        body_hash = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        now = time.time()
        if idempotency_key:  # BE-23/48：同 key 同 body 复用；异 body 冲突
            cached = _idempotency_keys.get(idempotency_key)
            if cached and cached[2] > now:
                if cached[0] != body_hash:
                    raise ProblemError(409,
                                       "Idempotency-Key already used with "
                                       "a different request body",
                                       "IDEMPOTENCY_KEY_CONFLICT")
                with Session(get_db_engine()) as session:
                    row = session.get(Case, cached[1])
                if row is not None:
                    return JSONResponse(status_code=200,
                                        content=_case_payload(row))
            _idempotency_keys.pop(idempotency_key, None)

        case_id = str(uuid.uuid4())
        with Session(get_db_engine()) as session:
            session.add(Case(id=case_id, owner_id=str(user["id"]),
                                  title=body.title,
                                  description=body.description))
            session.commit()
        if idempotency_key:
            _idempotency_keys[idempotency_key] = (body_hash, case_id,
                                                  now + 86400)
        return {"id": case_id, "title": body.title,
                "description": body.description, "status": "open"}

    @app.get("/api/v1/cases")
    def list_cases(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        status_filter: str | None = Query(default=None,
                                          alias="status",
                                          pattern="^(open|investigating|closed)$"),
        user: dict = Depends(require_role("investigator", "admin")),
    ):
        from ..models.base import Case

        stmt = select(Case)
        count_stmt = select(func.count(Case.id))
        if user["role"] != "admin":
            cond = Case.owner_id == str(user["id"])
            stmt, count_stmt = stmt.where(cond), count_stmt.where(cond)
        if status_filter:
            cond = Case.status == status_filter
            stmt, count_stmt = stmt.where(cond), count_stmt.where(cond)
        with Session(get_db_engine()) as session:
            total = session.execute(count_stmt).scalar_one()
            rows = session.execute(
                stmt.order_by(Case.created_at.desc())
                .offset((page - 1) * page_size).limit(page_size)).scalars().all()
        return {"items": [_case_summary(r) for r in rows], "total": total,
                "page": page, "page_size": page_size,
                "pages": math.ceil(total / page_size) if total else 0}

    @app.get("/api/v1/cases/{case_id}")
    def get_case(case_id: str,
                 user: dict = Depends(require_role("investigator", "admin"))):
        with Session(get_db_engine()) as session:
            case = _load_owned_case(session, user, case_id)
            links = session.execute(
                select(CaseAddress).where(
                    CaseAddress.case_id == case_id)
                .order_by(CaseAddress.added_at)).scalars().all()
            payload = _case_payload(case)
            payload["addresses"] = []
            for link in links:
                latest = session.execute(
                    select(Judgment)
                    .where(Judgment.address == link.address,
                           Judgment.status == "completed")
                    .order_by(Judgment.created_at.desc())
                    .limit(1)).scalar_one_or_none()
                payload["addresses"].append({
                    "address": link.address,
                    "label": link.label,
                    "added_at": link.added_at.isoformat() if link.added_at else None,
                    "latest_judgment": ({
                        "id": latest.id, "status": latest.status,
                        "risk_level": latest.risk_level,
                        "confidence": latest.confidence,
                        "created_at": latest.created_at.isoformat()
                        if latest.created_at else None,
                        "concluded_at": latest.concluded_at.isoformat()
                        if latest.concluded_at else None,
                        "data_as_of": latest.data_as_of.isoformat()
                        if latest.data_as_of else None}
                        if latest else None),
                })
        return payload

    @app.patch("/api/v1/cases/{case_id}")
    def patch_case(case_id: str, body: CasePatchRequest,
                   user: dict = Depends(require_role("investigator", "admin"))):
        with Session(get_db_engine()) as session:
            case = _load_owned_case(session, user, case_id)
            if body.title is not None:
                case.title = body.title
            if body.description is not None:
                case.description = body.description
            if body.status is not None:
                new_rank = CASE_STATUS_ORDER.get(body.status)
                if new_rank is None:
                    raise ProblemError(422, f"unknown status {body.status!r}",
                                       "VALIDATION_ERROR")
                if new_rank < CASE_STATUS_ORDER[case.status]:
                    # BE-25：状态只能单向推进 open→investigating→closed
                    raise ProblemError(
                        422,
                        "case status can only move forward: "
                        "open → investigating → closed",
                        "VALIDATION_ERROR")
                case.status = body.status
            session.commit()
            payload = _case_payload(case)
        return payload

    @app.post("/api/v1/cases/{case_id}/addresses")
    def associate_addresses(case_id: str, body: CaseAddressesRequest,
                            user: dict = Depends(require_role("investigator", "admin"))):
        from ..core.btc_address import validate_btc_address

        for addr in body.addresses:  # BE-49：先全量校验，非法即 422
            err = validate_btc_address(addr)
            if err:
                raise ProblemError(422, err, "VALIDATION_ERROR")

        added = 0
        with Session(get_db_engine()) as session:
            _load_owned_case(session, user, case_id)
            for addr in body.addresses:
                exists = session.execute(
                    select(func.count(CaseAddress.id)).where(
                        CaseAddress.case_id == case_id,
                        CaseAddress.address == addr)).scalar_one()
                if exists:      # BE-24：重复关联幂等跳过
                    continue
                session.add(CaseAddress(id=str(uuid.uuid4()),
                                        case_id=case_id, address=addr))
                added += 1
            session.commit()
        return JSONResponse(status_code=201 if added else 200,
                            content={"added": added,
                                     "addresses": body.addresses})

    @app.delete("/api/v1/cases/{case_id}/addresses/{address}")
    def remove_address(case_id: str, address: str,
                       user: dict = Depends(require_role("investigator", "admin"))):
        with Session(get_db_engine()) as session:
            _load_owned_case(session, user, case_id)
            deleted = session.execute(
                delete(CaseAddress).where(CaseAddress.case_id == case_id,
                                          CaseAddress.address == address))
            session.commit()
        if deleted.rowcount == 0:
            raise ProblemError(404, "address not linked to this case",
                               "NOT_FOUND")
        return {"detail": "removed"}

    # ---- 报告导出（阶段5 · 异步 + 签名下载）----
    @app.post("/api/v1/cases/{case_id}/reports", status_code=202)
    async def create_report(
        case_id: str,
        format: str = Query(default="pdf", pattern="^(pdf|html)$"),
        user: dict = Depends(require_role("investigator", "admin")),
    ):
        from ..services.task_queue import dispatch_report

        rid = str(uuid.uuid4())
        with Session(get_db_engine()) as session:
            _load_owned_case(session, user, case_id)
            pending = session.execute(
                select(func.count(Report.id))
                .where(Report.created_by == str(user["id"]),
                       Report.status == "processing")).scalar_one()
            if pending >= 2:  # BE-27：单用户并发报告 ≤ 2
                raise ProblemError(
                    429, "concurrent report limit (2) reached",
                    "REPORT_CONCURRENCY_LIMIT",
                    headers={"Retry-After": "30"})
            session.add(Report(id=rid, case_id=case_id, format=format,
                               created_by=str(user["id"])))
            session.commit()

        # issue #22：报告经持久化队列由 worker 渲染，文件写入共享存储卷，
        # 多实例/容器替换后仍可下载
        await dispatch_report(rid)
        return {"report_id": rid, "status": "processing",
                "poll_url": f"/api/v1/reports/{rid}"}

    @app.get("/api/v1/reports/{report_id}")
    def get_report(report_id: str,
                   user: dict = Depends(require_role("investigator", "admin"))):
        from ..services.report_service import build_download_url

        with Session(get_db_engine()) as session:
            report = session.get(Report, report_id)
            if report is None:
                raise ProblemError(404, "Report not found", "NOT_FOUND")
            case = session.get(Case, report.case_id)
            if user["role"] != "admin" \
                    and (case is None
                         or case.owner_id != str(user["id"])):
                write_access_denied(_current_request.get(), "report",
                                    report_id, str(user.get("id")))
                raise ProblemError(404, "Report not found", "NOT_FOUND")

        payload = {
            "id": report.id, "case_id": report.case_id,
            "format": report.format, "status": report.status,
            "created_at": report.created_at.isoformat()
            if report.created_at else None,
        }
        if report.status == "completed":
            payload["download_url"] = build_download_url(
                f"/api/v1/reports/{report_id}/download", report_id,
                settings.jwt_secret)
        elif report.status == "failed":
            payload["error_code"] = report.error_code
            payload["error_message"] = report.error_message
        return payload

    @app.get("/api/v1/reports/{report_id}/download")
    def download_report(report_id: str, exp: int = Query(...),
                        sig: str = Query(...)):
        # 签名 URL 自证授权（HMAC + 过期时间）；无需 Bearer，便于临时交付
        from fastapi.responses import FileResponse

        from ..services.report_service import (
            REPORTS_DIR,
            verify_download_token,
        )

        if not verify_download_token(report_id, exp, sig, settings.jwt_secret):
            raise ProblemError(403, "download URL invalid or expired",
                               "DOWNLOAD_URL_INVALID")
        with Session(get_db_engine()) as session:
            report = session.get(Report, report_id)
        if report is None or report.status != "completed" \
                or not report.storage_key:
            raise ProblemError(404, "Report not available", "NOT_FOUND")
        path = REPORTS_DIR / report.storage_key
        media = "application/pdf" if report.format == "pdf" \
            else "text/html; charset=utf-8"
        return FileResponse(path, media_type=media,
                            filename=f"patterntrace-{report_id}{path.suffix}")

    # ---- 用户与审计（admin only）----
    @app.post("/api/v1/users", status_code=201)
    def create_user(body: UserCreateRequest,
                    admin: dict = Depends(require_role("admin"))):
        if _lookup_user(body.email) is not None:
            raise ProblemError(409, "email already exists", "EMAIL_EXISTS")
        seed_user(body.email, body.password, role=body.role)
        created = _lookup_user(body.email)
        return {"id": created["id"], "email": body.email,
                "role": body.role, "is_active": True}

    @app.patch("/api/v1/users/{email}")
    def update_user(email: str, role: str | None = Query(default=None),
                    is_active: bool | None = Query(default=None),
                    admin: dict = Depends(require_role("admin"))):
        target = _lookup_user(email)
        if target is None:
            raise ProblemError(404, "user not found", "NOT_FOUND")
        from sqlalchemy import text as _t

        sets, params = [], {"email": email}
        if role is not None:
            if role not in ("investigator", "admin"):
                raise ProblemError(422, "unknown role", "VALIDATION_ERROR")
            sets.append("role = :role")
            params["role"] = role
        if is_active is not None:
            sets.append("is_active = :ia")
            params["ia"] = is_active
        if sets:
            with Session(get_db_engine()) as session:
                session.execute(_t(f"UPDATE users SET {', '.join(sets)} "
                                   "WHERE email = :email"), params)
                session.commit()
        if role is not None and email in _users_db:
            _users_db[email]["role"] = role
        updated = _lookup_user(email)
        return {"email": email, "role": updated["role"],
                "is_active": updated.get("is_active", True)}

    @app.get("/api/v1/audit-logs")
    def audit_logs_endpoint(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        action: str | None = Query(default=None),
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
        admin: dict = Depends(require_role("admin")),
    ):
        from ..models.base import AuditLog

        stmt = select(AuditLog)
        count_stmt = select(func.count(AuditLog.id))
        conds = []
        if action:
            conds.append(AuditLog.action == action)
        try:
            if start:
                conds.append(AuditLog.created_at >= start)
            if end:
                conds.append(AuditLog.created_at <= end)
        except Exception:
            raise ProblemError(422, "invalid time range", "VALIDATION_ERROR")
        for c in conds:
            stmt, count_stmt = stmt.where(c), count_stmt.where(c)
        with Session(get_db_engine()) as session:
            total = session.execute(count_stmt).scalar_one()
            rows = session.execute(
                stmt.order_by(AuditLog.created_at.desc())
                .offset((page - 1) * page_size).limit(page_size)).scalars().all()
        return {
            "items": [{
                "id": r.id, "user_id": r.user_id,
                "request_id": r.request_id,
                "http_method": r.http_method, "http_path": r.http_path,
                "response_status": r.response_status, "action": r.action,
                "resource_type": r.resource_type,
                "resource_id": r.resource_id,
                "action_result": r.action_result,
                "latency_ms": r.latency_ms,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            } for r in rows],
            "total": total, "page": page, "page_size": page_size,
            "pages": math.ceil(total / page_size) if total else 0}

    @app.get("/api/v1/audit-logs")
    def audit_logs(user: dict = Depends(require_role("admin"))):
        return {"items": [], "total": 0, "page": 1, "page_size": 20, "pages": 0}

    # CORS 最后注册 = 最外层（preflight OPTIONS 不经过 CSRF 中间件即被应答）；
    # spec：allow_credentials=True 时禁止通配符，只允许显式白名单
    if settings.cors_origins_list:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
        )

    return app


def _case_summary(c: Case) -> dict:
    return {
        "id": c.id, "title": c.title, "status": c.status,
        "owner_id": c.owner_id,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def _case_payload(c: Case) -> dict:
    return {
        **_case_summary(c),
        "description": c.description,
    }


def _bfs_truncate(nodes: list, edges: list,
                  max_nodes: int) -> tuple[list, list]:
    """issue #84：按 BFS 连通性截断超大闭包（保证边可见、结构可读）。

    不做连通截断而按层硬切时，大 hub 闭包的 L0 动辄 2,000+ 节点，
    切出的全是孤立节点、一条边都留不下（实测 2,448 节点闭包
    max_nodes=100 → 0 边）。

    起点选**度最大的节点**：KB 里有 249 个巨型 hub 闭包（单笔交易
    数千笔），其 L0 seed 往往只是 hub 的上游小节点，从 seed 出发
    只能覆盖个位数节点；从 hub 出发才能展示该图的真实中心结构。
    """
    from collections import deque

    by_id = {n.get("id"): n for n in nodes}
    adj: dict[str, list] = {}
    for e in edges:
        s, t = e.get("source"), e.get("target")
        if s in by_id and t in by_id:
            adj.setdefault(s, []).append(t)
            adj.setdefault(t, []).append(s)
    start = min(
        (n.get("id") for n in nodes),
        key=lambda i: (-len(adj.get(i, ())), str(i)),
    )

    order = [start]
    seen = {start}
    queue = deque([start])
    while queue and len(order) < max_nodes:
        u = queue.popleft()
        for v in adj.get(u, ()):
            if v in seen:
                continue
            seen.add(v)
            order.append(v)
            if len(order) >= max_nodes:
                break
            queue.append(v)
    keep_edges = [e for e in edges
                  if e.get("source") in seen and e.get("target") in seen]
    return [by_id[i] for i in order], keep_edges


def _pattern_summary(p) -> dict:
    stats = (p.canonical_subgraph or {}).get("stats", {})
    return {
        "id": p.id, "name": p.name, "source": p.source,
        "provenance": p.provenance, "evidence_grade": p.evidence_grade,
        "seed_address": p.seed_address,
        "node_count": stats.get("node_count"),
        "edge_count": stats.get("edge_count"),
        "embedding_model": p.embedding_model,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _current_stage(row) -> str | None:
    """任务物的当前阶段（worker 写 Redis）。

    processing/failed 都读取：failed 行的最后上报阶段即失败发生阶段，
    前端据此把失败标到正确的步骤（issue #40 验收）；queued 无上报返回
    None。Redis 不可达/键过期（TTL 1h）降级 None，前端回退到第一阶段。
    """
    if row.status not in ("queued", "processing", "failed"):
        return None
    try:
        import redis

        from ..core.config import get_settings

        client = redis.Redis.from_url(get_settings().redis_url,
                                      socket_connect_timeout=1)
        val = client.get(f"judgment:stage:{row.id}")
        return val.decode() if isinstance(val, bytes) else val
    except Exception:  # noqa: BLE001 — 进度缺失降级，不影响轮询主流程
        return None


def _judgment_payload(row) -> dict:
    base = {
        "id": row.id,
        "address": row.address,
        "hops": row.hops,
        "time_window_days": row.time_window_days,
        "status": row.status,
        "stage": _current_stage(row),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "concluded_at": row.concluded_at.isoformat()
        if row.concluded_at else None,
        "data_as_of": row.data_as_of.isoformat() if row.data_as_of else None,
        "data_quality": getattr(row, "data_quality", "complete"),
        "requires_manual_review": bool(getattr(row, "requires_manual_review", False)),
        "poll_url": f"/api/v1/judgments/{row.id}",
    }
    if row.status == "completed":
        snap = row.subgraph_snapshot or {}
        snap_stats = snap.get("stats", {})
        base.update({
            "risk_level": row.risk_level,
            "matched_pattern_id": row.matched_pattern_id,
            "matched_pattern_name": row.matched_pattern_name,
            "confidence": row.confidence,
            "evidence": row.evidence or [],
            "reasoning": row.reasoning,
            "recommended_action": row.recommended_action,
            "subgraph": {"nodes": snap.get("nodes", []),
                         "edges": snap.get("edges", [])},
            "model": row.model,
            "prompt_version": row.prompt_version,
            "builder_version": row.builder_version,
            "latency_ms": row.latency_ms,
            # issue #8：数据质量 / 缺失分支 / 源错误摘要
            "missing_branches": int(snap_stats.get("missing_branches", 0)),
            "source_errors": snap_stats.get("source_errors", []),
        })
    elif row.status == "failed":
        # spec：failed 态不含判断字段（BE-14 断言 risk_level/confidence 为 null）
        base.update({
            "risk_level": None, "confidence": None, "reasoning": None,
            "error_code": row.error_code,
            "error_message": row.error_message,
            "retry_count": row.retry_count,
            "failed_at": row.failed_at.isoformat() if row.failed_at else None,
        })
    return base


def reset_stores() -> None:
    global _refresh_store
    _users_db.clear()
    _refresh_store = RefreshTokenStore()
    # 限流状态随单例重建（issue #23）：Redis 模式下计数在 Redis 侧，
    # 重建只影响降级路径的进程内字典
    from ..core.rate_limit import reset_rate_limiter

    reset_rate_limiter()
    _idempotency_keys.clear()
    close_db_engine()
