#!/usr/bin/env python
"""破坏性用例的库名守卫。

为什么需要它：单测/评估脚本会执行 `DELETE FROM judgments` 这类全表清理，
而它们的 DATABASE_URL 默认就是开发库（`postgresql://pt:pt@localhost:5432/patterntrace`，
与本地栈共用一个 Postgres）。跑一次全量单测就会把开发库里的分析记录清空，
正在查看的 /analyze/<id> 页面随即 404。

约定（与 infra/seed_kb_fixture.py 的 `EXPECTED_E2E_DB_MARKER` 同一套）：
破坏性操作只允许指向**专用库**——本文件把"专用"定义为库名 = `pt_e2e`
或 `patterntrace_test`（也接受任意 `*_test` 后缀）。开发库/生产库一律拒绝。

用法：
    from scripts.db_guard import require_destructive_db, is_destructive_db_allowed

    require_destructive_db("全表清理 judgments")   # 不满足即抛 RuntimeError
"""
from __future__ import annotations

__all__ = ["DESTRUCTIVE_DB_MARKERS", "db_name_of", "is_destructive_db_allowed",
           "require_destructive_db"]

# 专用库名：pt_e2e 为既有 E2E 约定；patterntrace_test 为单测隔离库
DESTRUCTIVE_DB_MARKERS = ("pt_e2e", "patterntrace_test")


def db_name_of(url: str) -> str:
    """从 DATABASE_URL 取库名（忽略 query string）。"""
    return url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]


def current_db_name() -> str:
    from backend.core.config import get_settings

    return db_name_of(get_settings().database_url)


def is_destructive_db_allowed(url: str | None = None) -> bool:
    name = db_name_of(url) if url else current_db_name()
    return name in DESTRUCTIVE_DB_MARKERS or name.endswith("_test")


def require_destructive_db(what: str, url: str | None = None) -> None:
    """确认当前库可以承受破坏性清理；否则抛出带操作指引的错误。

    刻意抛错而不是静默跳过：静默跳过会让"测试通过"变成假象，而抛错能立刻
    告诉当事人"你正对着开发库跑破坏性用例"。CI 与专用库不受影响。
    """
    if is_destructive_db_allowed(url):
        return
    name = db_name_of(url) if url else current_db_name()
    raise RuntimeError(
        f"{what} 会清空整张表，但当前 DATABASE_URL 指向 `{name}`。"
        "破坏性用例只允许在专用库（patterntrace_test / pt_e2e）上运行。\n"
        "  · 单测：bash infra/run_unit_tests.sh（自动建/刷新 patterntrace_test）\n"
        "  · 或手动：export DATABASE_URL=postgresql://pt:pt@localhost:5432/"
        "patterntrace_test\n"
        "  开发库里的分析记录被清掉后无法恢复，正在查看的分析页会直接 404。")
