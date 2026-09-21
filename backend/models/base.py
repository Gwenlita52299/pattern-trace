"""SQLAlchemy declarative base + all ORM models — 阶段0 DB schema."""
from __future__ import annotations

import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=_uuid)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(20), default="investigator", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    refresh_tokens = relationship("RefreshToken", back_populates="user", cascade="all, delete-orphan")
    cases = relationship("Case", back_populates="owner")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    family_id = Column(String(36), nullable=False, index=True)
    jti = Column(String(64), unique=True, nullable=False)
    status = Column(String(20), default="active", nullable=False)  # active|revoked|rotated
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="refresh_tokens")


class Case(Base):
    __tablename__ = "cases"

    id = Column(String(36), primary_key=True, default=_uuid)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text, default="")
    status = Column(String(20), default="open", nullable=False)  # open|analyzing|closed
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    owner = relationship("User", back_populates="cases")
    addresses = relationship("CaseAddress", back_populates="case", cascade="all, delete-orphan")


class CaseAddress(Base):
    __tablename__ = "case_addresses"
    __table_args__ = (
        # PK(case_id, address) 语义（BE-24）：重复关联幂等跳过
        UniqueConstraint("case_id", "address", name="uq_case_addresses_case_addr"),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    case_id = Column(String(36), ForeignKey("cases.id"), nullable=False, index=True)
    address = Column(String(62), nullable=False, index=True)
    judgment_id = Column(String(36))  # 关联分析结果摘要（阶段5）
    label = Column(String(100), default="")
    added_at = Column(DateTime(timezone=True), server_default=func.now())

    case = relationship("Case", back_populates="addresses")


class Report(Base):
    """异步报告导出任务（backend-api-spec §4 reports）。

    issue #74：状态机补 queued 排队态（此前投递即 processing，无法区分
    「已入队」与「worker 正在生成」）：
    queued → processing → completed / failed / cancelled
    """
    __tablename__ = "reports"

    id = Column(String(36), primary_key=True, default=_uuid)
    case_id = Column(String(36), ForeignKey("cases.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    format = Column(String(10), nullable=False)  # pdf | html
    status = Column(String(20), default="queued", nullable=False,
                    index=True)  # queued | processing | completed | failed | cancelled
    storage_key = Column(String(200))
    error_code = Column(String(50))
    error_message = Column(Text)
    created_by = Column(String(36))
    completed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class TaskDeadLetter(Base):
    """任务死信（issue #74）：重试耗尽或永久错误的任务档案。

    只读治理用：保留任务类型、业务 ID、尝试次数、最后错误与失败时间，
    支持管理员查看与重新入队。业务对象的失败状态由各自的终态写入维护，
    本表是「为什么失败、试了几次」的可追溯记录（不参与状态机判定）。
    """
    __tablename__ = "task_dead_letters"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    task_type = Column(String(20), nullable=False, index=True)   # analysis | report
    business_id = Column(String(64), nullable=False, index=True)
    queue = Column(String(64))
    attempts = Column(Integer, nullable=False, default=1)
    error_code = Column(String(50))
    last_error = Column(Text)
    failed_at = Column(DateTime(timezone=True), server_default=func.now(),
                       nullable=False, index=True)
    requeued_at = Column(DateTime(timezone=True))
    requeued_by = Column(String(36))


class AuditLog(Base):
    """审计日志（spec §4 完整字段，BE-07/32；CM-10 按 request_id 串联）。"""
    __tablename__ = "audit_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(String(36))          # 匿名请求可空
    request_id = Column(String(36), nullable=False, index=True)
    http_method = Column(String(10), nullable=False)
    http_path = Column(String(500), nullable=False)
    response_status = Column(Integer)
    action = Column(String(50), nullable=False, index=True)
    resource_type = Column(String(50))
    resource_id = Column(String(100))
    action_result = Column(String(20))    # success | failure
    detail = Column(JSONB)
    latency_ms = Column(Integer)
    ip = Column(String(64))
    user_agent = Column(String(300))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class JudgmentEvent(Base):
    """D5 状态机迁移事件（CM-03 断言完整迁移序列的数据源）。"""
    __tablename__ = "judgment_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    judgment_id = Column(String(36),
                         ForeignKey("judgments.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    from_status = Column(String(20))
    to_status = Column(String(20), nullable=False)
    # issue #78：阶段/检索摘要观测点——retrieval 候选快照、分析阶段记录
    # （HTTP 轮询会跳过快阶段，Redis 只存当前值，顺序只能靠持久化行）
    detail = Column(JSONB)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


JUDGMENT_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
# issue #74：报告与判断共用终态集合（取消也是终态，不可再迁移）
TERMINAL_STATUSES = JUDGMENT_TERMINAL_STATUSES


class InvalidStateTransition(RuntimeError):
    """D5 状态机终态不可变（BE-47）：completed/failed 后拒绝任何改写。"""


def assert_transition(current_status: str, next_status: str) -> None:
    if current_status in TERMINAL_STATUSES:
        raise InvalidStateTransition(
            f"judgment in terminal status {current_status!r}; "
            f"refusing transition to {next_status!r}")


class Judgment(Base):
    """LLM 判断状态机 D5: queued → processing → completed / failed.

    backend-api-spec §4：快照 JSONB（LZ4 TOAST）、evidence 引用 D3 ID、
    模型三元组 (model, prompt_version, builder_version) 构成证据链。
    """
    __tablename__ = "judgments"
    __table_args__ = (
        # 同参数幂等的 DB 层保障（BE-46）：至多一条进行中记录
        Index(
            "uq_judgments_active_per_params",
            "address", "hops", "time_window_days",
            unique=True,
            postgresql_where=text("status IN ('queued','processing')"),
        ),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    address = Column(String(62), nullable=False, index=True)
    hops = Column(Integer, default=3, nullable=False)
    time_window_days = Column(Integer, default=90, nullable=False)
    status = Column(String(20), default="queued", nullable=False, index=True)

    # completed 产物
    subgraph_snapshot = Column(JSONB)   # canonical 子图（节点/边 D3 ID 空间）
    subgraph_hash = Column(String(64))  # canonical_subgraph_hash，缓存 key 成员
    risk_level = Column(String(20))     # high | medium | low（#72 删 no_match；历史行保留旧值）
    matched_pattern_id = Column(String(36))
    matched_pattern_name = Column(String(120))
    confidence = Column(Float)
    evidence = Column(JSONB)            # list[str]：addr:/tx:/edge: 前缀 ID
    reasoning = Column(Text)
    recommended_action = Column(String(20))

    # 证据链三元组 + 性能口径
    model = Column(String(200))
    prompt_version = Column(String(20))
    builder_version = Column(String(20))
    latency_ms = Column(Integer)

    # failed 产物
    error_code = Column(String(50))     # LLM_VALIDATION_FAILED | TASK_TIMEOUT | ...
    error_message = Column(Text)
    retry_count = Column(Integer, default=0, nullable=False)
    thinking = Column(Text)             # Qwen3 <think> 审计留痕（llm-judge §7）
    failed_at = Column(DateTime(timezone=True))

    # 结论/数据时间戳（issue #7）：history Judgment 时间版本化的证据链时间字段
    concluded_at = Column(DateTime(timezone=True))  # 进入 completed/failed 的时间
    data_as_of = Column(DateTime(timezone=True))    # 本次分析使用的链上数据时间点

    # 数据质量（issue #8）：部分上游分支失败时保留部分子图并标记 degraded/需人工复核
    data_quality = Column(String(20), default="complete", nullable=False)
    requires_manual_review = Column(Boolean, default=False, nullable=False)

    # issue #78：mock provider 的 per-judgment 场景（E2E HTTP 模式的
    # worker 故障注入通道；仅 LLM_PROVIDER=mock 时经 API 写入）
    mock_scenario = Column(String(100))

    created_by = Column(String(36), ForeignKey("users.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
