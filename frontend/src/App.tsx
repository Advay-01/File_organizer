import * as Dialog from "@radix-ui/react-dialog";
import { invoke, isTauri } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import {
  AlertTriangle,
  ArrowRight,
  Check,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  FileText,
  Folder,
  FolderOpen,
  FolderSearch,
  FolderTree,
  HardDrive,
  History as HistoryIcon,
  LoaderCircle,
  LockKeyhole,
  Monitor,
  Moon,
  Plug,
  RotateCcw,
  Settings2,
  ShieldCheck,
  Sparkles,
  Sun,
  X,
} from "lucide-react";
import { useEffect, useId, useMemo, useState } from "react";
import HistoryPanel from "./components/HistoryPanel";
import ProviderStudio from "./components/ProviderStudio";
import { Button } from "./components/ui/button";
import { fetchJson } from "./lib/api";

const BROWSER_API = import.meta.env.VITE_SORTLY_API ?? "http://127.0.0.1:8765";
const DESKTOP = isTauri();
const THEME_STORAGE_KEY = "sortly.theme";

type WorkspaceView = "organize" | "history" | "providers" | "settings";
type ThemePreference = "system" | "light" | "dark";

const VIEW_META: Record<WorkspaceView, { label: string; description: string }> = {
  organize: {
    label: "Organize",
    description: "Scan, review, and move files with an explicit plan.",
  },
  history: {
    label: "History",
    description: "Review previous runs and reverse changes safely.",
  },
  providers: {
    label: "Providers",
    description: "Choose the models and endpoints used for analysis.",
  },
  settings: {
    label: "Settings",
    description: "Control how Sortly looks on this device.",
  },
};

const NAV_ITEMS = [
  { id: "organize", label: "Organize", icon: FolderSearch },
  { id: "history", label: "History", icon: HistoryIcon },
  { id: "providers", label: "Providers", icon: Plug },
  { id: "settings", label: "Settings", icon: Settings2 },
] as const satisfies ReadonlyArray<{ id: WorkspaceView; label: string; icon: typeof FolderSearch }>;

