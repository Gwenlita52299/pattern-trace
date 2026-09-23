"use client";
// issue #75：模式版本/审核/索引生命周期面板。
//
// - 状态徽标：当前生效版本、生命周期（active/draft/deprecated）与索引状态
// - 版本历史：不可变链路（含来源 edit/rollback/baseline），可查看任一版本
//   的内容摘要并渲染其子图（与当前版本的差异用节点/边/描述/哈希标注）
// - admin 操作：编辑（待审核 draft）、审核通过/驳回、停用、回滚到历史版本
//
// 权限：入口显隐依据 auth store 的 role，真正的判定仍在后端 require_role
// （非 admin 即便构造请求也会得到 403）。
import { useCallback, useEffect, useMemo, useState } from "react";

import GraphCanvas from "@/components/GraphCanvas";
import {
  deprecatePattern,
  editPattern,
  getPatternRevision,
  listPatternRevisions,
  reviewPattern,
  rollbackPattern,
  type PatternDetail,
  type PatternRevision,
} from "@/lib/api";
import { useAuthStore } from "@/store/auth";
import type { GraphEdge, GraphNode } from "@/store/analysis";

export const STATUS_LABELS: Record<string, string> = {
  active: "生效中",
  draft: "待审核",
  deprecated: "已停用",
  rejected: "已驳回",
};

export const INDEX_LABELS: Record<string, string> = {
  indexed: "索引完成",
  pending: "索引中",
  failed: "索引失败",
};

export const ORIGIN_LABELS: Record<string, string> = {
  ingest: "建库初版",
  baseline: "建库初版",
  edit: "内容编辑",
  rollback: "回滚",
  current: "当前版本",
};

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

export function indexLabel(status: string): string {
  return INDEX_LABELS[status] ?? status;
}

export function originLabel(origin: string): string {
  return ORIGIN_LABELS[origin] ?? origin;
}

/** 参与差异比较的一侧（当前版本来自详情、历史版本来自 revisions 端点）。 */
export interface DiffSide {
  name?: string;
  description?: string;
  node_count?: number;
  edge_count?: number;
  canonical_subgraph?: {
    nodes?: unknown[];
    edges?: unknown[];
  };
}

function shape(side: DiffSide): [number, number] | null {
  if (side.canonical_subgraph) {
    return [side.canonical_subgraph.nodes?.length ?? 0,
            side.canonical_subgraph.edges?.length ?? 0];
  }
  if (side.node_count !== undefined) return [side.node_count, side.edge_count ?? 0];
  return null;
}

/** 两个版本的可见差异（只列变化项，避免噪声）。 */
export function revisionDiff(base: DiffSide, target: DiffSide): string[] {
  const out: string[] = [];
  if (base.name !== undefined && target.name !== undefined
      && base.name !== target.name) {
    out.push(`名称：${base.name} → ${target.name}`);
  }
  if ((base.description ?? "") !== (target.description ?? "")) {
    out.push("描述：已修改");
  }
  const a = shape(base);
  const b = shape(target);
  if (a && b && (a[0] !== b[0] || a[1] !== b[1])) {
    out.push(`规模：${a[0]}/${a[1]} → ${b[0]}/${b[1]} 节点/边`);
  }
  return out;
}

function statusTone(status: string): string {
  if (status === "active") {
    return "border-pt-amber/40 bg-pt-amber/5 text-pt-amber-hi";
  }
  if (status === "deprecated") return "border-pt-line bg-pt-panel-2 text-pt-muted";
  return "border-pt-line bg-pt-panel-2 text-pt-muted";
}

