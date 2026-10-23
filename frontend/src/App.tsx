import * as Dialog from "@radix-ui/react-dialog";
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
  ShieldCheck,
  Sparkles,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";
import HistoryPanel from "./components/HistoryPanel";
import { Button } from "./components/ui/button";

const API = import.meta.env.VITE_SORTLY_API ?? "http://127.0.0.1:8765";

type PlanItem = {
  id: string;
  name: string;
  destination_name: string;
  target_path: string;
  reasoning: string;
  renamed: boolean;
  selected: boolean;
};

type Preview = { plan_id: string; source: string; files: PlanItem[]; analysis_seconds: number; already_organized: number };
type HierarchyNode = { name: string; files: string[]; folders: HierarchyNode[] };
type Completion = { moved: number; kept_in_root: number; hierarchy: HierarchyNode };

async function request<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${API}${path}`, {
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
  const [rename, setRename] = useState(false);
  const [reorganize, setReorganize] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [selectedTargets, setSelectedTargets] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState<"preview" | "apply" | "undo" | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [undoAvailable, setUndoAvailable] = useState(false);
  const [completion, setCompletion] = useState<Completion | null>(null);

  const groups = useMemo(() => {
    const grouped = new Map<string, PlanItem[]>();
    for (const file of preview?.files ?? []) {
      grouped.set(file.target_path, [...(grouped.get(file.target_path) ?? []), file]);
    }
    return [...grouped.entries()];
  }, [preview]);
  const selectedCount = groups
    .filter(([target]) => selectedTargets[target])
    .reduce((total, [, files]) => total + files.length, 0);

  function resetPreview() {
    setPreview(null);
    setSelectedTargets({});
  }

  async function chooseFolder() {
    setError("");
    try {
      const result = await request<{ source: string }>("/api/choose-folder", {});
      setSource(result.source);
      resetPreview();
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "Folder picker unavailable.";
      if (message !== "No folder was selected.") setError(message);
    }
  }

  async function createPreview() {
    if (!source.trim()) return;
    setBusy("preview");
    setError("");
    setMessage("");
    resetPreview();
    try {
      const result = await request<Preview>("/api/preview", { source, recursive, rename, reorganize });
      setPreview(result);
      setSource(result.source);
      setSelectedTargets(Object.fromEntries(result.files.map((file) => [file.target_path, file.selected])));
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
    if (!preview) return;
    setConfirmOpen(false);
    setBusy("apply");
    setError("");
    try {
      const selected_targets = Object.entries(selectedTargets)
        .filter(([, selected]) => selected)
        .map(([target]) => target);
      const result = await request<Completion>("/api/apply", {
        plan_id: preview.plan_id,
        selected_targets,
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
    if (!source.trim()) return;
    setBusy("undo");
    setError("");
    try {
      const result = await request<{ restored: number }>("/api/undo", { source });
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
    setSelectedTargets((current) => ({ ...current, [target]: !current[target] }));
  }

  async function openFolder() {
    if (!source.trim()) return;
    setError("");
    try {
      await request("/api/open-folder", { source });
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
        <Button variant="ghost" className="mt-2 justify-start" onClick={undo} disabled={!undoAvailable || busy !== null}>
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
            <span className="size-1.5 rounded-full bg-emerald-400" /> Local API
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
                <Button variant="secondary" size="sm" onClick={chooseFolder} disabled={busy !== null}><Folder size={14} /> Browse</Button>
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
                <div className="flex items-center gap-4">
                  <span className="hidden items-center gap-2 text-xs text-zinc-500 sm:flex"><ShieldCheck size={15} className="text-emerald-400" /> Preview only · No files moved</span>
                  <Button onClick={createPreview} disabled={busy !== null || !source.trim()}>
                    {busy === "preview" ? <LoaderCircle size={16} className="animate-spin" /> : <Sparkles size={16} />}
                    Analyze folder <ArrowRight size={15} />
                  </Button>
                </div>
              </div>
            </section>

            <AnimatePresence>
              {message && <Notice tone="success" text={message} />}
              {error && <Notice tone="error" text={error} />}
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
                    <div><h2 className="font-medium">Proposed organization</h2><p className="mt-1 text-xs text-zinc-500">Turn off a target to keep every file in that group at the root.</p></div>
                    <span className="rounded-full border border-white/10 px-2.5 py-1 text-xs text-zinc-400">{selectedCount} / {preview.files.length} selected</span>
                  </div>
                  <div className="divide-y divide-white/[0.05]">
                    {groups.map(([target, files]) => {
                      const selected = selectedTargets[target];
                      return <div key={target} className="p-5">
                        <div className="flex items-center justify-between gap-4">
                          <div className="flex min-w-0 items-center gap-3"><Folder size={17} className="shrink-0 text-violet-300" /><div className="min-w-0"><p className="truncate text-sm font-medium">{target}</p><p className="mt-0.5 text-xs text-zinc-500">{files.length} {files.length === 1 ? "file" : "files"}</p></div></div>
                          <button role="switch" aria-checked={selected} onClick={() => toggleTarget(target)} className={`relative h-6 w-11 rounded-full transition ${selected ? "bg-violet-500" : "bg-zinc-700"}`}>
                            <span className={`absolute top-1 size-4 rounded-full bg-white shadow transition ${selected ? "left-6" : "left-1"}`} />
                            <span className="sr-only">Toggle {target}</span>
                          </button>
                        </div>
                        <div className="mt-4 space-y-2 border-l border-white/[0.08] pl-4">
                          {files.map((file) => <div key={file.id} className="flex items-start gap-2 text-sm"><FileText size={14} className="mt-0.5 shrink-0 text-zinc-500" /><div className="min-w-0"><p className="truncate text-zinc-300">{file.renamed ? <><span className="text-zinc-600 line-through">{file.name}</span><span className="mx-1 text-violet-300">→</span>{file.destination_name}</> : file.name}</p><p className="mt-0.5 text-xs text-zinc-600">{file.reasoning}</p></div></div>)}
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
              <HistoryPanel folder={source || undefined} apiBase={API} onChanged={() => { setUndoAvailable(false); setCompletion(null); }} />
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
            <div className="mt-6 flex gap-3">
              <Dialog.Close asChild><Button variant="secondary" className="flex-1">Cancel</Button></Dialog.Close>
              <Button className="flex-1" onClick={applyPlan} disabled={busy !== null}>{busy === "apply" && <LoaderCircle size={16} className="animate-spin" />} Confirm moves</Button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
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