function workspaceViewFromHash(): WorkspaceView {
  const route = window.location.hash.replace(/^#\/?/, "").split("/")[0];
  return route === "history" || route === "providers" || route === "settings" ? route : "organize";
}

function readThemePreference(): ThemePreference {
  try {
    const saved = window.localStorage.getItem(THEME_STORAGE_KEY);
    return saved === "light" || saved === "dark" ? saved : "system";
  } catch {
    return "system";
  }
}

const INITIAL_THEME_PREFERENCE = readThemePreference();
document.documentElement.dataset.theme = INITIAL_THEME_PREFERENCE;

type PlanItem = {
  id: string;
  name: string;
  current_path: string;
  destination_name: string;
  target_path: string;
  reasoning: string;
  renamed: boolean;
  selected: boolean;
  category: string;
  confidence: "high" | "medium" | "low" | "insufficient";
  evidence_excerpt: string;
  evidence_sources: string[];
  warnings: string[];
  filename_conflict: "none" | "generic" | "misleading" | "extension_mismatch";
  evidence_sufficient: boolean;
  detected_type: string;
  extraction_status: string;
  rename_reason: string;
  sensitive: boolean;
};

type Preview = {
  plan_id: string;
  source: string;
  files: PlanItem[];
  analysis_seconds: number;
  already_organized: number;
  plan_warnings: string[];
  requires_confirmation: boolean;
  scan_summary: { files_seen: number; skipped: number; reasons: Record<string, number>; truncated: boolean };
};
type HierarchyNode = { name: string; files: string[]; folders: HierarchyNode[] };
type ApplyResult = { moved: number; kept_in_root: number; hierarchy: HierarchyNode };
type Completion = ApplyResult & { requested: number };
type FileEdit = { accepted: boolean; target_path: string; destination_name: string };
type ApiRuntime = {
  state: "starting" | "ready" | "error";
  base_url?: string | null;
  detail?: string | null;
};

async function request<T>(apiBase: string, path: string, body?: unknown): Promise<T> {
  if (!apiBase) throw new Error("The local Sortly service is not ready yet.");
  return fetchJson<T>(`${apiBase}${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  }, "The local organizer could not complete that request.");
}

export default function App() {
  const [source, setSource] = useState("");
  const [recursive, setRecursive] = useState(false);
  const [rename, setRename] = useState(true);
  const [reorganize, setReorganize] = useState(false);
  const [allowSensitiveCloud, setAllowSensitiveCloud] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [fileEdits, setFileEdits] = useState<Record<string, FileEdit>>({});
  const [busy, setBusy] = useState<"preview" | "apply" | "undo" | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [undoAvailable, setUndoAvailable] = useState(false);
  const [completion, setCompletion] = useState<Completion | null>(null);
  const [riskAcknowledged, setRiskAcknowledged] = useState(false);
  const [activeView, setActiveView] = useState<WorkspaceView>(workspaceViewFromHash);
  const [themePreference, setThemePreference] = useState<ThemePreference>(INITIAL_THEME_PREFERENCE);
  const reducedMotion = useReducedMotion();
  const [apiRuntime, setApiRuntime] = useState<ApiRuntime>(() => ({
    state: DESKTOP ? "starting" : "ready",
    base_url: DESKTOP ? null : BROWSER_API,
  }));
  const apiBase = apiRuntime.base_url ?? "";
  const apiReady = apiRuntime.state === "ready" && Boolean(apiBase);

  useEffect(() => {
    const syncRoute = () => setActiveView(workspaceViewFromHash());
    if (!/^#\/(organize|history|providers|settings)(?:\/|$)/.test(window.location.hash)) {
      window.history.replaceState({ view: "organize" }, "", "#/organize");
    }
    window.addEventListener("popstate", syncRoute);
    window.addEventListener("hashchange", syncRoute);
    return () => {
      window.removeEventListener("popstate", syncRoute);
      window.removeEventListener("hashchange", syncRoute);
    };
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = themePreference;
    document.title = `${VIEW_META[activeView].label} · Sortly`;
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, themePreference);
    } catch {
      // A blocked storage API should not make the appearance controls unusable.
    }
  }, [activeView, themePreference]);

  useEffect(() => {
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }, [activeView]);

  function navigateTo(view: WorkspaceView) {
    if (view === activeView) {
      if (window.location.hash !== `#/${view}`) window.history.replaceState({ view }, "", `#/${view}`);
      return;
    }
    window.history.pushState({ view }, "", `#/${view}`);
    setActiveView(view);
  }

  useEffect(() => {
    if (!DESKTOP) return;
    let active = true;
    let timer: number | undefined;

    async function poll() {
      try {
        const status = await invoke<ApiRuntime>("get_api_status");
        if (!active) return;
        if (status.state === "ready" && status.base_url) {
          const health = await fetch(`${status.base_url}/api/health`, { cache: "no-store" });
          if (!health.ok) throw new Error(`Health check failed with HTTP ${health.status}.`);
        }
        setApiRuntime(status);
        if (status.state === "starting") {
          timer = window.setTimeout(() => void poll(), 150);
        }
      } catch (caught) {
        if (!active) return;
        const detail = caught instanceof Error ? caught.message : "Could not start the local service.";
        setApiRuntime({ state: "error", detail });
      }
    }

    void poll();
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, []);

  const groups = useMemo(() => {
    const grouped = new Map<string, PlanItem[]>();
    for (const file of preview?.files ?? []) {
      grouped.set(file.target_path, [...(grouped.get(file.target_path) ?? []), file]);
    }
    return [...grouped.entries()];
  }, [fileEdits, preview]);
  const selectedCount = Object.values(fileEdits).filter((edit) => edit.accepted).length;
  const invalidEditCount = useMemo(() => {
    if (!preview) return 0;
    return preview.files.filter((file) => {
      const edit = fileEdits[file.id];
      const accepted = edit?.accepted ?? file.selected;
      const destination = edit?.target_path ?? file.target_path;
      const filename = edit?.destination_name ?? file.destination_name;
      return accepted && (!destination.trim() || !filename.trim());
    }).length;
  }, [fileEdits, preview]);

  function resetPreview() {
    setPreview(null);
    setFileEdits({});
    setRiskAcknowledged(false);
  }

  async function chooseFolder() {
    setError("");
    try {
      if (DESKTOP) {
        const selected = await open({
          directory: true,
          multiple: false,
          title: "Choose a folder to organize",
        });
        if (typeof selected !== "string") return;
        setSource(selected);
      } else {
        const result = await request<{ source: string }>(apiBase, "/api/choose-folder", {});
        setSource(result.source);
      }
      resetPreview();
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "Folder picker unavailable.";
      if (message !== "No folder was selected.") setError(message);
    }
  }

  async function createPreview() {
    if (!source.trim() || !apiReady) return;
    setBusy("preview");
    setError("");
    setMessage("");
    resetPreview();
    try {
      const result = await request<Preview>(apiBase, "/api/preview", {
        source,
        recursive,
        rename,
        reorganize,
        allow_sensitive_cloud: allowSensitiveCloud,
      });
      setPreview(result);
      setSource(result.source);
      setFileEdits(Object.fromEntries(result.files.map((file) => [file.id, {
        accepted: file.selected,
        target_path: file.target_path,
        destination_name: file.destination_name,
      }])));
      setCompletion(null);
      const cached = result.already_organized ? ` · ${result.already_organized} already organized skipped` : "";
      setMessage(result.files.length ? `Preview ready · ${result.files.length} files · ${result.analysis_seconds}s${cached}` : result.already_organized ? `${result.already_organized} files are already organized.` : "No eligible files found.");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not preview this folder.");
    } finally {
      setBusy(null);
    }
  }

  async function applyPlan() {
    if (!preview || !apiReady || invalidEditCount > 0 || (preview.requires_confirmation && !riskAcknowledged)) return;
    setConfirmOpen(false);
    setBusy("apply");
    setError("");
    try {
      const result = await request<ApplyResult>(apiBase, "/api/apply", {
        plan_id: preview.plan_id,
        risk_acknowledged: riskAcknowledged,
        files: Object.entries(fileEdits).map(([id, edit]) => ({
          id,
          accepted: edit.accepted,
          target_path: edit.target_path,
          destination_name: edit.destination_name,
        })),
      });
      setMessage("");
      setCompletion({ ...result, requested: selectedCount });
      setUndoAvailable(result.moved > 0);
      resetPreview();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not organize the selected files.");
    } finally {
      setBusy(null);
    }
  }

  async function undo() {
    if (!source.trim() || !apiReady) return;
    setBusy("undo");
    setError("");
    try {
      const result = await request<{ restored: number }>(apiBase, "/api/undo", { source });
      setMessage(result.restored ? `Restored ${result.restored} files.` : "There was no organization run to undo.");
      setUndoAvailable(false);
      setCompletion(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Undo could not be completed.");
    } finally {
      setBusy(null);
    }
  }

  function toggleTarget(target: string) {
    const files = groups.find(([name]) => name === target)?.[1] ?? [];
    const allAccepted = files.every((file) => fileEdits[file.id]?.accepted);
    setFileEdits((current) => {
      const next = { ...current };
      for (const file of files) {
        next[file.id] = { ...next[file.id], accepted: !allAccepted };
      }
      return next;
    });
  }

  async function openFolder() {
    if (!source.trim() || !apiReady) return;
    setError("");
    try {
      await request(apiBase, "/api/open-folder", { source });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not open the folder.");
    }
  }

  return (
    <div className="min-h-screen text-ink">
      <aside className="ui-sidebar app-sidebar fixed inset-y-0 left-0 z-40 flex flex-col border-r px-3 py-5">
        <div className="mb-8 flex items-center gap-3 px-2">
          <div className="app-brand-mark grid size-10 shrink-0 place-items-center rounded-lg bg-accent text-accent-ink shadow-[var(--shadow-accent)]">
            <SortlyMark />
          </div>
          <div className="min-w-0">
            <div className="font-display text-[17px] font-semibold leading-none tracking-[-0.02em]">sortly</div>
            <div className="ui-eyebrow mt-1.5 text-ink-muted">File intelligence</div>
          </div>
        </div>

        <nav aria-label="Primary navigation" className="space-y-1">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            const active = activeView === item.id;
            return (
              <button
                key={item.id}
                type="button"
                className="app-nav-item"
                data-active={active}
                aria-current={active ? "page" : undefined}
                onClick={() => navigateTo(item.id)}
              >
                <span className="app-nav-indicator" aria-hidden="true" />
                <Icon size={17} strokeWidth={1.8} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>

        <div className="my-5 border-t border-line" />

        <div className="space-y-1">
          <p className="ui-eyebrow mb-2 px-3 text-ink-muted">Quick actions</p>
          <Button variant="ghost" size="sm" className="w-full justify-start" onClick={undo} disabled={!undoAvailable || busy !== null || !apiReady}>
            <RotateCcw size={16} /> Undo last move
          </Button>
        </div>

        <div className="mt-auto rounded-lg border border-line bg-surface-secondary/70 p-3">
          <div className="flex items-center gap-2 text-xs font-medium text-ink-soft">
            <span className="size-2 rounded-full bg-success" /> Local workspace
          </div>
          <p className="mt-2 text-[11px] leading-relaxed text-ink-muted">Files stay on this computer. Moves require your confirmation.</p>
        </div>
      </aside>

      <main className="app-main min-h-screen">
        <header className="ui-header sticky top-0 z-30 flex h-16 items-center justify-between border-b px-7">
          <div className="flex items-center gap-2 text-xs text-ink-muted">
            Workspace <ChevronRight size={13} /> <span className="font-medium text-ink">{VIEW_META[activeView].label}</span>
          </div>
          <div className="flex items-center gap-2 rounded-full border border-line bg-surface-secondary/70 px-3 py-1.5 text-xs text-ink-soft">
            <span className={`size-1.5 rounded-full ${apiReady ? "bg-success" : apiRuntime.state === "error" ? "bg-danger" : "bg-warning"}`} />
            {apiReady ? "Local API ready" : apiRuntime.state === "error" ? "Local API unavailable" : "Starting local API"}
          </div>
        </header>

        <div className="app-content">
          {activeView === "organize" && (
          <motion.div key="organize" initial={reducedMotion ? false : { opacity: 0, y: 7 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.2, ease: "easeOut" }}>
            <div className="ui-eyebrow mb-3 flex items-center gap-2 text-accent-text"><Sparkles size={14} /> Your AI workspace</div>
            <h1 className="font-display text-[2rem] font-semibold leading-[1.15] tracking-[-0.035em]">A place for everything.</h1>
            <p className="mt-3 text-sm text-ink-muted">Pick a folder. Preview the plan. Let AI do the sorting.</p>

            <section className="ui-panel mt-8 p-6">
              <label className="ui-eyebrow mb-3 block text-ink-muted">Source folder</label>
              <div className="ui-input-shell flex items-center gap-3 p-2 pl-3">
                <HardDrive size={17} className="shrink-0 text-ink-muted" />
                <input
                  value={source}
                  onChange={(event) => { setSource(event.target.value); resetPreview(); }}
                  className="min-w-0 flex-1 bg-transparent text-sm text-ink outline-none placeholder:text-ink-faint"
                  placeholder={String.raw`C:\Users\you\Documents`}
                />
                <Button variant="secondary" size="sm" onClick={chooseFolder} disabled={busy !== null || !apiReady}><Folder size={14} /> Browse</Button>
              </div>
              <div className="mt-5 flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
                <div className="flex flex-wrap items-center gap-x-5 gap-y-3 lg:flex-1">
                  <label className="flex cursor-pointer items-center gap-2 text-xs text-ink-muted">
                    <input type="checkbox" checked={recursive} onChange={(event) => { setRecursive(event.target.checked); resetPreview(); }} className="size-3.5 accent-accent" />
                    Include subfolders
                  </label>
                  <label className="flex cursor-pointer items-center gap-2 text-xs text-ink-muted">
                    <input type="checkbox" checked={rename} onChange={(event) => { setRename(event.target.checked); resetPreview(); }} className="size-3.5 accent-accent" />
                    Suggest cleaner file names
                  </label>
                  <label className="flex cursor-pointer items-center gap-2 text-xs text-ink-muted">
                    <input type="checkbox" checked={reorganize} onChange={(event) => { setReorganize(event.target.checked); resetPreview(); }} className="size-3.5 accent-accent" />
                    Reorganize already sorted files
                  </label>
                  <label className="flex cursor-pointer items-center gap-2 text-xs text-ink-muted">
                    <input type="checkbox" checked={allowSensitiveCloud} onChange={(event) => { setAllowSensitiveCloud(event.target.checked); resetPreview(); }} className="size-3.5 accent-warning" />
                    Allow redacted cloud analysis for sensitive files
                  </label>
                </div>
                <div className="flex items-center gap-4 lg:ml-auto">
                  <span className="hidden items-center gap-2 text-xs text-ink-muted sm:flex"><ShieldCheck size={15} className="text-success" /> Preview only · No files moved</span>
                  <Button onClick={createPreview} disabled={busy !== null || !source.trim() || !apiReady}>
                    {busy === "preview" ? <LoaderCircle size={16} className="animate-spin" /> : <Sparkles size={16} />}
                    Analyze folder <ArrowRight size={15} />
                  </Button>
                </div>
              </div>
            </section>

            <AnimatePresence initial={false}>
              {busy === "preview" && (
                <motion.div
                  role="status"
                  className="workflow-status mt-4"
                  initial={reducedMotion ? false : { opacity: 0, y: -4 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={reducedMotion ? undefined : { opacity: 0, y: -4 }}
                  transition={{ duration: reducedMotion ? 0 : 0.2, ease: "easeOut" }}
                >
                  <span className="workflow-status-icon"><LoaderCircle size={17} className="animate-spin" /></span>
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm font-medium text-ink">Scanning and analyzing</span>
                    <span className="mt-0.5 block text-xs text-ink-muted">Sortly is building a preview from the selected folder. No files will move during this step.</span>
                  </span>
                  <span className="workflow-indeterminate" aria-hidden="true" />
                </motion.div>
              )}
              {busy === "apply" && (
                <motion.div
                  role="status"
                  aria-live="polite"
                  className="workflow-status mt-4"
                  data-tone="accent"
                  initial={reducedMotion ? false : { opacity: 0, y: -4 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={reducedMotion ? undefined : { opacity: 0, y: -4 }}
                  transition={{ duration: reducedMotion ? 0 : 0.2, ease: "easeOut" }}
                >
                  <span className="workflow-status-icon"><LoaderCircle size={17} className="animate-spin" /></span>
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm font-medium text-ink">Applying the confirmed plan</span>
                    <span className="mt-0.5 block text-xs text-ink-muted">Moving {selectedCount} selected {selectedCount === 1 ? "file" : "files"}. Results appear only after the local service responds.</span>
                  </span>
                  <span className="workflow-indeterminate" aria-hidden="true" />
                </motion.div>
              )}
            </AnimatePresence>

            <AnimatePresence>
              {message && <Notice tone="success" text={message} />}
              {error && <Notice tone="error" text={error} />}
              {apiRuntime.state === "error" && <Notice tone="error" text={`Local API unavailable. ${apiRuntime.detail ?? ""}`.trim()} />}
              {preview && preview.scan_summary.skipped > 0 && (
                <Notice tone="error" text={`${preview.scan_summary.skipped} files were skipped by scan safety limits${preview.scan_summary.truncated ? " and the file-count limit was reached" : ""}.`} />
              )}
              {preview?.plan_warnings.map((warning) => <Notice key={warning} tone="error" text={warning} />)}
            </AnimatePresence>

            {preview && <PreviewSummary preview={preview} selectedCount={selectedCount} groupCount={groups.length} />}

            <AnimatePresence>
              {completion && (
                <CompletionSummary
                  completion={completion}
                  busy={busy !== null}
                  onUndo={undo}
                  onOpenFolder={openFolder}
                  onViewHistory={() => navigateTo("history")}
                />
              )}
            </AnimatePresence>

            <AnimatePresence>
              {preview && preview.files.length > 0 && (
                <motion.section
                  initial={reducedMotion ? false : { opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={reducedMotion ? undefined : { opacity: 0, y: -4 }}
                  transition={{ duration: reducedMotion ? 0 : 0.24, ease: "easeOut" }}
                  className="ui-panel mt-6 overflow-hidden"
                >
                  <div className="flex items-center justify-between border-b border-line p-5">
                    <div><h2 className="font-display font-semibold tracking-tight">Proposed organization</h2><p className="mt-1 text-xs text-ink-muted">Review the evidence, then accept, edit, or reject each file. Group switches apply to every file in that target.</p></div>
                    <span className="rounded-full border border-line bg-surface-secondary px-2.5 py-1 font-mono text-[11px] text-ink-soft">{selectedCount} / {preview.files.length} selected{invalidEditCount > 0 ? ` · ${invalidEditCount} need attention` : ""}</span>
                  </div>
                  <div className="divide-y divide-line">
                    {groups.map(([target, files], groupIndex) => {
                      const selected = files.every((file) => fileEdits[file.id]?.accepted);
                      return <motion.div
                        key={target}
                        className="p-5"
                        initial={reducedMotion ? false : { opacity: 0, y: 5 }}
                        animate={{ opacity: 1, y: 0 }}
                        transition={{ duration: reducedMotion ? 0 : 0.2, delay: reducedMotion ? 0 : Math.min(groupIndex * 0.035, 0.18), ease: "easeOut" }}
                      >
                        <div className="flex items-center justify-between gap-4">
                          <div className="flex min-w-0 items-center gap-3"><Folder size={17} className="shrink-0 text-accent-text" /><div className="min-w-0"><p className="truncate text-sm font-medium">{target}</p><p className="mt-0.5 text-xs text-ink-muted">{files.length} {files.length === 1 ? "file" : "files"}</p></div></div>
                          <button role="switch" aria-checked={selected} onClick={() => toggleTarget(target)} className="ui-switch" data-checked={selected}>
                            <span className="ui-switch-thumb" />
                            <span className="sr-only">Toggle {target}</span>
                          </button>
                        </div>
                        <div className="mt-4 space-y-3 border-l border-line pl-4">
                          {files.map((file) => (
                            <FilePreviewRow
                              key={file.id}
                              file={file}
                              edit={fileEdits[file.id]}
                              onChange={(edit) => setFileEdits((current) => ({ ...current, [file.id]: edit }))}
                            />
                          ))}
                        </div>
                      </motion.div>;
                    })}
                  </div>
                  <div className="flex items-center justify-between border-t border-line p-5">
                    <div className="flex items-center gap-2 text-xs text-ink-muted"><LockKeyhole size={14} /> The shown plan is what will be submitted. The backend remains authoritative for path normalization and collisions.</div>
                    <Button onClick={() => setConfirmOpen(true)} disabled={busy !== null || selectedCount === 0 || invalidEditCount > 0}>{busy === "apply" && <LoaderCircle size={15} className="animate-spin" />} {busy === "apply" ? "Applying…" : invalidEditCount > 0 ? "Fix required fields" : "Review & organize"}</Button>
                  </div>
                  {invalidEditCount > 0 && (
                    <div className="plan-validation-summary" role="alert">
                      <AlertTriangle size={15} />
                      <span>{invalidEditCount} selected {invalidEditCount === 1 ? "file needs" : "files need"} a non-empty destination and filename before you can continue.</span>
                    </div>
                  )}
                </motion.section>
              )}
            </AnimatePresence>

          </motion.div>
          )}

          {activeView === "history" && (
            <motion.div key="history" initial={reducedMotion ? false : { opacity: 0, y: 7 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.2, ease: "easeOut" }}>
              <div className="ui-eyebrow mb-3 flex items-center gap-2 text-accent-text"><HistoryIcon size={14} /> Activity log</div>
              <h1 className="font-display text-[2rem] font-semibold leading-[1.15] tracking-[-0.035em]">Every move, reversible.</h1>
              <p className="mt-3 max-w-2xl text-sm leading-relaxed text-ink-muted">Review organization runs across your workspace. Each run keeps its original undo path.</p>

              <section className="ui-panel mt-8 p-6">
                {apiReady
                  ? <HistoryPanel apiBase={apiBase} onChanged={() => { setUndoAvailable(false); setCompletion(null); }} />
                  : <ServiceNotice runtime={apiRuntime} subject="History" />}
              </section>
            </motion.div>
          )}

          {activeView === "providers" && (
            <motion.div key="providers" initial={reducedMotion ? false : { opacity: 0, y: 7 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.2, ease: "easeOut" }}>
              <div className="ui-eyebrow mb-3 flex items-center gap-2 text-accent-text"><Plug size={14} /> Model routing</div>
              <h1 className="font-display text-[2rem] font-semibold leading-[1.15] tracking-[-0.035em]">Choose what powers analysis.</h1>
              <p className="mt-3 max-w-2xl text-sm leading-relaxed text-ink-muted">Configure a primary model and explicit fallbacks. Credentials remain encrypted by the local backend.</p>

              <section className="ui-panel mt-8 p-6">
                {apiReady
                  ? <ProviderStudio open onOpenChange={() => undefined} apiBase={apiBase} presentation="page" />
                  : <ServiceNotice runtime={apiRuntime} subject="Provider configuration" />}
              </section>
            </motion.div>
          )}

          {activeView === "settings" && (
            <motion.div key="settings" initial={reducedMotion ? false : { opacity: 0, y: 7 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.2, ease: "easeOut" }}>
              <SettingsPanel preference={themePreference} onChange={setThemePreference} />
            </motion.div>
          )}
        </div>
      </main>

      <Dialog.Root open={confirmOpen} onOpenChange={setConfirmOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="ui-overlay fixed inset-0 z-40" />
          <Dialog.Content className="ui-dialog fixed left-1/2 top-1/2 z-50 w-[min(92vw,30rem)] -translate-x-1/2 -translate-y-1/2 p-6 focus:outline-none">
            <div className="grid size-11 place-items-center rounded-lg bg-accent-soft text-accent-text"><FolderOpen size={21} /></div>
            <Dialog.Title className="font-display mt-4 text-lg font-semibold tracking-tight">Organize these files?</Dialog.Title>
            <Dialog.Description className="mt-2 text-sm leading-relaxed text-ink-soft">Sortly will submit {selectedCount} selected files using the edits above. The backend may normalize paths or resolve collisions before moving. Unselected files stay in the source folder.</Dialog.Description>
            {preview?.requires_confirmation && (
              <label className="mt-4 flex items-start gap-2 rounded-lg border border-warning/30 bg-warning-soft p-3 text-xs text-warning">
                <input type="checkbox" checked={riskAcknowledged} onChange={(event) => setRiskAcknowledged(event.target.checked)} className="mt-0.5 accent-warning" />
                <span>I reviewed the unusual plan warnings and want to continue. The plan has not been rewritten automatically.</span>
              </label>
            )}
            <div className="mt-6 flex gap-3">
              <Dialog.Close asChild><Button variant="secondary" className="flex-1">Cancel</Button></Dialog.Close>
              <Button className="flex-1" onClick={applyPlan} disabled={busy !== null || !apiReady || invalidEditCount > 0 || Boolean(preview?.requires_confirmation && !riskAcknowledged)}>{busy === "apply" && <LoaderCircle size={16} className="animate-spin" />} Confirm moves</Button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}

function SortlyMark() {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" width="21" height="21" fill="none">
      <path d="M3.75 7.25c0-1.1.9-2 2-2h4.1l1.8 2h6.6c1.1 0 2 .9 2 2v7.5c0 1.1-.9 2-2 2H5.75c-1.1 0-2-.9-2-2v-9.5Z" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
      <path d="M8 13h6.5M8 16h4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      <path d="M17.5 9.25v2h-2" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function ServiceNotice({ runtime, subject }: { runtime: ApiRuntime; subject: string }) {
  const failed = runtime.state === "error";
  return (
    <div className={`flex items-start gap-3 rounded-lg border p-4 ${failed ? "border-danger/25 bg-danger-soft" : "border-line bg-surface-secondary"}`} role="status">
      <span className={`mt-1.5 size-2 shrink-0 rounded-full ${failed ? "bg-danger" : "bg-warning"}`} />
      <div>
        <p className={`text-sm font-medium ${failed ? "text-danger" : "text-ink"}`}>
          {failed ? `${subject} is unavailable.` : `Starting the local service…`}
        </p>
        <p className="mt-1 text-xs leading-relaxed text-ink-muted">
          {failed ? runtime.detail ?? "Restart Sortly to reconnect to the local service." : `${subject} will appear when the local API is ready.`}
        </p>
      </div>
    </div>
  );
}

function SettingsPanel({
  preference,
  onChange,
}: {
  preference: ThemePreference;
  onChange: (preference: ThemePreference) => void;
}) {
  const themes = [
    { id: "system", label: "System", description: "Follow the Windows app theme.", icon: Monitor },
    { id: "dark", label: "Dark", description: "Use the graphite workspace.", icon: Moon },
    { id: "light", label: "Light", description: "Use the lighter workspace.", icon: Sun },
  ] as const;
  const activeTheme = themes.find((theme) => theme.id === preference) ?? themes[0];
  const ActiveThemeIcon = activeTheme.icon;

  return (
    <>
      <div className="ui-eyebrow mb-3 flex items-center gap-2 text-accent-text"><Settings2 size={14} /> Preferences</div>
      <h1 className="font-display text-[2rem] font-semibold leading-[1.15] tracking-[-0.035em]">Make the workspace yours.</h1>
      <p className="mt-3 max-w-2xl text-sm leading-relaxed text-ink-muted">Only preferences implemented by the current frontend appear here.</p>

      <section className="ui-panel mt-8 overflow-hidden" aria-labelledby="appearance-heading">
        <div className="border-b border-line p-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2 text-ink"><ActiveThemeIcon size={17} className="text-accent-text" /><h2 id="appearance-heading" className="font-display text-lg font-semibold tracking-tight">Appearance</h2></div>
            <span className="settings-current"><Check size={12} /> Active: {activeTheme.label}</span>
          </div>
          <p className="mt-1.5 text-xs leading-relaxed text-ink-muted">System follows Windows automatically. Explicit themes are saved for this desktop app only.</p>
        </div>
        <div className="grid gap-3 p-6 lg:grid-cols-3" role="radiogroup" aria-label="App theme">
          {themes.map((theme) => {
            const Icon = theme.icon;
            const selected = preference === theme.id;
            return (
              <label
                key={theme.id}
                className="app-theme-option"
                data-active={selected}
              >
                <input
                  type="radio"
                  name="appearance"
                  value={theme.id}
                  checked={selected}
                  onChange={() => onChange(theme.id)}
                  className="sr-only"
                />
                <span className="app-theme-option-icon"><Icon size={18} /></span>
                <span className="min-w-0 text-left">
                  <span className="block text-sm font-semibold text-ink">{theme.label}</span>
                  <span className="mt-1 block text-xs leading-relaxed text-ink-muted">{theme.description}</span>
                </span>
                <span className="app-theme-selection">
                  {selected && <span className="app-theme-active"><Check size={11} /> Active</span>}
                  <span className="app-radio-dot" aria-hidden="true" />
                </span>
              </label>
            );
          })}
        </div>
        <p className="settings-current-note" aria-live="polite">
          {preference === "system"
            ? "Sortly is following the current Windows app theme."
            : `Sortly is using the ${activeTheme.label.toLowerCase()} theme on this device.`}
        </p>
      </section>

      <section className="ui-panel mt-6 p-6">
        <div className="flex items-start gap-3">
          <ShieldCheck size={18} className="mt-0.5 shrink-0 text-success" />
          <div>
            <h2 className="text-sm font-semibold text-ink">Local behavior</h2>
            <p className="mt-1 max-w-2xl text-xs leading-relaxed text-ink-muted">
              Reduced motion and increased contrast continue to follow Windows accessibility settings. Sortly does not yet expose controls for indexing, shortcuts, or notifications, so none are shown here.
            </p>
          </div>
        </div>
      </section>
    </>
  );
}

function PreviewSummary({ preview, selectedCount, groupCount }: {
  preview: Preview;
  selectedCount: number;
  groupCount: number;
}) {
  const reducedMotion = useReducedMotion();
  const skippedReasons = Object.entries(preview.scan_summary.reasons ?? {}).filter(([, count]) => count > 0);
  return (
    <motion.section
      initial={reducedMotion ? false : { opacity: 0, y: 5 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: reducedMotion ? 0 : 0.22, ease: "easeOut" }}
      className="ui-panel mt-4 overflow-hidden"
      aria-label="Scan and preview summary"
    >
      <div className="preview-summary">
        <div className="summary-metric"><span className="summary-label">Files seen</span><strong>{preview.scan_summary.files_seen}</strong></div>
        <div className="summary-metric"><span className="summary-label">Plan items</span><strong>{preview.files.length}</strong></div>
        <div className="summary-metric"><span className="summary-label">Destination groups</span><strong>{groupCount}</strong></div>
        <div className="summary-metric"><span className="summary-label">Selected</span><strong>{selectedCount}</strong></div>
        <div className="summary-metric"><span className="summary-label">Analysis</span><strong>{preview.analysis_seconds}s</strong></div>
        {preview.scan_summary.skipped > 0 && (
          <div className="summary-metric" data-tone="warning"><span className="summary-label">Skipped</span><strong>{preview.scan_summary.skipped}</strong></div>
        )}
        {preview.already_organized > 0 && (
          <div className="summary-metric"><span className="summary-label">Already organized</span><strong>{preview.already_organized}</strong></div>
        )}
      </div>
      {(skippedReasons.length > 0 || preview.scan_summary.truncated) && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 border-t border-warning/20 bg-warning-soft px-5 py-3 text-xs text-warning">
          {preview.scan_summary.truncated && <span>File-count safety limit reached.</span>}
          {skippedReasons.map(([reason, count]) => <span key={reason}>{count} skipped: {reason}</span>)}
        </div>
      )}
    </motion.section>
  );
}

function CompletionSummary({
  completion,
  busy,
  onUndo,
  onOpenFolder,
  onViewHistory,
}: {
  completion: Completion;
  busy: boolean;
  onUndo: () => void;
  onOpenFolder: () => void;
  onViewHistory: () => void;
}) {
  const reducedMotion = useReducedMotion();
  const notReported = Math.max(completion.requested - completion.moved, 0);
  const complete = notReported === 0;
  const movedNothing = completion.moved === 0;
  const StatusIcon = complete ? Check : movedNothing ? X : AlertTriangle;
  const toneClass = complete ? "success" : movedNothing ? "danger" : "warning";
  const title = complete
    ? "Organization complete"
    : movedNothing
      ? "No files were moved"
      : "Plan partially completed";

  return (
    <motion.section
      initial={reducedMotion ? false : { opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      exit={reducedMotion ? undefined : { opacity: 0 }}
      transition={{ duration: reducedMotion ? 0 : 0.36, ease: "easeOut" }}
      className="ui-panel mt-6 overflow-hidden"
      aria-labelledby="completion-heading"
    >
      <div className={`completion-header completion-header--${toneClass}`}>
        <div className="completion-icon"><StatusIcon size={20} /></div>
        <div>
          <div className="ui-eyebrow">{title}</div>
          <h2 id="completion-heading" className="font-display mt-2 text-2xl font-semibold tracking-tight">
            {completion.moved} {completion.moved === 1 ? "file" : "files"} moved
          </h2>
          <p className="mt-2 max-w-2xl text-sm text-ink-soft">
            {complete
              ? "Every selected file was reported as moved by the local service."
              : `${notReported} selected ${notReported === 1 ? "file was" : "files were"} not reported as moved. The current API does not provide individual failure reasons.`}
          </p>
        </div>
      </div>

      <div className="completion-metrics">
        <div className="summary-metric"><span className="summary-label">Requested</span><strong>{completion.requested}</strong></div>
        <div className="summary-metric" data-tone="success"><span className="summary-label">Moved</span><strong>{completion.moved}</strong></div>
        {notReported > 0 && <div className="summary-metric" data-tone="warning"><span className="summary-label">Not reported moved</span><strong>{notReported}</strong></div>}
        <div className="summary-metric"><span className="summary-label">Kept in source</span><strong>{completion.kept_in_root}</strong></div>
      </div>

      {completion.moved > 0 && (
        <div className="p-6">
          <div className="ui-eyebrow mb-3 flex items-center gap-2 text-ink-muted"><FolderTree size={15} className="text-accent-text" /> Confirmed destinations</div>
          <p className="mb-4 text-xs text-ink-muted">This tree is built only from destination paths returned by the local service.</p>
          <HierarchyTree node={completion.hierarchy} />
        </div>
      )}

      <div className="flex flex-wrap justify-end gap-3 border-t border-line p-5">
        <Button variant="ghost" onClick={onViewHistory} disabled={busy}><HistoryIcon size={15} /> View history</Button>
        <Button variant="secondary" onClick={onUndo} disabled={busy || completion.moved === 0}><RotateCcw size={15} /> Undo</Button>
        <Button onClick={onOpenFolder} disabled={busy}><ExternalLink size={15} /> Open folder</Button>
      </div>
    </motion.section>
  );
}

function FilePreviewRow({ file, edit, onChange }: {
  file: PlanItem;
  edit: FileEdit | undefined;
  onChange: (edit: FileEdit) => void;
}) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const detailsId = useId();
  const validationId = useId();
  const reducedMotion = useReducedMotion();
  const current = edit ?? {
    accepted: file.selected,
    target_path: file.target_path,
    destination_name: file.destination_name,
  };
  const nameChanged = current.destination_name !== file.name;
  const destinationInvalid = current.accepted && !current.target_path.trim();
  const filenameInvalid = current.accepted && !current.destination_name.trim();
  const confidenceClass = file.confidence === "high"
    ? "border-success/30 bg-success-soft text-success"
    : file.confidence === "medium"
      ? "border-warning/30 bg-warning-soft text-warning"
      : "border-line-strong bg-surface-secondary text-ink-soft";
  return (
    <article data-file-id={file.id} className="plan-file" data-excluded={!current.accepted}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <label className="flex cursor-pointer items-start gap-2 text-sm">
          <input
            type="checkbox"
            checked={current.accepted}
            onChange={(event) => onChange({ ...current, accepted: event.target.checked })}
            className="mt-0.5 accent-accent"
          />
          <span>
            <span className="block text-xs font-medium text-ink">{current.accepted ? "Include this file" : "File excluded"}</span>
            <span className="mt-0.5 block text-[11px] text-ink-muted">{file.evidence_sufficient ? "Evidence supports this plan" : "Limited evidence"}</span>
          </span>
        </label>
        <span className={`rounded-full border px-2 py-0.5 text-[11px] ${confidenceClass}`}>{file.confidence} confidence</span>
      </div>

      <div className="filename-compare" aria-label={nameChanged ? "Filename will change" : "Name unchanged"}>
        <div className="name-track">
          <span className="ui-eyebrow text-ink-muted">Original filename</span>
          <code className="filename-value mt-1 block text-ink-soft" title={file.name}>{file.name}</code>
        </div>
        <span className="filename-arrow" aria-hidden="true"><ArrowRight size={15} /></span>
        <div className="name-track">
          <span className="ui-eyebrow text-ink-muted">{nameChanged ? "Proposed filename" : "Filename change"}</span>
          {nameChanged
            ? <code className="filename-value mt-1 block text-accent-text" title={current.destination_name}>{current.destination_name}</code>
            : <span className="name-unchanged mt-1.5"><Check size={13} /> Name unchanged</span>}
        </div>
      </div>

      {nameChanged && file.rename_reason && <p className="mt-2 text-xs leading-relaxed text-accent-text">{file.rename_reason}</p>}

      <div className="mt-3 grid gap-2 md:grid-cols-2">
        <label className="text-[11px] text-ink-muted">Destination folder
          <input
            value={current.target_path}
            onChange={(event) => onChange({ ...current, target_path: event.target.value })}
            disabled={!current.accepted}
            className="ui-field mt-1 w-full px-2.5 py-1.5 text-xs"
            data-invalid={destinationInvalid}
            aria-invalid={destinationInvalid}
            aria-describedby={destinationInvalid ? `${validationId}-destination` : undefined}
          />
          {destinationInvalid && <span id={`${validationId}-destination`} className="field-error">Destination cannot be empty.</span>}
        </label>
        <label className="text-[11px] text-ink-muted">Proposed filename
          <input
            value={current.destination_name}
            onChange={(event) => onChange({ ...current, destination_name: event.target.value })}
            disabled={!current.accepted}
            className="ui-field mt-1 w-full px-2.5 py-1.5 text-xs"
            data-invalid={filenameInvalid}
            aria-invalid={filenameInvalid}
            aria-describedby={filenameInvalid ? `${validationId}-filename` : undefined}
          />
          {filenameInvalid && <span id={`${validationId}-filename`} className="field-error">Filename cannot be empty.</span>}
        </label>
      </div>
      <p className="edit-guidance">Windows-invalid filename characters may be normalized by the backend. Collisions are resolved locally without overwriting files.</p>

      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-ink-muted">
        {file.sensitive && <span className="font-medium text-warning">Possible sensitive content · cloud disabled by default</span>}
        <span>{file.detected_type !== "unknown" ? `Detected: ${file.detected_type}` : "Type signature unavailable"}</span>
        {file.filename_conflict !== "none" && <span className="text-warning">Filename conflict: {file.filename_conflict.replace("_", " ")}</span>}
      </div>
      {file.warnings.map((warning) => <p key={warning} className="mt-1 text-xs text-warning">{warning}</p>)}

      <button
        type="button"
        className="file-details-trigger mt-3"
        aria-expanded={detailsOpen}
        aria-controls={detailsId}
        onClick={() => setDetailsOpen((open) => !open)}
      >
        <ChevronDown size={14} className="file-details-chevron" data-open={detailsOpen} />
        <span>{detailsOpen ? "Hide evidence and reasoning" : "View evidence and reasoning"}</span>
        {file.evidence_sources.length > 0 && <span className="file-details-count">{file.evidence_sources.length} {file.evidence_sources.length === 1 ? "source" : "sources"}</span>}
      </button>

      <AnimatePresence initial={false}>
        {detailsOpen && (
          <motion.div
            id={detailsId}
            initial={reducedMotion ? false : { height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={reducedMotion ? undefined : { height: 0, opacity: 0 }}
            transition={{ duration: reducedMotion ? 0 : 0.2, ease: "easeOut" }}
            className="overflow-hidden"
          >
            <div className="file-details-panel">
              <p className="text-xs leading-relaxed text-ink-soft">{file.reasoning}</p>
              {file.evidence_excerpt && <p className="mt-2 text-xs italic leading-relaxed text-ink-faint">“{file.evidence_excerpt}”</p>}
              <div className="mt-3 grid gap-2 text-[11px] text-ink-muted">
                <span className="break-all">Current location: {file.current_path}</span>
                <span>Detected type: {file.detected_type}</span>
                <span>Extraction: {file.extraction_status}</span>
                {file.evidence_sources.length > 0 && <span>Evidence sources: {file.evidence_sources.join(", ")}</span>}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </article>
  );
}

function Notice({ tone, text }: { tone: "success" | "error"; text: string }) {
  const success = tone === "success";
  const reducedMotion = useReducedMotion();
  return <motion.div
    initial={reducedMotion ? false : { opacity: 0, y: 5 }}
    animate={{ opacity: 1, y: 0 }}
    exit={reducedMotion ? undefined : { opacity: 0 }}
    transition={{ duration: reducedMotion ? 0 : 0.18, ease: "easeOut" }}
    className={`mt-4 flex items-start gap-2 rounded-lg border p-3 text-sm ${success ? "border-success/25 bg-success-soft text-success" : "border-danger/25 bg-danger-soft text-danger"}`}
  >
    {success ? <Check size={16} className="mt-0.5" /> : <X size={16} className="mt-0.5" />}<span>{text}</span>
  </motion.div>;
}

function HierarchyTree({ node }: { node: HierarchyNode }) {
  return <div className="rounded-lg border border-line bg-surface-inset p-3 text-sm">
    <div className="flex items-center gap-2 font-medium text-ink"><FolderOpen size={16} className="text-accent-text" />{node.name}</div>
    <div className="ml-3 mt-2 border-l border-line pl-3">
      {node.folders.map((folder) => <details key={folder.name} open className="group py-1">
        <summary className="flex cursor-pointer list-none items-center gap-2 text-ink-soft"><ChevronDown size={14} className="transition group-open:rotate-0 -rotate-90" /><Folder size={15} className="text-accent-text" />{folder.name}</summary>
        <div className="ml-3 mt-1 border-l border-line pl-3"><HierarchyTreeContents node={folder} /></div>
      </details>)}
      {node.files.map((file) => <div key={file} className="flex items-center gap-2 py-1 text-ink-soft"><FileText size={14} className="text-ink-faint" />{file}</div>)}
    </div>
  </div>;
}

function HierarchyTreeContents({ node }: { node: HierarchyNode }) {
  return <>
    {node.folders.map((folder) => <details key={folder.name} open className="group py-1">
      <summary className="flex cursor-pointer list-none items-center gap-2 text-ink-soft"><ChevronDown size={14} className="transition group-open:rotate-0 -rotate-90" /><Folder size={15} className="text-accent-text" />{folder.name}</summary>
      <div className="ml-3 mt-1 border-l border-line pl-3"><HierarchyTreeContents node={folder} /></div>
    </details>)}
    {node.files.map((file) => <div key={file} className="flex items-center gap-2 py-1 text-ink-soft"><FileText size={14} className="text-ink-faint" />{file}</div>)}
  </>;
}