export default function PatternLifecycle({
  pattern,
  onChanged,
}: {
  pattern: PatternDetail;
  onChanged: () => void;
}) {
  const role = useAuthStore((s) => s.role);
  const isAdmin = role === "admin";

  const [items, setItems] = useState<PatternRevision[]>([]);
  const [current, setCurrent] = useState<{
    revision: number; status: string; index_status: string;
  } | null>(null);
  const [selected, setSelected] = useState<PatternRevision | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [draftName, setDraftName] = useState(pattern.name);
  const [draftNote, setDraftNote] = useState("");

  const revision = pattern.revision ?? current?.revision ?? 1;

  const load = useCallback(async () => {
    try {
      const data = await listPatternRevisions(pattern.id);
      setItems(data.items);
      setCurrent(data.current);
    } catch (e) {
      setError((e as Error).message || "版本历史加载失败");
    }
  }, [pattern.id]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    setDraftName(pattern.name);
  }, [pattern.name]);

  const pendingDraft = useMemo(
    () => items.find((r) => r.status === "draft") ?? null,
    [items],
  );
  const currentRevision = useMemo(
    () => items.find((r) => r.revision === revision) ?? null,
    [items, revision],
  );
  const selectedGraph = useMemo(() => {
    const g = selected?.canonical_subgraph;
    if (!g) return null;
    return {
      nodes: (g.nodes ?? []) as unknown as GraphNode[],
      edges: (g.edges ?? []) as unknown as GraphEdge[],
    };
  }, [selected]);

  async function guard(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await fn();
    } catch (e) {
      const err = e as { status?: number; message?: string };
      setError(err.status === 409
        ? "版本冲突：该模式已被他人修改，请刷新后重试"
        : err.status === 422
          ? `状态不允许该操作（${err.message ?? ""}）`
          : err.message ?? "操作失败");
    } finally {
      setBusy(false);
    }
  }

  async function viewRevision(rev: number) {
    await guard(async () => {
      setSelected(await getPatternRevision(pattern.id, rev));
    });
  }

  async function submitEdit() {
    await guard(async () => {
      const created = await editPattern(pattern.id, {
        expected_revision: revision,
        name: draftName,
        change_note: draftNote || undefined,
      });
      setEditing(false);
      setDraftNote("");
      setNotice(`已创建待审核版本 r${created.revision}（索引重算中）`);
      await load();
    });
  }

  async function decide(rev: number, approve: boolean) {
    await guard(async () => {
      await reviewPattern(pattern.id, { revision: rev, approve });
      setNotice(approve ? `已发布 r${rev}` : `已驳回 r${rev}`);
      setSelected(null);
      await load();
      onChanged();
    });
  }

  async function deprecate() {
    await guard(async () => {
      await deprecatePattern(pattern.id, revision);
      setNotice("已停用：该模式退出召回");
      await load();
      onChanged();
    });
  }

  async function rollback(to: number) {
    await guard(async () => {
      const rolled = await rollbackPattern(pattern.id, {
        to_revision: to, expected_revision: revision,
      });
      setNotice(`已回滚为 r${rolled.revision}`);
      setSelected(null);
      await load();
      onChanged();
    });
  }

  return (
    <section
      className="mt-4 rounded-xl border border-pt-line bg-pt-panel p-4"
      data-testid="pattern-lifecycle"
    >
      <div className="flex flex-wrap items-center gap-2 text-[10px]">
        <span className="font-mono text-pt-muted">r{revision}</span>
        <span
          className={`rounded border px-1.5 py-0.5 ${statusTone(pattern.status ?? "active")}`}
          data-testid="lifecycle-status"
        >
          {statusLabel(pattern.status ?? "active")}
        </span>
        <span
          className={`rounded border border-pt-line px-1.5 py-0.5 ${
            pattern.index_status === "failed" ? "text-red-400" : "text-pt-muted"
          }`}
          data-testid="lifecycle-index"
        >
          {indexLabel(pattern.index_status ?? "indexed")}
        </span>
        {pattern.reviewed_by && (
          <span className="font-mono text-pt-faint">
            审核人 {pattern.reviewed_by.slice(0, 8)}…
          </span>
        )}
        {pendingDraft && (
          <span className="rounded border border-pt-amber/40 px-1.5 py-0.5 text-pt-amber-hi">
            待审核 r{pendingDraft.revision}
            {pendingDraft.index_status !== "indexed"
              ? ` · ${indexLabel(pendingDraft.index_status)}`
              : ""}
          </span>
        )}
      </div>

      {notice && <p className="mt-2 text-[11px] text-pt-amber-hi">{notice}</p>}
      {error && (
        <p className="mt-2 text-[11px] text-red-400" data-testid="lifecycle-error">
          {error}
        </p>
      )}

      {isAdmin && (
        <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px]">
          <button
            onClick={() => {
              setEditing((v) => !v);
              setSelected(null);
            }}
            disabled={busy}
            data-testid="btn-edit"
            className="rounded border border-pt-line px-2 py-1 text-pt-muted hover:border-pt-amber hover:text-pt-amber-hi disabled:opacity-40"
          >
            编辑
          </button>
          {pendingDraft && (
            <>
              <button
                onClick={() => void decide(pendingDraft.revision, true)}
                disabled={busy || pendingDraft.index_status !== "indexed"}
                data-testid="btn-approve"
                className="rounded border border-pt-amber/40 px-2 py-1 text-pt-amber-hi hover:bg-pt-amber/10 disabled:opacity-40"
              >
                审核通过
              </button>
              <button
                onClick={() => void decide(pendingDraft.revision, false)}
                disabled={busy}
                data-testid="btn-reject"
                className="rounded border border-pt-line px-2 py-1 text-pt-muted hover:border-pt-red"
              >
                驳回
              </button>
            </>
          )}
          {(pattern.status ?? "active") !== "deprecated" && (
            <button
              onClick={() => void deprecate()}
              disabled={busy}
              data-testid="btn-deprecate"
              className="rounded border border-pt-line px-2 py-1 text-pt-muted hover:border-pt-red"
            >
              停用
            </button>
          )}
        </div>
      )}

      {editing && isAdmin && (
        <div className="mt-3 rounded border border-pt-line bg-pt-panel-2 p-3">
          <label className="block text-[10px] text-pt-muted">
            名称
            <input
              value={draftName}
              onChange={(e) => setDraftName(e.target.value)}
              data-testid="edit-name"
              className="mt-1 w-full rounded border border-pt-line bg-pt-panel px-2 py-1 font-mono text-[11px] text-pt-ink"
            />
          </label>
          <label className="mt-2 block text-[10px] text-pt-muted">
            变更说明
            <input
              value={draftNote}
              onChange={(e) => setDraftNote(e.target.value)}
              placeholder="为什么改"
              data-testid="edit-note"
              className="mt-1 w-full rounded border border-pt-line bg-pt-panel px-2 py-1 font-mono text-[11px] text-pt-ink"
            />
          </label>
          <p className="mt-2 text-[10px] text-pt-faint">
            编辑创建待审核版本，当前版本在审核通过前继续生效（不影响召回）。
          </p>
          <button
            onClick={() => void submitEdit()}
            disabled={busy || draftName.trim() === ""}
            data-testid="btn-submit-edit"
            className="mt-2 rounded border border-pt-amber/40 px-2 py-1 text-[11px] text-pt-amber-hi hover:bg-pt-amber/10 disabled:opacity-40"
          >
            提交待审核
          </button>
        </div>
      )}

      <div className="mt-3" data-testid="revision-list">
        <p className="text-[10px] uppercase tracking-[0.2em] text-pt-muted">
          版本历史
        </p>
        <ul className="mt-2 space-y-1">
          {items.map((item) => (
            <li
              key={`${item.revision}-${item.origin}`}
              className="flex flex-wrap items-center gap-2 rounded border border-pt-line px-2 py-1 font-mono text-[10px]"
              data-testid={`revision-${item.revision}`}
            >
              <span className="text-pt-ink">r{item.revision}</span>
              <span className="text-pt-muted">{originLabel(item.origin)}</span>
              <span
                className={item.status === "active" ? "text-pt-amber-hi" : "text-pt-faint"}
              >
                {statusLabel(item.status)}
              </span>
              <span
                className={item.index_status === "failed" ? "text-red-400" : "text-pt-faint"}
              >
                {indexLabel(item.index_status)}
              </span>
              {item.change_note && (
                <span className="max-w-[220px] truncate text-pt-faint">
                  {item.change_note}
                </span>
              )}
              <button
                onClick={() => void viewRevision(item.revision)}
                data-testid={`btn-view-${item.revision}`}
                className="text-pt-amber-hi underline"
              >
                查看
              </button>
              {isAdmin && item.revision !== revision && (
                <button
                  onClick={() => void rollback(item.revision)}
                  disabled={busy}
                  data-testid={`btn-rollback-${item.revision}`}
                  className="text-pt-muted underline hover:text-pt-ink"
                >
                  回滚到此版本
                </button>
              )}
            </li>
          ))}
        </ul>
      </div>

      {selected && (
        <div
          className="mt-3 rounded border border-pt-line bg-pt-panel-2 p-3"
          data-testid="revision-detail"
        >
          <div className="flex items-center justify-between">
            <p className="font-mono text-[11px] text-pt-ink">
              r{selected.revision} · {originLabel(selected.origin)} ·{" "}
              {statusLabel(selected.status)}
            </p>
            <button
              onClick={() => setSelected(null)}
              className="text-[10px] text-pt-faint hover:text-pt-ink"
            >
              关闭
            </button>
          </div>
          {selected.revision !== revision && (
            <ul
              className="mt-2 space-y-0.5 text-[10px] text-pt-muted"
              data-testid="revision-diff"
            >
              {revisionDiff(pattern, selected).map((line) => (
                <li key={line}>· {line}</li>
              ))}
            </ul>
          )}
          <p className="mt-1 break-all font-mono text-[10px] text-pt-faint">
            {selected.content_hash}
          </p>
          {selected.index_status === "failed" && selected.index_error && (
            <p className="mt-1 break-all text-[10px] text-red-400">
              {selected.index_error}
            </p>
          )}
          {selectedGraph && selectedGraph.nodes.length > 0 && (
            <div className="mt-2 h-[320px] overflow-hidden rounded border border-pt-line bg-[#0c0f13]">
              <GraphCanvas subgraph={selectedGraph} highlightIds={new Set()} />
            </div>
          )}
        </div>
      )}
    </section>
  );
}
