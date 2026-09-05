"""issue #12 — live 构建隔离：同步 build 不阻塞 FastAPI 事件循环的线程模型。

覆盖验收标准：
- live 模式（graph_data_mode == "live"）同步构建经 anyio.to_thread 隔离到线程池；
- fixture 模式保持同步（不引入线程切换，避免打破既有测试/CI）；
- 并发/后台任务场景下，缓慢的同步构建不阻塞 healthz / polling。
"""
from __future__ import annotations

import asyncio
import time

import anyio


def _blocking(delay: float, value: str) -> str:
    """模拟慢速同步构建（如 live Esplora 网络请求 + 重试等待）。"""
    time.sleep(delay)
    return value


class TestIsolatedSync:
    def _run(self, coro):
        return asyncio.run(coro)

    def _spy(self, monkeypatch, calls):
        """用记录调用、内部委托真实的 anyio.to_thread.run_sync。"""
        from backend.services import orchestration

        orig = orchestration.anyio.to_thread.run_sync
        calls["orig"] = orig

        def spy(func, *args, **kwargs):
            calls["func"] = func
            calls["args"] = (args, kwargs)
            calls["thread"] = True
            return orig(func, *args, **kwargs)

        monkeypatch.setattr(anyio.to_thread, "run_sync", spy)
        return spy

    def test_live_mode_dispatches_to_anyio_thread(self, monkeypatch):
        """live=True → 通过 anyio.to_thread.run_sync 执行。

        kwargs 经 functools.partial 打包后传入（anyio 3.x run_sync 只收位置参数），
        断言解包 partial 后 func/args 与原始调用一致。
        """
        import functools

        from backend.services.orchestration import _isolated_sync

        calls: dict = {}
        self._spy(monkeypatch, calls)
        out = self._run(_isolated_sync(True, _blocking, 0.01, "v"))
        assert out == "v"
        fn = calls.get("func")
        if isinstance(fn, functools.partial):
            assert fn.func is _blocking
            assert fn.args == (0.01, "v")
        else:
            assert fn is _blocking
        assert calls.get("thread") is True

    def test_fixture_mode_stays_sync(self, monkeypatch):
        """live=False → 直接同步调用，不触发 anyio.to_thread。"""
        from backend.services.orchestration import _isolated_sync

        calls: dict = {}
        self._spy(monkeypatch, calls)
        out = self._run(_isolated_sync(False, _blocking, 0.01, "v"))
        assert out == "v"
        assert calls.get("thread") is None  # 未走线程池

    def test_result_passthrough_both_modes(self):
        from backend.services.orchestration import _isolated_sync

        assert self._run(_isolated_sync(True, _blocking, 0.01, "a")) == "a"
        assert self._run(_isolated_sync(False, _blocking, 0.01, "b")) == "b"


class TestConcurrencyIntegration:
    """TestClient 后台任务跑慢速同步构建时 healthz 不被阻塞（integration）。"""

    def test_healthz_not_blocked_by_slow_background_task(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from backend.services.orchestration import _isolated_sync

        app = FastAPI()
        bg: set[asyncio.Task] = set()

        @app.get("/start")
        async def start():
            async def work():
                # 模拟 live 模式：慢速同步构建隔离到线程池
                return await _isolated_sync(True, _blocking, 0.4, "built")

            t = asyncio.create_task(work())
            bg.add(t)
            t.add_done_callback(bg.discard)
            return {"accepted": True}

        @app.get("/healthz")
        async def healthz():
            return {"ok": True}

        with TestClient(app) as c:
            c.get("/start")  # 后台任务开始（0.4s 阻塞构建）
            t0 = time.monotonic()
            r = c.get("/healthz")  # 应立即返回，不被后台任务阻塞
            elapsed = (time.monotonic() - t0) * 1000
            assert r.json() == {"ok": True}
            assert elapsed < 300, f"healthz blocked for {elapsed:.0f}ms"

            time.sleep(0.6)
            assert len(bg) == 0  # 后台任务已完结（未卡死在 processing）

    def test_fixture_mode_background_completes_fast(self):
        """fixture 模式无 anyio 线程切换：后台任务仍可完成（回归保护）。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from backend.services.orchestration import _isolated_sync

        app = FastAPI()
        bg: set[asyncio.Task] = set()

        @app.get("/start")
        async def start():
            async def work():
                return await _isolated_sync(False, _blocking, 0.01, "done")

            t = asyncio.create_task(work())
            bg.add(t)
            t.add_done_callback(bg.discard)
            return {"accepted": True}

        with TestClient(app) as c:
            c.get("/start")
            time.sleep(0.15)
            assert len(bg) == 0  # 已完成
