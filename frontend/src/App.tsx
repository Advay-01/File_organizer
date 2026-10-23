import * as Dialog from "@radix-ui/react-dialog";
import { invoke, isTauri } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";
import { AnimatePresence, motion } from "motion/react";
import {
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
  LoaderCircle,
  LockKeyhole,
  RotateCcw,
  Settings2,
  ShieldCheck,
  Sparkles,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import HistoryPanel from "./components/HistoryPanel";
import ProviderStudio from "./components/ProviderStudio";
import { Button } from "./components/ui/button";

const BROWSER_API = import.meta.env.VITE_SORTLY_API ?? "http://127.0.0.1:8765";
const DESKTOP = isTauri();

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
type Completion = { moved: number; kept_in_root: number; hierarchy: HierarchyNode };
type FileEdit = { accepted: boolean; target_path: string; destination_name: string };
type ApiRuntime = {
  state: "starting" | "ready" | "error";
  base_url?: string | null;
  detail?: string | null;
};

async function request<T>(apiBase: string, path: string, body?: unknown): Promise<T> {
  if (!apiBase) throw new Error("The local Sortly service is not ready yet.");
  const response = await fetch(`${apiBase}${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = (await response.json()) as T & { detail?: string };
  if (!response.ok) throw new Error(data.detail ?? "The local organizer could not complete that request.");
  return data;
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
  const [providerStudioOpen, setProviderStudioOpen] = useState(false);
  const [riskAcknowledged, setRiskAcknowledged] = useState(false);
  const [apiRuntime, setApiRuntime] = useState<ApiRuntime>(() => ({
    state: DESKTOP ? "starting" : "ready",
    base_url: DESKTOP ? null : BROWSER_API,
  }));
  const apiBase = apiRuntime.base_url ?? "";
  const apiReady = apiRuntime.state === "ready" && Boolean(apiBase);

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
    if (!preview || !apiReady || (preview.requires_confirmation && !riskAcknowledged)) return;
    setConfirmOpen(false);
    setBusy("apply");
    setError("");
    try {
      const result = await request<Completion>(apiBase, "/api/apply", {
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
      setCompletion(result);
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
    <div className="min-h-screen bg-[#0e0e12] text-zinc-100">
      <aside className="fixed inset-y-0 left-0 flex w-56 flex-col border-r border-white/[0.07] bg-[#141419] p-4">
        <div className="mb-10 flex items-center gap-3 px-2">
          <div className="grid size-9 place-items-center rounded-xl bg-violet-500 shadow-lg shadow-violet-500/25">
            <FolderOpen size={19} />
          </div>
          <div>
            <div className="font-semibold tracking-tight">sortly</div>
            <div className="text-[10px] tracking-[0.18em] text-zinc-500">AI ORGANIZER</div>
          </div>
        </div>

        <p className="mb-2 px-3 text-[10px] font-semibold tracking-[0.18em] text-zinc-600">WORKSPACE</p>
        <div className="flex items-center gap-3 rounded-xl border border-violet-400/15 bg-violet-400/[0.09] px-3 py-3 text-sm text-violet-200">
          <FolderSearch size={17} /> Organize files
        </div>
        <Button variant="ghost" className="mt-2 justify-start" onClick={() => setProviderStudioOpen(true)} disabled={!apiReady}>
          <Settings2 size={17} /> Provider Studio
        </Button>
        <Button variant="ghost" className="mt-2 justify-start" onClick={undo} disabled={!undoAvailable || busy !== null || !apiReady}>
          <RotateCcw size={17} /> Undo last move
        </Button>

        <div className="mt-auto rounded-xl border border-white/[0.07] bg-white/[0.025] p-3">
          <div className="flex items-center gap-2 text-xs text-zinc-300">
            <span className="size-2 rounded-full bg-emerald-400" /> Local workspace
          </div>
          <p className="mt-2 text-[11px] leading-relaxed text-zinc-500">Your files stay on this computer. Nothing moves without confirmation.</p>
        </div>
      </aside>

      <main className="ml-56 min-h-screen">
        <header className="flex h-16 items-center justify-between border-b border-white/[0.07] px-8">
          <div className="flex items-center gap-2 text-xs text-zinc-500">
            Workspace <ChevronRight size={13} /> <span className="text-zinc-200">Organize</span>
          </div>
          <div className="flex items-center gap-2 rounded-full border border-white/[0.08] px-3 py-1.5 text-xs text-zinc-400">
            <span className={`size-1.5 rounded-full ${apiReady ? "bg-emerald-400" : apiRuntime.state === "error" ? "bg-red-400" : "bg-amber-400"}`} />
            {apiReady ? "Local API ready" : apiRuntime.state === "error" ? "Local API unavailable" : "Starting local API"}
          </div>
        </header>

        <div className="mx-auto max-w-5xl px-8 py-10">
          <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.28 }}>
            <div className="mb-3 flex items-center gap-2 text-xs font-medium text-violet-300"><Sparkles size={14} /> YOUR AI WORKSPACE</div>
            <h1 className="text-3xl font-semibold tracking-tight">A place for everything.</h1>
            <p className="mt-2 text-sm text-zinc-500">Pick a folder. Preview the plan. Let AI do the sorting.</p>

            <section className="mt-8 rounded-2xl border border-white/[0.08] bg-[#18181f] p-5 shadow-2xl shadow-black/10">
              <label className="mb-3 block text-xs font-medium tracking-wide text-zinc-400">SOURCE FOLDER</label>
              <div className="flex items-center gap-3 rounded-xl border border-white/[0.09] bg-[#0e0e12] p-2.5 pl-3 focus-within:border-violet-400/50">
                <HardDrive size={17} className="shrink-0 text-zinc-500" />
                <input
                  value={source}
                  onChange={(event) => { setSource(event.target.value); resetPreview(); }}
                  className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-zinc-600"
                  placeholder={String.raw`C:\Users\you\Documents`}
                />
                <Button variant="secondary" size="sm" onClick={chooseFolder} disabled={busy !== null || !apiReady}><Folder size={14} /> Browse</Button>
              </div>
              <div className="mt-4 flex items-center justify-between gap-4">
                <label className="flex cursor-pointer items-center gap-2 text-xs text-zinc-500">
                  <input type="checkbox" checked={recursive} onChange={(event) => { setRecursive(event.target.checked); resetPreview(); }} className="size-3.5 accent-violet-500" />
                  Include subfolders
                </label>
                <label className="flex cursor-pointer items-center gap-2 text-xs text-zinc-500">
                  <input type="checkbox" checked={rename} onChange={(event) => { setRename(event.target.checked); resetPreview(); }} className="size-3.5 accent-violet-500" />
                  Suggest cleaner file names
                </label>
                <label className="flex cursor-pointer items-center gap-2 text-xs text-zinc-500">
                  <input type="checkbox" checked={reorganize} onChange={(event) => { setReorganize(event.target.checked); resetPreview(); }} className="size-3.5 accent-violet-500" />
                  Reorganize already sorted files
                </label>
                <label className="flex cursor-pointer items-center gap-2 text-xs text-zinc-500">
                  <input type="checkbox" checked={allowSensitiveCloud} onChange={(event) => { setAllowSensitiveCloud(event.target.checked); resetPreview(); }} className="size-3.5 accent-amber-500" />
                  Allow redacted cloud analysis for suspected sensitive files
                </label>
                <div className="flex items-center gap-4">
                  <span className="hidden items-center gap-2 text-xs text-zinc-500 sm:flex"><ShieldCheck size={15} className="text-emerald-400" /> Preview only · No files moved</span>
                  <Button onClick={createPreview} disabled={busy !== null || !source.trim() || !apiReady}>
                    {busy === "preview" ? <LoaderCircle size={16} className="animate-spin" /> : <Sparkles size={16} />}
                    Analyze folder <ArrowRight size={15} />
                  </Button>
                </div>
              </div>
            </section>

            <AnimatePresence>
              {message && <Notice tone="success" text={message} />}
              {error && <Notice tone="error" text={error} />}
              {apiRuntime.state === "error" && <Notice tone="error" text={`Local API unavailable. ${apiRuntime.detail ?? ""}`.trim()} />}
              {preview && preview.scan_summary.skipped > 0 && (
                <Notice tone="error" text={`${preview.scan_summary.skipped} files were skipped by scan safety limits${preview.scan_summary.truncated ? " and the file-count limit was reached" : ""}.`} />
              )}
              {preview?.plan_warnings.map((warning) => <Notice key={warning} tone="error" text={warning} />)}
            </AnimatePresence>

            <AnimatePresence>
              {completion && (
                <motion.section initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="mt-6 overflow-hidden rounded-2xl border border-emerald-400/15 bg-[#18181f]">
                  <div className="border-b border-white/[0.07] p-6">
                    <div className="flex items-center gap-2 text-sm font-medium text-emerald-300"><Check size={17} /> Organization complete</div>
                    <h2 className="mt-3 text-2xl font-semibold">{completion.moved} files organized successfully</h2>
                    {completion.kept_in_root > 0 && <p className="mt-2 text-sm text-zinc-500">{completion.kept_in_root} files stayed in the source folder.</p>}
                  </div>
                  <div className="p-6">
                    <div className="mb-3 flex items-center gap-2 text-xs font-medium tracking-wide text-zinc-400"><FolderTree size={15} className="text-violet-300" /> NEW FOLDER HIERARCHY</div>
                    <HierarchyTree node={completion.hierarchy} />
                  </div>
                  <div className="flex justify-end gap-3 border-t border-white/[0.07] p-5">
                    <Button variant="secondary" onClick={undo} disabled={busy !== null}><RotateCcw size={15} /> Undo</Button>
                    <Button onClick={openFolder} disabled={busy !== null}><ExternalLink size={15} /> Open folder</Button>
                  </div>
                </motion.section>
              )}
            </AnimatePresence>

            <AnimatePresence>
              {preview && preview.files.length > 0 && (
                <motion.section initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="mt-6 overflow-hidden rounded-2xl border border-white/[0.08] bg-[#18181f]">
                  <div className="flex items-center justify-between border-b border-white/[0.07] p-5">
                    <div><h2 className="font-medium">Proposed organization</h2><p className="mt-1 text-xs text-zinc-500">Review the evidence, then accept, edit, or reject each file. Group switches apply to every file in that target.</p></div>
                    <span className="rounded-full border border-white/10 px-2.5 py-1 text-xs text-zinc-400">{selectedCount} / {preview.files.length} selected</span>
                  </div>
                  <div className="divide-y divide-white/[0.05]">
                    {groups.map(([target, files]) => {
                      const selected = files.every((file) => fileEdits[file.id]?.accepted);
                      return <div key={target} className="p-5">
                        <div className="flex items-center justify-between gap-4">
                          <div className="flex min-w-0 items-center gap-3"><Folder size={17} className="shrink-0 text-violet-300" /><div className="min-w-0"><p className="truncate text-sm font-medium">{target}</p><p className="mt-0.5 text-xs text-zinc-500">{files.length} {files.length === 1 ? "file" : "files"}</p></div></div>
                          <button role="switch" aria-checked={selected} onClick={() => toggleTarget(target)} className={`relative h-6 w-11 rounded-full transition ${selected ? "bg-violet-500" : "bg-zinc-700"}`}>
                            <span className={`absolute top-1 size-4 rounded-full bg-white shadow transition ${selected ? "left-6" : "left-1"}`} />
                            <span className="sr-only">Toggle {target}</span>
                          </button>
                        </div>
                        <div className="mt-4 space-y-3 border-l border-white/[0.08] pl-4">
                          {files.map((file) => (
                            <FilePreviewRow
                              key={file.id}
                              file={file}
                              edit={fileEdits[file.id]}
                              onChange={(edit) => setFileEdits((current) => ({ ...current, [file.id]: edit }))}
                            />
                          ))}
                        </div>
                      </div>;
                    })}
                  </div>
                  <div className="flex items-center justify-between border-t border-white/[0.07] p-5">
                    <div className="flex items-center gap-2 text-xs text-zinc-500"><LockKeyhole size={14} /> The shown preview is the exact plan that will run.</div>
                    <Button onClick={() => setConfirmOpen(true)} disabled={busy !== null || selectedCount === 0}>Review & organize</Button>
                  </div>
                </motion.section>
              )}
            </AnimatePresence>

            <section className="mt-6 rounded-2xl border border-white/[0.08] bg-[#18181f] p-5">
              {apiReady
                ? <HistoryPanel folder={source || undefined} apiBase={apiBase} onChanged={() => { setUndoAvailable(false); setCompletion(null); }} />
                : <p className="text-sm text-zinc-500">{apiRuntime.state === "error" ? "History is unavailable until the local service restarts." : "Starting the local file service…"}</p>}
            </section>
          </motion.div>
        </div>
      </main>

      <Dialog.Root open={confirmOpen} onOpenChange={setConfirmOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-40 bg-black/70 backdrop-blur-sm" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-[min(92vw,30rem)] -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-white/10 bg-[#1a1a22] p-6 shadow-2xl focus:outline-none">
            <div className="grid size-11 place-items-center rounded-xl bg-violet-500/15 text-violet-300"><FolderOpen size={21} /></div>
            <Dialog.Title className="mt-4 text-lg font-semibold">Organize these files?</Dialog.Title>
            <Dialog.Description className="mt-2 text-sm leading-relaxed text-zinc-400">Sortly will move {selectedCount} selected files using the exact preview above. Unselected target groups stay in the source folder.</Dialog.Description>
            {preview?.requires_confirmation && (
              <label className="mt-4 flex items-start gap-2 rounded-xl border border-amber-400/20 bg-amber-400/[0.06] p-3 text-xs text-amber-200">
                <input type="checkbox" checked={riskAcknowledged} onChange={(event) => setRiskAcknowledged(event.target.checked)} className="mt-0.5 accent-amber-400" />
                <span>I reviewed the unusual plan warnings and want to continue. The plan has not been rewritten automatically.</span>
              </label>
            )}
            <div className="mt-6 flex gap-3">
              <Dialog.Close asChild><Button variant="secondary" className="flex-1">Cancel</Button></Dialog.Close>
              <Button className="flex-1" onClick={applyPlan} disabled={busy !== null || !apiReady || Boolean(preview?.requires_confirmation && !riskAcknowledged)}>{busy === "apply" && <LoaderCircle size={16} className="animate-spin" />} Confirm moves</Button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
      {apiReady && (
        <ProviderStudio
          open={providerStudioOpen}
          onOpenChange={setProviderStudioOpen}
          apiBase={apiBase}
        />
      )}
    </div>
  );
}

function FilePreviewRow({ file, edit, onChange }: {
  file: PlanItem;
  edit: FileEdit | undefined;
  onChange: (edit: FileEdit) => void;
}) {
  const current = edit ?? {
    accepted: file.selected,
    target_path: file.target_path,
    destination_name: file.destination_name,
  };
  const confidenceClass = file.confidence === "high"
    ? "border-emerald-400/20 text-emerald-300"
    : file.confidence === "medium"
      ? "border-amber-400/20 text-amber-300"
      : "border-zinc-500/20 text-zinc-400";
  return (
    <div data-file-id={file.id} className={`rounded-xl border p-3 ${current.accepted ? "border-white/[0.08] bg-black/20" : "border-white/[0.04] bg-black/10 opacity-60"}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <label className="flex min-w-0 items-start gap-2 text-sm">
          <input
            type="checkbox"
            checked={current.accepted}
            onChange={(event) => onChange({ ...current, accepted: event.target.checked })}
            className="mt-0.5 accent-violet-500"
          />
          <span className="min-w-0">
            <span className="block truncate text-zinc-200">{file.name}</span>
            <span className="mt-0.5 block text-xs text-zinc-600">{file.evidence_sufficient ? "Move and rename may be applied together" : "Evidence is limited; keeping the original name is safer"}</span>
          </span>
        </label>
        <span className={`rounded-full border px-2 py-0.5 text-[11px] ${confidenceClass}`}>{file.confidence} confidence</span>
      </div>

      <div className="mt-3 grid gap-2 md:grid-cols-2">
        <label className="text-[11px] text-zinc-500">Destination folder
          <input
            value={current.target_path}
            onChange={(event) => onChange({ ...current, target_path: event.target.value })}
            disabled={!current.accepted}
            className="mt-1 w-full rounded-lg border border-white/10 bg-[#0e0e12] px-2.5 py-1.5 text-xs text-zinc-200 outline-none focus:border-violet-400/50 disabled:opacity-50"
          />
        </label>
        <label className="text-[11px] text-zinc-500">Filename
          <input
            value={current.destination_name}
            onChange={(event) => onChange({ ...current, destination_name: event.target.value })}
            disabled={!current.accepted}
            className="mt-1 w-full rounded-lg border border-white/10 bg-[#0e0e12] px-2.5 py-1.5 text-xs text-zinc-200 outline-none focus:border-violet-400/50 disabled:opacity-50"
          />
        </label>
      </div>

      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-zinc-500">
        <span className="break-all">Current: {file.current_path}</span>
        {file.sensitive && <span className="font-medium text-amber-300">Possible sensitive content · cloud disabled by default</span>}
        <span>{file.detected_type !== "unknown" ? `Detected: ${file.detected_type}` : "Type signature unavailable"}</span>
        {file.evidence_sources.length > 0 && <span>Evidence: {file.evidence_sources.join(", ")}</span>}
        {file.filename_conflict !== "none" && <span className="text-amber-300">Filename conflict: {file.filename_conflict.replace("_", " ")}</span>}
      </div>
      <p className="mt-2 text-xs text-zinc-400">{file.reasoning}</p>
      {file.evidence_excerpt && <p className="mt-1 line-clamp-3 text-xs italic text-zinc-600">“{file.evidence_excerpt}”</p>}
      {file.rename_reason && <p className="mt-1 text-xs text-violet-300">{file.rename_reason}</p>}
      {file.warnings.map((warning) => <p key={warning} className="mt-1 text-xs text-amber-300">{warning}</p>)}
    </div>
  );
}

function Notice({ tone, text }: { tone: "success" | "error"; text: string }) {
  const success = tone === "success";
  return <motion.div initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className={`mt-4 flex items-start gap-2 rounded-xl border p-3 text-sm ${success ? "border-emerald-400/15 bg-emerald-400/[0.06] text-emerald-300" : "border-red-400/20 bg-red-400/[0.06] text-red-300"}`}>
    {success ? <Check size={16} className="mt-0.5" /> : <X size={16} className="mt-0.5" />}<span>{text}</span>
  </motion.div>;
}

function HierarchyTree({ node }: { node: HierarchyNode }) {
  return <div className="rounded-xl border border-white/[0.07] bg-[#101014] p-3 text-sm">
    <div className="flex items-center gap-2 font-medium text-zinc-200"><FolderOpen size={16} className="text-violet-300" />{node.name}</div>
    <div className="ml-3 mt-2 border-l border-white/[0.08] pl-3">
      {node.folders.map((folder) => <details key={folder.name} open className="group py-1">
        <summary className="flex cursor-pointer list-none items-center gap-2 text-zinc-300"><ChevronDown size={14} className="transition group-open:rotate-0 -rotate-90" /><Folder size={15} className="text-violet-300" />{folder.name}</summary>
        <div className="ml-3 mt-1 border-l border-white/[0.06] pl-3"><HierarchyTreeContents node={folder} /></div>
      </details>)}
      {node.files.map((file) => <div key={file} className="flex items-center gap-2 py-1 text-zinc-400"><FileText size={14} className="text-zinc-600" />{file}</div>)}
    </div>
  </div>;
}

function HierarchyTreeContents({ node }: { node: HierarchyNode }) {
  return <>
    {node.folders.map((folder) => <details key={folder.name} open className="group py-1">
      <summary className="flex cursor-pointer list-none items-center gap-2 text-zinc-300"><ChevronDown size={14} className="transition group-open:rotate-0 -rotate-90" /><Folder size={15} className="text-violet-300" />{folder.name}</summary>
      <div className="ml-3 mt-1 border-l border-white/[0.06] pl-3"><HierarchyTreeContents node={folder} /></div>
    </details>)}
    {node.files.map((file) => <div key={file} className="flex items-center gap-2 py-1 text-zinc-400"><FileText size={14} className="text-zinc-600" />{file}</div>)}
  </>;
}
