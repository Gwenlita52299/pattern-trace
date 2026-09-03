"""案件报告生成（阶段5 · backend-api-spec §3 Reports）。

- HTML：全量 UTF-8，自包含单文件；
- PDF：fpdf2 渲染，核心字体仅 latin-1——CJK 文本以「?」有损替换（MVP 取舍，
  完整 CJK 支持需内嵌 TTF 字体，属 P2）；HTML 版始终保留完整 reasoning。
- 内容内嵌证据链：每条 judgment 的 subgraph_hash / model / prompt_version /
  builder_version（spec §3 报告要求）。

签名下载 URL：HMAC-SHA256(report_id:exp)，15 分钟有效（BE-50）。
"""
from __future__ import annotations

import hashlib
import hmac
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, update

from ..models.base import Case, CaseAddress, Judgment, Report

DOWNLOAD_TTL_SECONDS = 15 * 60
REPORTS_DIR = Path("output/reports")


class ReportError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# 签名下载 URL
# ---------------------------------------------------------------------------
def sign_download_token(report_id: str, exp: int, secret: str) -> str:
    msg = f"{report_id}:{exp}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify_download_token(report_id: str, exp: int, sig: str,
                          secret: str) -> bool:
    if not isinstance(exp, int) or exp < time.time():
        return False
    expected = sign_download_token(report_id, exp, secret)
    return hmac.compare_digest(expected, sig or "")


def build_download_url(base_path: str, report_id: str, secret: str,
                       ttl_seconds: int = DOWNLOAD_TTL_SECONDS) -> str:
    exp = int(time.time()) + ttl_seconds
    sig = sign_download_token(report_id, exp, secret)
    return f"{base_path}?exp={exp}&sig={sig}"


