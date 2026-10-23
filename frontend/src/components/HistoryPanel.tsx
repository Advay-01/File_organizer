import { useCallback, useEffect, useState } from "react";

/**
 * Run history with per-run undo. Talks only to the local API (app/history_api.py).
 *
 *   <HistoryPanel folder={selectedFolder} />        // runs for one folder
 *   <HistoryPanel />                                 // runs from every folder organized so far
 *
 * Colours come from CSS variables with fallbacks, so it follows your theme:
 *   --surface, --surface-2, --text, --muted, --accent, --warn, --border
 */

type Status = "applied" | "undone" | "partially undone";

interface Run {
  id: number;
  time: string;
  files: number;
  renamed: number;
  folders: Record<string, number>;
  status: Status;
  sample: { from: string; to: string; folder: string }[];
  folder?: string; // present in the global list
}

interface Move {
  from_name: string;
  to_name: string;
  folder: string;
  renamed: boolean;
  restored: boolean;
}

interface UndoResult {
  restored: number;
  skipped: { file: string; reason: string }[];
  status: Status;
}

interface Props {
  folder?: string;
  apiBase?: string;
  onChanged?: () => void; // e.g. refresh your file view after an undo
}

const MAX_MOVES_SHOWN = 100;

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* keep statusText */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export default function HistoryPanel({ folder, apiBase = "http://127.0.0.1:8765", onChanged }: Props) {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [moves, setMoves] = useState<Move[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const keyOf = (r: Run) => `${r.folder ?? folder ?? ""}#${r.id}`;
  const folderOf = (r: Run) => r.folder ?? folder ?? "";

  const load = useCallback(async () => {
    try {
      const q = folder ? `?folder=${encodeURIComponent(folder)}` : "";
      setRuns(await api<Run[]>(`${apiBase}/history${q}`));
      setError(null);
    } catch (e) {
      // a folder with no history yet is not an error worth shouting about
      const msg = e instanceof Error ? e.message : String(e);
      if (folder && msg.includes("No history")) setRuns([]);
      else setError(msg);
    }
  }, [apiBase, folder]);

  useEffect(() => {
    setRuns(null);
    setOpen(null);
    void load();
  }, [load]);

  const toggle = async (r: Run) => {
    const k = keyOf(r);
    if (open === k) return setOpen(null);
    setOpen(k);
    setMoves([]);
    try {
      const d = await api<{ moves: Move[] }>(
        `${apiBase}/history/${r.id}?folder=${encodeURIComponent(folderOf(r))}`,
      );
      setMoves(d.moves);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const undo = async (r: Run) => {
    const ok = window.confirm(
      `Move ${r.files} file${r.files === 1 ? "" : "s"} back to where they were?\n` +
        "Files you have since moved or deleted are left alone.",
    );
    if (!ok) return;
    const k = keyOf(r);
    setBusy(k);
    setNotice(null);
    try {
      const res = await api<UndoResult>(`${apiBase}/history/${r.id}/undo`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder: folderOf(r) }),
      });
      const skipped = res.skipped.length
        ? ` ${res.skipped.length} skipped (${res.skipped[0].file}: ${res.skipped[0].reason}).`
        : "";
      setNotice(`Restored ${res.restored} file${res.restored === 1 ? "" : "s"}.${skipped}`);
      await load();
      setOpen(null);
      onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="hp">
      <style>{CSS}</style>
      <header className="hp-head">
        <h3>History</h3>
        <button className="hp-btn" onClick={() => void load()}>Refresh</button>
      </header>

      {notice && <p className="hp-note" role="status">{notice}</p>}
      {error && <p className="hp-err" role="alert">{error}</p>}
      {runs === null && !error && <p className="hp-muted">Loading…</p>}
      {runs !== null && runs.length === 0 && (
        <p className="hp-muted">No runs yet. Organized folders will show up here, each with its own undo.</p>
      )}

      <ul className="hp-list">
        {runs?.map((r) => {
          const k = keyOf(r);
          const top = Object.entries(r.folders).slice(0, 3);
          return (
            <li key={k} className="hp-card">
              <button className="hp-row" onClick={() => void toggle(r)} aria-expanded={open === k}>
                <span className="hp-main">
                  <strong>{r.files} file{r.files === 1 ? "" : "s"}</strong>
                  {r.renamed > 0 && <span className="hp-chip">{r.renamed} renamed</span>}
                  <span className={`hp-status hp-${r.status.replace(" ", "-")}`}>{r.status}</span>
                </span>
                <span className="hp-sub">
                  {r.time ? new Date(r.time).toLocaleString() : "unknown time"}
                  {r.folder ? ` · ${r.folder}` : ""}
                </span>
                <span className="hp-sub">
                  {top.map(([name, n]) => `${name} (${n})`).join(", ")}
                  {Object.keys(r.folders).length > 3 ? " …" : ""}
                </span>
              </button>

              {open === k && (
                <div className="hp-detail">
                  {moves.length === 0 && <p className="hp-muted">Loading…</p>}
                  <ul>
                    {moves.slice(0, MAX_MOVES_SHOWN).map((m, i) => (
                      <li key={i} className={m.restored ? "hp-done" : ""}>
                        <span className="hp-from">{m.from_name}</span>
                        <span className="hp-arrow">→</span>
                        <span>{m.folder === "." ? "" : `${m.folder}/`}{m.to_name}</span>
                        {m.renamed && <span className="hp-chip">renamed</span>}
                      </li>
                    ))}
                  </ul>
                  {moves.length > MAX_MOVES_SHOWN && (
                    <p className="hp-muted">…and {moves.length - MAX_MOVES_SHOWN} more</p>
                  )}
                  {r.status !== "undone" && (
                    <button className="hp-btn hp-primary" disabled={busy === k} onClick={() => void undo(r)}>
                      {busy === k ? "Undoing…" : r.status === "applied" ? "Undo this run" : "Retry the rest"}
                    </button>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

const CSS = `
.hp{color:var(--text,#ece9ff);font:14px/1.45 system-ui,sans-serif}
.hp-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.hp-head h3{margin:0;font-size:16px}
.hp-list{list-style:none;margin:0;padding:0;display:grid;gap:8px}
.hp-card{background:var(--surface,#16131f);border:1px solid var(--border,#2a2540);border-radius:10px;overflow:hidden}
.hp-row{all:unset;box-sizing:border-box;display:grid;gap:2px;width:100%;padding:10px 12px;cursor:pointer}
.hp-row:hover,.hp-row:focus-visible{background:var(--surface-2,#1d1930)}
.hp-main{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.hp-sub,.hp-muted{color:var(--muted,#9a93b8);font-size:12.5px}
.hp-chip{background:var(--surface-2,#241f3a);border-radius:999px;padding:1px 8px;font-size:11.5px}
.hp-status{margin-left:auto;font-size:12px;border-radius:999px;padding:1px 8px;border:1px solid var(--accent,#7c5cff);color:var(--accent,#7c5cff)}
.hp-undone{border-color:var(--border,#2a2540);color:var(--muted,#9a93b8)}
.hp-partially-undone{border-color:var(--warn,#e0a030);color:var(--warn,#e0a030)}
.hp-detail{border-top:1px solid var(--border,#2a2540);padding:10px 12px}
.hp-detail ul{list-style:none;margin:0 0 10px;padding:0;max-height:240px;overflow:auto;display:grid;gap:3px}
.hp-detail li{display:flex;gap:6px;align-items:baseline;flex-wrap:wrap;font-size:13px}
.hp-from{color:var(--muted,#9a93b8)}
.hp-arrow{color:var(--accent,#7c5cff)}
.hp-done{opacity:.5;text-decoration:line-through}
.hp-btn{background:var(--surface-2,#241f3a);color:inherit;border:1px solid var(--border,#2a2540);border-radius:8px;padding:6px 12px;cursor:pointer}
.hp-btn:disabled{opacity:.6;cursor:default}
.hp-primary{background:var(--accent,#7c5cff);border-color:var(--accent,#7c5cff);color:#fff}
.hp-note{color:var(--accent,#7c5cff)}
.hp-err{color:#ff6b6b}
`;

