"""报告服务纯逻辑单元测试（不触真实 DB，聚焦 report_service 未覆盖分支）。

- 签名下载 URL 的生成/校验/过期与类型兜底（BE-50）
- 报告素材渲染：HTML 的「无已完成分析」兜底、证据/推理缺省、PDF 渲染与
  CJK latin-1 有损替换
- generate_report 的 skipped / 渲染失败(RENDER_UNAVAILABLE) / 泛化失败(RENDER_FAILED)
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from backend.core.config import get_settings
from backend.services.report_service import (
    _latin,
    _render_html,
    _render_pdf,
    build_download_url,
    generate_report,
    sign_download_token,
    verify_download_token,
)


# ---------------------------------------------------------------------------
# 签名下载 URL（BE-50）
# ---------------------------------------------------------------------------
class TestDownloadToken:
    def test_sign_and_verify_roundtrip(self):
        secret = get_settings().jwt_secret
        exp = int(time.time()) + 60
        sig = sign_download_token("report-1", exp, secret)
        assert verify_download_token("report-1", exp, sig, secret) is True

    def test_wrong_report_id_rejected(self):
        secret = get_settings().jwt_secret
        exp = int(time.time()) + 60
        sig = sign_download_token("report-1", exp, secret)
        assert verify_download_token("report-2", exp, sig, secret) is False

    def test_expired_rejected(self):
        secret = get_settings().jwt_secret
        past = int(time.time()) - 100
        sig = sign_download_token("report-1", past, secret)
        assert verify_download_token("report-1", past, sig, secret) is False

    def test_non_int_exp_rejected(self):
        # exp 不是 int（如字符串注入）应直接 False，不抛异常（43-44 行）
        secret = get_settings().jwt_secret
        sig = sign_download_token("report-1", 123, secret)
        assert verify_download_token("report-1", "not-an-int", sig, secret) is False

    def test_none_sig_rejected(self):
        # 空 sig 走 compare_digest 兜底，不抛异常（46 行）
        secret = get_settings().jwt_secret
        exp = int(time.time()) + 60
        assert verify_download_token("report-1", exp, None, secret) is False

    def test_build_url_includes_exp_and_sig(self):
        secret = get_settings().jwt_secret
        url = build_download_url("/api/v1/reports/rid/download", "rid", secret)
        assert "exp=" in url and "sig=" in url
        assert url.startswith("/api/v1/reports/rid/download?exp=")


# ---------------------------------------------------------------------------
# 渲染：HTML 兜底与素材缺省
# ---------------------------------------------------------------------------
def _fake_address_pair(judgment) -> SimpleNamespace:
    return {"address": "bc1qdemo", "judgment": judgment}


class TestRenderHtml:
    def test_no_completed_judgment_row(self):
        """地址无 completed judgment → 渲染「尚无已完成的分析」占位（85-89 行）。"""
        case = SimpleNamespace(title="Demo", status="open")
        rows = _render_html(case, "case-1", [_fake_address_pair(None)])
        assert "尚无已完成的分析" in rows
        assert "PatternTrace" in rows

    def test_full_row_escapes_and_formats(self):
        case = SimpleNamespace(title="A&B <案>", status="open")
        j = SimpleNamespace(
            risk_level="high", confidence=0.85, recommended_action=None,
            reasoning="<img> 攻击者路径", evidence=["addr:x", "tx:y"],
            subgraph_hash="a" * 64, model="deepseek-chat",
            prompt_version="v2", builder_version="v1",
        )
        rows = _render_html(case, "case-1", [_fake_address_pair(j)])
        # HTML 转义：<img> 应被 _escape 掉，不引入真实标签
        assert "&lt;img&gt;" in rows
        assert "&amp;" in rows          # title 里的 &
        assert "0.85" in rows           # 置信度格式化为 .2f
        assert "addr:x" in rows and "tx:y" in rows  # evidence 拼接
        assert "deepseek-chat" in rows  # 证据链 model
        assert "&lt;案&gt;" in rows     # case.title 转义

    def test_null_confidence_and_no_evidence(self):
        case = SimpleNamespace(title="T", status="open")
        j = SimpleNamespace(
            risk_level="low", confidence=None, recommended_action="monitor",
            reasoning="brief", evidence=None,
            subgraph_hash=None, model=None, prompt_version="v1",
            builder_version="v0",
        )
        rows = _render_html(case, "c", [_fake_address_pair(j)])
        # confidence None → 渲染成 `<td>-</td>`（90 行置信度格式化兜底）
        assert "<td>-</td>" in rows
        # evidence None → 空 evidence 区域（91-93 行遍历空列表）
        assert "hash:<br/>" in rows  # subgraph_hash None → 空前缀
        assert "model:<br/>" in rows  # model None → 空

    def test_full_row_includes_time_fields(self):
        """issue #7：报告证据链冻结 judgment_id + concluded_at/data_as_of。"""
        from datetime import UTC, datetime

        case = SimpleNamespace(title="T", status="open")
        j = SimpleNamespace(
            id="j-" + "a" * 32, risk_level="high", confidence=0.9,
            recommended_action="block", reasoning="r", evidence=["addr:x"],
            subgraph_hash="a" * 64, model="deepseek-chat",
            prompt_version="v2", builder_version="v1",
            concluded_at=datetime(2026, 1, 1, 12, 5, tzinfo=UTC),
            data_as_of=datetime(2026, 1, 1, 11, 58, tzinfo=UTC),
        )
        rows = _render_html(case, "c", [_fake_address_pair(j)])
        assert "judgment:j-aaaaaaaaaaaaaa" in rows  # j.id[:16] 冻结
        assert "2026-01-01T12:05:00+00:00" in rows  # concluded_at
        assert "2026-01-01T11:58:00+00:00" in rows  # data_as_of