# ---------------------------------------------------------------------------
# 报告素材与渲染
# ---------------------------------------------------------------------------
def _fmt_time(v) -> str:
    """把 datetime 字段格式化为可读字符串；空值/缺省返回 '-'。"""
    if v is None:
        return "-"
    if isinstance(v, str):
        return v
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def collect_case_entries(session, case_id: str) -> list[dict]:
    """每个关联地址取 **冻结** 的 completed judgment 作为报告素材（issue #7）。

    优先使用 `CaseAddress.judgment_id`（该案件关联地址的「当前」分析结论，报告
    生成时即捕获/落盘，后续重新分析不会影响已生成报告）；指针为空（历史地址或
    旧数据）时回退到地址的最新 completed judgment，并把该指针回写冻结。
    报告绝不在此处查询「全局最新」judgment 而绕过案件指针。
    """
    links = session.execute(
        select(CaseAddress).where(CaseAddress.case_id == case_id)
        .order_by(CaseAddress.added_at)).scalars().all()
    entries = []
    for link in links:
        j: Judgment | None = None
        if link.judgment_id:
            j = session.get(Judgment, link.judgment_id)
            # 指针仅指向 completed 结论；指向其它状态则视为无效并回退
            if j is not None and j.status != "completed":
                j = None
        if j is None:
            j = session.execute(
                select(Judgment)
                .where(Judgment.address == link.address,
                       Judgment.status == "completed")
                .order_by(Judgment.created_at.desc(),
                          Judgment.id.desc()).limit(1)).scalar_one_or_none()
            if j is not None:
                link.judgment_id = j.id  # 冻结：回写指针，后续生成不再重查全局最新
        entries.append({"address": link.address, "judgment": j,
                        "case_address": link})
    session.commit()  # 落盘冻结指针（若发生回退写入）
    return entries


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _render_html(case: Case, case_id: str, entries: list[dict]) -> str:
    rows = []
    for e in entries:
        j: Judgment | None = e["judgment"]
        addr = _escape(e["address"])
        if j is None:
            rows.append(
                f"<tr><td class='mono'>{addr}</td>"
                "<td colspan='6' class='dim'>尚无已完成的分析</td></tr>")
            continue
        conf = f"{j.confidence:.2f}" if j.confidence is not None else "-"
        evidence_html = "".join(
            f"<div class='mono'>{_escape(eid)}</div>"
            for eid in (j.evidence or []))
        # issue #7：报告冻结 judgment_id + 时间字段，重建/下载不再重查最新 Judgment
        chain = (f"judgment:{_escape(getattr(j, 'id', '')[:16])}<br/>"
                 f"hash:{(j.subgraph_hash or '')[:16]}<br/>"
                 f"model:{_escape(j.model or '')}<br/>"
                 f"prompt:{j.prompt_version} · builder:{j.builder_version}<br/>"
                 f"concluded:{_escape(_fmt_time(getattr(j, 'concluded_at', None)))}<br/>"
                 f"as_of:{_escape(_fmt_time(getattr(j, 'data_as_of', None)))}")
        rows.append(
            f"<tr><td class='mono'>{addr}</td>"
            f"<td><b>{j.risk_level}</b></td><td>{conf}</td>"
            f"<td>{j.recommended_action or '-'}</td>"
            f"<td>{_escape((j.reasoning or '')[:400])}</td>"
            f"<td>{evidence_html}</td>"
            f"<td class='small mono'>{chain}</td></tr>")

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>PatternTrace 报告 · {_escape(case.title)}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:40px;color:#1e293b}}
h1{{font-size:20px}} table{{border-collapse:collapse;width:100%;margin-top:16px}}
th,td{{border:1px solid #cbd5e1;padding:8px;font-size:12px;text-align:left;vertical-align:top}}
th{{background:#f1f5f9}} .mono{{font-family:ui-monospace,monospace;font-size:11px}}
.small{{font-size:10px;color:#64748b}} .dim{{color:#94a3b8}}
.meta{{margin-top:8px;color:#64748b;font-size:12px}}
</style></head><body>
<h1>PatternTrace 调查报告 · {_escape(case.title)}</h1>
<div class="meta">案件 {case_id} · 状态 {case.status} · 共 {len(entries)} 个地址</div>
<table><thead><tr>
<th>地址</th><th>风险等级</th><th>置信度</th><th>建议动作</th>
<th>推理摘要</th><th>Evidence</th><th>证据链</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>
<p class="meta">Generated by PatternTrace · 本报告含机器判断结果，仅供调查参考。</p>
</body></html>"""


def _latin(text: str | None) -> str:
    """PDF 核心字体安全化：非 latin-1 字符替换为 '?'。"""
    return (text or "").encode("latin-1", "replace").decode("latin-1")


def _render_pdf(case: Case, case_id: str, entries: list[dict]) -> bytes:
    try:
        from fpdf import FPDF
    except ImportError as exc:  # 依赖缺失是配置错误，显式失败而非静默降级
        raise ReportError(
            "RENDER_UNAVAILABLE",
            "PDF rendering requires the 'fpdf2' package") from exc

    class _ReportPDF(FPDF):
        def footer(self):  # 页脚含页码（证据文档基本要求）
            self.set_y(-15)
            self.set_font("Helvetica", "I", 8)
            self.cell(0, 10, f"Page {self.page_no()} of {{nb}}")

    pdf = _ReportPDF()
    pdf.add_page()

    def _line(text: str, *, style: str = "", size: int = 10, h: float = 5):
        """始终从左边界起以整页宽输出一行；规避 cell/multi_cell 光标残留。"""
        pdf.set_font("Helvetica", style, size)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(w=pdf.epw, h=h, text=_latin(text), new_x="LMARGIN",
                       new_y="NEXT")

    _line("PatternTrace Forensics Report", style="B", size=16, h=10)
    _line(f"Case: {case.title}  [{case.status}]", size=11, h=7)
    _line(f"Case ID: {case_id}", size=11, h=7)
    _line(f"Addresses: {len(entries)}   Generated: "
          f"{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
          size=11, h=7)
    pdf.ln(4)

    for e in entries:
        j: Judgment | None = e["judgment"]
        _line(f"Address: {e['address']}", style="B", size=12, h=7)
        if j is None:
            _line("  No completed analysis yet.", style="I", size=10)
            pdf.ln(2)
            continue
        conf = f"{j.confidence:.2f}" if j.confidence is not None else "-"
        _line(f"  risk={j.risk_level}  confidence={conf}  "
              f"action={j.recommended_action}")
        if j.reasoning:
            _line(f"  reasoning: {j.reasoning[:500]}")
        _line(f"  evidence chain: hash={(j.subgraph_hash or '')[:24]} "
              f"model={j.model} prompt={j.prompt_version} "
              f"builder={j.builder_version}")
        _line(f"  judgment={getattr(j, 'id', '')[:16]} "
              f"concluded={_fmt_time(getattr(j, 'concluded_at', None))} "
              f"data_as_of={_fmt_time(getattr(j, 'data_as_of', None))}")
        for eid in (j.evidence or [])[:10]:
            _line(f"    - {eid}")
        pdf.ln(3)

    return bytes(pdf.output())


def generate_report(report_id: str) -> str:
    """同步执行渲染并落盘；由 API 进程后台任务或 worker 调用。

    返回最终状态（completed/failed/skipped）。
    """
    from sqlalchemy.orm import Session

    from ..api.app import get_db_engine

    engine = get_db_engine()
    with Session(engine) as session:
        report = session.get(Report, report_id)
        if report is None or report.status != "processing":
            return "skipped"
        case = session.get(Case, report.case_id)

        def _fail(code: str, message: str) -> None:
            session.rollback()
            session.execute(
                update(Report)
                .where(Report.id == report_id, Report.status == "processing")
                .values(status="failed", error_code=code,
                        error_message=message[:500]))
            session.commit()

        try:
            entries = collect_case_entries(session, report.case_id)
            if report.format == "pdf":
                payload: bytes | str = _render_pdf(
                    case, report.case_id, entries)
                suffix = ".pdf"
            else:
                payload = _render_html(case, report.case_id, entries)
                suffix = ".html"

            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            storage_key = f"{report_id}{suffix}"
            (REPORTS_DIR / storage_key).write_bytes(
                payload if isinstance(payload, bytes) else payload.encode())

            session.execute(
                update(Report)
                .where(Report.id == report_id, Report.status == "processing")
                .values(status="completed", storage_key=storage_key,
                        completed_at=datetime.now(UTC)))
            session.commit()
            return "completed"
        except ReportError as exc:
            _fail(exc.code, str(exc))
            return "failed"
        except Exception as exc:  # noqa: BLE001 — 渲染失败进终态而非悬挂
            _fail("RENDER_FAILED", repr(exc))
            return "failed"
