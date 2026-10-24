import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { AlertTriangle, Check, ChevronDown, FolderClock, LoaderCircle, RefreshCw, RotateCcw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJson } from "../lib/api";
import { Button } from "./ui/button";

/**
 * Run history with per-run undo. Talks only to the local API (app/history_api.py).
 *
 *   <HistoryPanel folder={selectedFolder} />        // runs for one folder
 *   <HistoryPanel />                                 // runs from every folder organized so far
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
  folder?: string;
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
  onChanged?: () => void;
}

interface UndoNotice {
  tone: "success" | "warning";
  text: string;
}

const MAX_MOVES_SHOWN = 100;

const STATUS_LABELS: Record<Status, string> = {
  applied: "Completed",
  undone: "Undone",
  "partially undone": "Partially undone",
};

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  return fetchJson<T>(url, init, "The History request failed.");
}

function formatTimestamp(value: string) {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) {
    return { date: "Unknown date", time: value || "Time unavailable" };
  }
  return {
    date: date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }),
    time: date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }),
  };
}

export default function HistoryPanel({ folder, apiBase = "http://127.0.0.1:8765", onChanged }: Props) {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [moves, setMoves] = useState<Move[]>([]);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<UndoNotice | null>(null);
  const detailRequest = useRef(0);
  const reducedMotion = useReducedMotion();

  const keyOf = (run: Run) => `${run.folder ?? folder ?? ""}#${run.id}`;
  const folderOf = (run: Run) => run.folder ?? folder ?? "";

  const load = useCallback(async () => {
    try {
      const query = folder ? `?folder=${encodeURIComponent(folder)}` : "";
      setRuns(await api<Run[]>(`${apiBase}/history${query}`));
      setError(null);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : String(caught);
      if (folder && message.includes("No history")) setRuns([]);
      else setError(message);
    }
  }, [apiBase, folder]);

  useEffect(() => {
    setRuns(null);
    setOpen(null);
    setNotice(null);
    void load();
  }, [load]);

  const toggle = async (run: Run) => {
    const key = keyOf(run);
    if (open === key) {
      detailRequest.current += 1;
      setOpen(null);
      setDetailLoading(false);
      return;
    }

    const requestId = ++detailRequest.current;
    setOpen(key);
    setMoves([]);
    setDetailLoading(true);
    setError(null);
    try {
      const detail = await api<{ moves: Move[] }>(
        `${apiBase}/history/${run.id}?folder=${encodeURIComponent(folderOf(run))}`,
      );
      if (detailRequest.current === requestId) setMoves(detail.moves);
    } catch (caught) {
      if (detailRequest.current === requestId) {
        setError(caught instanceof Error ? caught.message : String(caught));
      }
    } finally {
      if (detailRequest.current === requestId) setDetailLoading(false);
    }
  };

  const undo = async (run: Run) => {
    const confirmed = window.confirm(
      `Move ${run.files} file${run.files === 1 ? "" : "s"} back to where they were?\n` +
        "Files you have since moved or deleted are left alone.",
    );
    if (!confirmed) return;

    const key = keyOf(run);
    setBusy(key);
    setNotice(null);
    setError(null);
    try {
      const result = await api<UndoResult>(`${apiBase}/history/${run.id}/undo`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder: folderOf(run) }),
      });
      const skipped = result.skipped.length
        ? ` ${result.skipped.length} skipped (${result.skipped[0].file}: ${result.skipped[0].reason}).`
        : "";
      setNotice({
        tone: result.status === "undone" ? "success" : "warning",
        text: `Restored ${result.restored} file${result.restored === 1 ? "" : "s"}.${skipped}`,
      });
      await load();
      detailRequest.current += 1;
      setOpen(null);
      onChanged?.();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="history-panel" aria-labelledby="history-panel-heading">
      <header className="history-toolbar">
        <div>
          <p className="ui-eyebrow text-ink-muted">Saved runs</p>
          <h2 id="history-panel-heading" className="history-title">Operation history</h2>
          <p className="history-intro">Each run preserves its original move path so you can review before undoing.</p>
        </div>
        <Button variant="secondary" size="sm" onClick={() => void load()}>
          <RefreshCw size={14} /> Refresh
        </Button>
      </header>

      {notice && (
        <div className="history-feedback" data-tone={notice.tone} role="status">
          {notice.tone === "success" ? <Check size={16} /> : <AlertTriangle size={16} />}
          <span>{notice.text}</span>
        </div>
      )}
      {error && (
        <div className="history-feedback" data-tone="danger" role="alert">
          <AlertTriangle size={16} />
          <span>{error}</span>
        </div>
      )}

      {runs === null && !error && (
        <div className="history-state" role="status">
          <LoaderCircle size={18} className="animate-spin text-accent-text" />
          <div><strong>Loading operation history</strong><span>Reading local run records…</span></div>
        </div>
      )}

      {runs !== null && runs.length === 0 && (
        <div className="history-empty">
          <span className="history-empty-icon"><FolderClock size={21} /></span>
          <div>
            <strong>No completed runs yet</strong>
            <p>After files are organized, each operation will appear here with its own safe Undo action.</p>
          </div>
        </div>
      )}

      {runs && runs.length > 0 && (
        <ol className="history-list">
          {runs.map((run) => {
            const key = keyOf(run);
            const expanded = open === key;
            const timestamp = formatTimestamp(run.time);
            const folders = Object.entries(run.folders);
            const visibleFolders = folders.slice(0, 3);
            return (
              <li key={key} className="history-run" data-expanded={expanded}>
                <button
                  type="button"
                  className="history-run-trigger"
                  aria-expanded={expanded}
                  onClick={() => void toggle(run)}
                >
                  <span className="history-run-icon"><FolderClock size={17} /></span>
                  <span className="history-run-main">
                    <span className="history-run-heading">
                      <strong>{run.files} {run.files === 1 ? "file" : "files"}</strong>
                      {run.renamed > 0 && <span className="history-chip">{run.renamed} renamed</span>}
                    </span>
                    <span className="history-run-time" title={run.time}>
                      <span>{timestamp.date}</span>
                      <span aria-hidden="true">·</span>
                      <span>{timestamp.time}</span>
                      {run.folder && <span className="history-folder-path" title={run.folder}>{run.folder}</span>}
                    </span>
                    <span className="history-folder-list">
                      {visibleFolders.map(([name, count]) => <span key={name} className="history-chip">{name} <span>{count}</span></span>)}
                      {folders.length > visibleFolders.length && <span className="history-chip">+{folders.length - visibleFolders.length} folders</span>}
                    </span>
                  </span>
                  <span className="history-status" data-status={run.status}>{STATUS_LABELS[run.status]}</span>
                  <ChevronDown size={16} className="history-chevron" data-open={expanded} />
                </button>

                <AnimatePresence initial={false}>
                  {expanded && (
                    <motion.div
                      className="history-detail-shell"
                      initial={reducedMotion ? false : { height: 0, opacity: 0 }}
                      animate={{ height: "auto", opacity: 1 }}
                      exit={reducedMotion ? undefined : { height: 0, opacity: 0 }}
                      transition={{ duration: reducedMotion ? 0 : 0.2, ease: "easeOut" }}
                    >
                      <div className="history-detail">
                        <div className="history-detail-heading">
                          <span>Moves in this run</span>
                          {!detailLoading && <span>{moves.length} shown</span>}
                        </div>
                        {detailLoading && (
                          <p className="history-detail-loading"><LoaderCircle size={14} className="animate-spin" /> Loading move details…</p>
                        )}
                        {!detailLoading && moves.length === 0 && <p className="history-muted">No move details were returned for this run.</p>}
                        {moves.length > 0 && (
                          <ul className="history-moves">
                            {moves.slice(0, MAX_MOVES_SHOWN).map((move, index) => (
                              <li key={`${move.from_name}-${move.to_name}-${index}`} data-restored={move.restored}>
                                <span className="history-move-name">{move.from_name}</span>
                                <span className="history-move-arrow" aria-hidden="true">→</span>
                                <span className="history-move-name">{move.folder === "." ? "" : `${move.folder}/`}{move.to_name}</span>
                                {move.renamed && <span className="history-chip">renamed</span>}
                                {move.restored && <span className="history-chip" data-tone="muted">restored</span>}
                              </li>
                            ))}
                          </ul>
                        )}
                        {moves.length > MAX_MOVES_SHOWN && (
                          <p className="history-muted">Showing the first {MAX_MOVES_SHOWN} of {moves.length} recorded moves.</p>
                        )}
                        {run.status !== "undone" && (
                          <div className="history-detail-footer">
                            <p>Undo only moves files still present at their recorded destinations. Other items are skipped safely.</p>
                            <Button
                              size="sm"
                              variant={run.status === "partially undone" ? "secondary" : "default"}
                              disabled={busy === key}
                              onClick={() => void undo(run)}
                            >
                              {busy === key ? <LoaderCircle size={14} className="animate-spin" /> : <RotateCcw size={14} />}
                              {busy === key ? "Undoing…" : run.status === "partially undone" ? "Retry remaining" : "Undo this run"}
                            </Button>
                          </div>
                        )}
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