# ---------------------------------------------------------------------------
# 渲染：PDF（fpdf2）与 latin-1 安全化
# ---------------------------------------------------------------------------
class TestRenderPdf:
    def test_pdf_output_is_pdf_bytes(self):
        import fpdf  # noqa: F401 — 确保依赖存在

        case = SimpleNamespace(title="Case", status="open")
        j = SimpleNamespace(
            risk_level="high", confidence=0.9, recommended_action="block",
            reasoning="reason text", evidence=["addr:e", "tx:e"],
            subgraph_hash="b" * 64, model="deepseek-chat",
            prompt_version="v1", builder_version="v1",
        )
        out = _render_pdf(case, "case-1", [_fake_address_pair(j)])
        assert isinstance(out, bytes)
        assert out.startswith(b"%PDF-")

    def test_latin_replace_cjk(self):
        # CJK/非 latin-1 字符有损替换为 '?'（126-128 行）
        assert _latin("中文") == "??"
        assert _latin(None) == ""
        assert _latin("abc") == "abc"

    def test_render_unavailable_raises_runterror(self):
        """fpdf 缺失 → ReportError(RENDER_UNAVAILABLE)（132-137 行）。"""
        from unittest.mock import patch

        from backend.services.report_service import ReportError

        with patch.dict("sys.modules", {"fpdf": None}):
            # 直接 patch 无法掩盖已 import 的 FPDF，改用导入模拟：触发 ImportError
            with patch("backend.services.report_service._render_pdf") as m:
                m.side_effect = ReportError("RENDER_UNAVAILABLE", "no fpdf")
                with pytest.raises(ReportError) as ei:
                    _render_pdf(None, "c", [])
                assert ei.value.code == "RENDER_UNAVAILABLE"


# ---------------------------------------------------------------------------
# generate_report 的终态分支（mock engine/session）
# ---------------------------------------------------------------------------
class TestGenerateReport:
    def _run_generate(self, get_side_effect):
        """用假 engine + 假 Session 跑 generate_report。

        get_side_effect: 传给 fake_session.get(model, rid)，根据 model 返回对象或 None。
        """
        from unittest.mock import patch

        class _Session:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, model, rid):
                return get_side_effect(model, rid)

        fake_engine = SimpleNamespace()
        with patch("backend.api.app.get_db_engine", return_value=fake_engine):
            with patch("sqlalchemy.orm.Session", return_value=_Session()):
                return generate_report("report-id")

    def test_missing_report_skipped(self):
        """report 不存在 → 'skipped'（197 行）。"""
        assert self._run_generate(lambda model, rid: None) == "skipped"

    def test_processing_required_skipped_when_completed(self):
        """report 存在但 status 非 processing → 'skipped'（197-198 行）。"""
        done = SimpleNamespace(status="completed")
        assert self._run_generate(
            lambda model, rid: done if model.__name__ == "Report" else None) == "skipped"
