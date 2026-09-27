"""破坏性用例的库名守卫（scripts/db_guard.py）。

这个守卫的价值在于"错误时大声失败"：单测里有 DELETE FROM judgments 这类
全表清理，一旦指向开发库，正在查看的分析页会立刻 404 且无法恢复。
"""
from __future__ import annotations

import pytest

from scripts.db_guard import (db_name_of, is_destructive_db_allowed,
                              require_destructive_db)


class TestDbName:
    def test_extracts_db_name(self):
        assert db_name_of("postgresql://pt:pt@localhost:5432/patterntrace") == "patterntrace"
        assert db_name_of("postgresql://pt:pt@localhost:5432/pt_e2e") == "pt_e2e"
        assert db_name_of("postgresql://pt:pt@localhost:5432/x_test?sslmode=disable") == "x_test"

    def test_allows_only_dedicated_databases(self):
        # 专用库：允许
        assert is_destructive_db_allowed("postgresql://pt:pt@localhost:5432/patterntrace_test")
        assert is_destructive_db_allowed("postgresql://pt:pt@localhost:5432/pt_e2e")
        assert is_destructive_db_allowed("postgresql://pt:pt@localhost:5432/whatever_test")
        # 开发库 / 生产库：拒绝
        assert not is_destructive_db_allowed("postgresql://pt:pt@localhost:5432/patterntrace")
        assert not is_destructive_db_allowed("postgresql://pt:pt@localhost:5432/prod")
        assert not is_destructive_db_allowed("postgresql://pt:pt@localhost:5432/patterns")

    def test_fails_loudly_with_actionable_message(self):
        with pytest.raises(RuntimeError) as exc:
            require_destructive_db(
                "测试前置清理（DELETE FROM judgments）",
                "postgresql://pt:pt@localhost:5432/patterntrace")
        msg = str(exc.value)
        assert "会清空整张表" in msg
        assert "patterntrace_test" in msg        # 给出该指向哪里
        assert "run_unit_tests.sh" in msg        # 给出怎么跑

    def test_passes_for_dedicated_db(self):
        require_destructive_db(
            "测试前置清理", "postgresql://pt:pt@localhost:5432/patterntrace_test")
