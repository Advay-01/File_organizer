import * as Dialog from "@radix-ui/react-dialog";
import { invoke, isTauri } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";
import {
  AlertTriangle,
  ArrowRight,
  Check,
  ChevronDown,
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
  RotateCcw,
  Search,
  ShieldCheck,
  Sparkles,
  Sun,
  X,
} from "lucide-react";
import { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import HistoryPanel from "./components/HistoryPanel";
import ProviderStudio from "./components/ProviderStudio";
import ChatPanel, { type ChatResult } from "./components/ChatPanel";
import AnalysisActivity from "./components/AnalysisActivity";
import { Button } from "./components/ui/button";
import sortlyMark from "./assets/sortly-mark.svg";
import { ApiError, fetchJson } from "./lib/api";
import {
  describeSkips,
  filterReviewGroups,
  getFileReviewStatus,
  getFilenameExtensionChange,
  getInitialGroupLimit,
  getReviewBatchMode,
  shouldExpandGroupsInitially,
  shouldShowReviewTools,
  skipReasonLabel,
  summarizeFileWarning,
  summarizeGroup,
  summarizeReview,
  type ReviewFileStatus,
  type ReviewFilter,
  type ReviewStats,
} from "./lib/review";
import { describeUndoResult, type UndoResult } from "./lib/undo";
import { applyInstructions, nextClarification, type ChatMessage } from "./lib/workspace";

const BROWSER_API = import.meta.env.VITE_SORTLY_API ?? "http://127.0.0.1:8765";
const DESKTOP = isTauri();
const THEME_STORAGE_KEY = "sortly.theme";

type WorkspaceView = "organize" | "preview" | "settings" | "providers" | "history";
type ThemePreference = "system" | "light" | "dark";
const VIEW_LABELS: Record<WorkspaceView, string> = { organize: "Organize", preview: "Preview", settings: "Settings", providers: "Providers", history: "History" };
function workspaceViewFromHash(): WorkspaceView {
  const route = window.location.hash.replace(/^#\/?/, "").split("/")[0];
  if (route === "providers") return "providers";
  if (route === "history") return "history";
  if (route === "settings") return "settings";
  return route === "preview" ? "preview" : "organize";
}

function folderIdentity(path: string): string {
  return path.trim().replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
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
type ApplyFailure = { file: string; reason: string };
type ApplyResult = {
  status: "success" | "partial" | "failed";
  moved: number;
  requested: number;
  failed: number;
  failures: ApplyFailure[];
  undo_available: boolean;
  kept_in_root: number;
  hierarchy: HierarchyNode;
  detail?: string;
};
type Completion = ApplyResult;
type FileEdit = { accepted: boolean; target_path: string; destination_name: string };
type ApiRuntime = {
  state: "starting" | "ready" | "error";
  base_url?: string | null;
  detail?: string | null;
};

async function request<T>(apiBase: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  if (!apiBase) throw new Error("The local Sortly service is not ready yet.");
  return fetchJson<T>(`${apiBase}${path}`, {
    signal,
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
  const [busy, setBusy] = useState<"preview" | "apply" | "undo" | "chat" | "browse" | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [undoAvailable, setUndoAvailable] = useState(false);
  const [completion, setCompletion] = useState<Completion | null>(null);
  const [riskAcknowledged, setRiskAcknowledged] = useState(false);
  const [activeView, setActiveView] = useState<WorkspaceView>(workspaceViewFromHash);
  const [themePreference, setThemePreference] = useState<ThemePreference>(INITIAL_THEME_PREFERENCE);
  const [reviewQuery, setReviewQuery] = useState("");
  const [reviewFilter, setReviewFilter] = useState<ReviewFilter>("all");
  const [expandedGroups, setExpandedGroups] = useState<Record<string, boolean>>({});
  const [visibleGroupLimit, setVisibleGroupLimit] = useState(Number.POSITIVE_INFINITY);
  const [chatDraft, setChatDraft] = useState("");
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [chatError, setChatError] = useState("");
  const [clarification, setClarification] = useState<string | null>(null);
  const [settingsVisited, setSettingsVisited] = useState(activeView === "settings");
  const [showProviders, setShowProviders] = useState(activeView === "providers");
  const [showHistory, setShowHistory] = useState(activeView === "history");
  const analysisController = useRef<AbortController | null>(null);
  const conversationFolder = useRef("");
  const reviewHeading = useRef<HTMLHeadingElement>(null);

  const [apiRuntime, setApiRuntime] = useState<ApiRuntime>(() => ({
    state: DESKTOP ? "starting" : "ready",
    base_url: DESKTOP ? null : BROWSER_API,
  }));
  const apiBase = apiRuntime.base_url ?? "";
  const apiReady = apiRuntime.state === "ready" && Boolean(apiBase);

  useEffect(() => {
    const syncRoute = () => {
      const next = workspaceViewFromHash();
      const view = next;
      if (view === "settings") setSettingsVisited(true);
      if (view === "providers") setShowProviders(true);
      if (view === "history") setShowHistory(true);
      setActiveView(view);
    };
    syncRoute();
    window.addEventListener("popstate", syncRoute);
    window.addEventListener("hashchange", syncRoute);
    return () => {
      window.removeEventListener("popstate", syncRoute);
      window.removeEventListener("hashchange", syncRoute);
      analysisController.current?.abort();
    };
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = themePreference;
    document.title = `${VIEW_LABELS[activeView]} · Sortly`;
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, themePreference);
    } catch {
      // A blocked storage API should not make the appearance controls unusable.
    }
  }, [activeView, themePreference]);

  useEffect(() => {
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }, [activeView]);

  useEffect(() => {
    if (activeView !== "preview" || !preview) return;
    const frame = window.requestAnimationFrame(() => {
      reviewHeading.current?.scrollIntoView({ block: "start", behavior: "auto" });
      reviewHeading.current?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeView, preview?.plan_id]);

  function navigateTo(view: WorkspaceView) {
    if (view === "settings") setSettingsVisited(true);
    if (view === "providers") setShowProviders(true);
    if (view === "history") setShowHistory(true);
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
  }, [preview]);
  const selectedCount = useMemo(
    () => Object.values(fileEdits).filter((edit) => edit.accepted).length,
    [fileEdits],
  );
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
  const extensionChangeCount = useMemo(() => (preview?.files ?? []).filter((file) =>
    (fileEdits[file.id]?.accepted ?? file.selected) && getFilenameExtensionChange(file.name, file.destination_name)
  ).length, [fileEdits, preview]);
  const reviewStats = useMemo(
    () => summarizeReview(preview?.files ?? [], fileEdits),
    [fileEdits, preview],
  );
  const filteredReviewGroups = useMemo(
    () => filterReviewGroups(groups, fileEdits, reviewQuery, reviewFilter),
    [fileEdits, groups, reviewFilter, reviewQuery],
  );
  const reviewBatchMode = getReviewBatchMode(preview?.files.length ?? 0);
  const visibleReviewGroups = filteredReviewGroups.slice(0, visibleGroupLimit);
  const hiddenGroupCount = Math.max(0, filteredReviewGroups.length - visibleReviewGroups.length);
  const showReviewTools = (
    shouldShowReviewTools(preview?.files.length ?? 0)
    || reviewFilter !== "all"
    || Boolean(reviewQuery)
  );

  useEffect(() => {
    if (!preview) {
      setReviewQuery("");
      setReviewFilter("all");
      setExpandedGroups({});
      setVisibleGroupLimit(Number.POSITIVE_INFINITY);
      return;
    }
    const initiallyOpen = shouldExpandGroupsInitially(preview.files.length);
    setExpandedGroups(Object.fromEntries(
      [...new Set(preview.files.map((file) => file.target_path))]
        .map((target) => [target, initiallyOpen]),
    ));
    setReviewQuery("");
    setReviewFilter("all");
    setVisibleGroupLimit(getInitialGroupLimit(preview.files.length));
  }, [preview?.plan_id]);

  useEffect(() => {
    setVisibleGroupLimit(getInitialGroupLimit(preview?.files.length ?? 0));
  }, [preview?.files.length, reviewFilter, reviewQuery]);

  const updateFileEdit = useCallback((fileId: string, edit: FileEdit) => {
    setFileEdits((current) => ({ ...current, [fileId]: edit }));
  }, []);

  function resetPreview() {
    setPreview(null);
    setClarification(null);
    setConfirmOpen(false);
    setFileEdits({});
    setRiskAcknowledged(false);
    setReviewQuery("");
    setReviewFilter("all");
    setExpandedGroups({});
    setVisibleGroupLimit(Number.POSITIVE_INFINITY);
  }

  function changeSource(next: string, commit = false) {
    if (folderIdentity(next) !== folderIdentity(source)) {
      if (conversationFolder.current && conversationFolder.current !== folderIdentity(next)) {
        setChatMessages([]);
        setChatDraft("");
        setChatError("");
        conversationFolder.current = folderIdentity(next);
      }
      setCompletion(null);
      setUndoAvailable(false);
      setMessage("");
      setError("");
    }
    if (commit) conversationFolder.current = folderIdentity(next);
    setSource(next);
    resetPreview();
  }

  async function chooseFolder() {
    if (busy) return;
    setBusy("browse");
    setError("");
    try {
      if (DESKTOP) {
        const selected = await open({
          directory: true,
          multiple: false,
          title: "Choose a folder to organize",
        });
        if (typeof selected !== "string") return;
        changeSource(selected, true);
      } else {
        const result = await request<{ source: string }>(apiBase, "/api/choose-folder", {});
        changeSource(result.source, true);
      }
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "Folder picker unavailable.";
      if (message !== "No folder was selected.") setError(message);
    } finally { setBusy(null); }
  }

  function publishPreview(next: Preview) {
    conversationFolder.current = folderIdentity(next.source);
    setPreview(next);
    setSource(next.source);
    setFileEdits(Object.fromEntries(next.files.map((file) => [file.id, {
      accepted: file.selected, target_path: file.target_path, destination_name: file.destination_name,
    }])));
    setRiskAcknowledged(false);
    setConfirmOpen(false);
  }

  function recordChatReply(user: ChatMessage, result: ChatResult) {
    setChatMessages((current) => [...current.map((item) => item.id === user.id
      ? { ...item, status: "sent" as const, instruction: result.rules.length > 0 || Boolean(result.needs_clarification) }
      : item), {
      id: crypto.randomUUID(), role: "assistant", rules: result.rules,
      text: [...new Set([result.reply, result.needs_clarification].filter(Boolean))].join(" "),
    }]);
  }

  function cancelAnalysis() {
    analysisController.current?.abort();
    analysisController.current = null;
    setBusy(null);
    setChatMessages((current) => current.map((item) => item.status === "sending" ? { ...item, status: "queued" } : item));
    setMessage("Analysis cancelled.");
  }

  async function createPreview() {
    if (!source.trim() || !apiReady || busy || analysisController.current) return;
    conversationFolder.current = folderIdentity(source);
    const controller = new AbortController();
    analysisController.current = controller;
    let messages = chatMessages;
    if (chatDraft.trim()) {
      const user: ChatMessage = { id: crypto.randomUUID(), role: "user", text: chatDraft.trim(), status: "queued", instruction: true };
      messages = [...messages, user];
      setChatMessages(messages);
      setChatDraft("");
    }
    setBusy("preview"); setError(""); setMessage(""); setChatError("");
    resetPreview();
    setCompletion(null);
    try {
      const original = await request<Preview>(apiBase, "/api/preview", {
        source, recursive, rename, reorganize, allow_sensitive_cloud: allowSensitiveCloud,
      }, controller.signal);
      if (controller.signal.aborted) return;
      let result = original;
      let question: string | null = null;
      if (original.files.length > 0) {
        setChatMessages((current) => current.map((item) => item.role === "user" && item.instruction !== false
          ? { ...item, status: "sending" } : item));
        const refined = await applyInstructions(original, messages,
          (planId, text) => request<{ result: ChatResult; preview: Preview | null }>(apiBase, "/api/chat", {
            plan_id: planId, message: text,
          }, controller.signal), recordChatReply, controller.signal);
        result = refined.preview;
        question = refined.clarification;
      }
      if (controller.signal.aborted) return;
      publishPreview(result);
      setClarification(question);
      navigateTo("preview");
      setMessage(result.files.length ? "Preview ready. Review the plan before confirming." : "No eligible files found.");
    } catch (caught) {
      if (!controller.signal.aborted) {
        setError(caught instanceof Error ? caught.message : "Could not preview this folder.");
        setChatMessages((current) => current.map((item) => item.status === "sending" ? { ...item, status: "queued" } : item));
      }
    } finally {
      if (analysisController.current === controller) { analysisController.current = null; setBusy(null); }
    }
  }

  async function sendChat() {
    const text = chatDraft.trim();
    if (!text || busy || analysisController.current) return;
    if (source.trim()) conversationFolder.current = folderIdentity(source);
    const user: ChatMessage = { id: crypto.randomUUID(), role: "user", text, status: preview ? "sending" : "queued", instruction: true };
    setChatError("");
    setChatMessages((current) => [...current, user]);
    if (!preview) { setChatDraft(""); return; }
    setBusy("chat");
    try {
      const response = await request<{ result: ChatResult; preview: Preview | null }>(apiBase, "/api/chat", {
        plan_id: preview.plan_id, message: text,
        files: Object.entries(fileEdits).map(([id, edit]) => ({ id, ...edit })),
      });
      recordChatReply(user, response.result);
      setChatDraft("");
      setClarification((current) => nextClarification(current, response.result));
      if (response.preview) {
        publishPreview(response.preview);
        navigateTo("preview");
        setMessage("Preview updated. Review the changes before confirming.");
      }
    } catch (caught) {
      setChatError(caught instanceof Error ? caught.message : "Chat is unavailable. Try again.");
      setChatMessages((current) => current.map((item) => item.id === user.id ? { ...item, status: "failed", instruction: false } : item));
    } finally { setBusy(null); }
  }

  async function applyPlan() {
    if (!preview || !apiReady || busy || clarification || invalidEditCount > 0 || (preview.requires_confirmation && !riskAcknowledged)) return;
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
      setCompletion(result);
      setUndoAvailable(result.undo_available && result.moved > 0);
      resetPreview();
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "Could not organize the selected files.";
      if (caught instanceof ApiError && caught.payload && typeof caught.payload === "object") {
        const payload = caught.payload as Partial<ApplyResult>;
        if (typeof payload.moved === "number" && payload.moved > 0) {
          const requested = typeof payload.requested === "number" ? payload.requested : selectedCount;
          setCompletion({
            status: "partial",
            moved: payload.moved,
            requested,
            failed: typeof payload.failed === "number" ? payload.failed : Math.max(requested - payload.moved, 0),
            failures: Array.isArray(payload.failures) ? payload.failures : [],
            undo_available: Boolean(payload.undo_available),
            kept_in_root: typeof payload.kept_in_root === "number" ? payload.kept_in_root : 0,
            hierarchy: payload.hierarchy ?? { name: "Source", files: [], folders: [] },
            detail: payload.detail,
          });
          setUndoAvailable(Boolean(payload.undo_available));
        } else {
          setUndoAvailable(false);
        }
      } else {
        // A dropped connection leaves the filesystem outcome unknown. Keep Undo
        // reachable, but remove the preview so this destructive request cannot
        // be repeated accidentally.
        setUndoAvailable(true);
      }
      setError(message);
      setMessage("");
      resetPreview();
    } finally {
      setBusy(null);
    }
  }

  async function undo() {
    if (!source.trim() || !apiReady) return;
    setBusy("undo");
    setError("");
    try {
      const result = await request<UndoResult>(apiBase, "/api/undo", { source });
      const feedback = describeUndoResult(result);
      if (feedback.tone === "success") {
        setMessage(feedback.message);
        setError("");
      } else {
        setMessage("");
        setError(feedback.message);
      }
      setUndoAvailable(feedback.recoveryAvailable);
      if (!feedback.recoveryAvailable) setCompletion(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Undo could not be completed.");
    } finally {
      setBusy(null);
    }
  }

  function toggleTarget(target: string) {
    const files = groups.find(([name]) => name === target)?.[1] ?? [];
    const allAccepted = files.every((file) => fileEdits[file.id]?.accepted ?? file.selected);
    setFileEdits((current) => {
      const next = { ...current };
      for (const file of files) {
        next[file.id] = {
          ...(current[file.id] ?? {
            accepted: file.selected,
            target_path: file.target_path,
            destination_name: file.destination_name,
          }),
          accepted: !allAccepted,
        };
      }
      return next;
    });
  }

  function toggleReviewGroup(target: string) {
    setExpandedGroups((current) => ({
      ...current,
      [target]: !current[target],
    }));
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
    <div className="sortly-workspace min-h-screen text-ink">
      <header className="sortly-header">
        <button type="button" className="sortly-home" aria-label="Sortly home" onClick={() => navigateTo("organize")}>
          <img src={sortlyMark} alt="" width="42" height="42" />
          <span className="font-display font-semibold">Sortly</span>
        </button>
        <nav className="sortly-tabs" aria-label="Workspace">
          <button type="button" data-active={activeView === "preview" || activeView === "organize"}
            aria-current={activeView === "preview" || activeView === "organize" ? "page" : undefined} onClick={() => navigateTo("preview")}>Preview</button>
          <button type="button" data-active={activeView === "settings"}
            aria-current={activeView === "settings" ? "page" : undefined} onClick={() => navigateTo("settings")}>Settings</button>
          <button type="button" data-active={activeView === "providers"}
            aria-current={activeView === "providers" ? "page" : undefined} onClick={() => navigateTo("providers")}>Providers</button>
          <button type="button" data-active={activeView === "history"}
            aria-current={activeView === "history" ? "page" : undefined} onClick={() => navigateTo("history")}>History</button>
        </nav>
      </header>
      <main className="sortly-main">
        <section className="sortly-start" hidden={activeView !== "organize" && activeView !== "preview"} aria-label="Analyze a folder">
          <div className="sortly-folder-row">
            <div className="ui-input-shell source-folder-control flex items-center gap-3">
              <HardDrive size={17} className="shrink-0 text-ink-muted" />
              <input aria-label="Folder directory" value={source} disabled={busy !== null}
                onChange={(event) => changeSource(event.target.value)}
                className="min-w-0 flex-1 bg-transparent text-sm text-ink outline-none placeholder:text-ink-faint"
                placeholder="Folder directory" />
              <Button variant="secondary" size="sm" className="source-browse-button" onClick={chooseFolder}
                disabled={busy !== null || !apiReady}><Folder size={14} /> Browse</Button>
            </div>
            <Button className="sortly-analyze-button" aria-label={busy === "preview" ? "Cancel analysis" : "Analyze folder"}
              title={busy === "preview" ? "Cancel analysis" : "Analyze folder"}
              data-analyzing={busy === "preview"} aria-busy={busy === "preview"}
              onClick={busy === "preview" ? cancelAnalysis : createPreview}
              disabled={(busy !== null && busy !== "preview") || !source.trim() || !apiReady}>
              {busy === "preview" ? <X size={17} /> : <FolderSearch size={17} strokeWidth={1.8} />}
              <span className="sortly-analyze-label">{busy === "preview" ? "Cancel" : "Analyze"}</span>
            </Button>
          </div>
          <ChatPanel disabled={busy !== null || Boolean(preview && !apiReady)} busy={busy === "chat"}
            draft={chatDraft} onDraftChange={(text) => {
              setChatDraft(text);
              if (source.trim()) conversationFolder.current = folderIdentity(source);
            }} messages={chatMessages} error={chatError}
            hasPreview={Boolean(preview)} onSend={sendChat} />
          {busy === "preview" && <AnalysisActivity />}
        </section>

        <div className="sortly-feedback">
          {busy === "apply" && <div className="workflow-status mt-4" role="status">
            <LoaderCircle size={17} className="animate-spin" /><span className="text-sm">Applying the confirmed plan…</span>
          </div>}
          {message && <Notice tone="success" text={message} />}
          {error && <Notice tone="error" text={error} />}
          {apiRuntime.state !== "ready" && <ServiceNotice runtime={apiRuntime} subject="Analysis" />}
        </div>

        <section hidden={activeView !== "preview"} aria-label="Organization preview" className="sortly-preview">
          {!preview && !completion && busy !== "preview" && <p className="mt-6 text-center text-sm text-ink-muted">Choose a folder and analyze it to generate a preview.</p>}
          {preview && <>
            {preview.scan_summary.skipped > 0 && <Notice tone="warning" text={describeSkips(preview.scan_summary)} />}
            {preview.plan_warnings.map((warning) => <Notice key={warning} tone="warning" text={warning} />)}
            {clarification && <Notice tone="warning" text={`Ask Sortly needs a clarification: ${clarification}`} />}
            <PreviewSummary preview={preview} stats={reviewStats} groupCount={groups.length} />
          </>}
            <>
              {preview && preview.files.length > 0 && (
                <section

                  className="ui-panel review-shell mt-6 overflow-hidden"
                  inert={busy === "chat"}
                  aria-labelledby="review-heading"
                >
                  <div className="review-header">
                    <div className="min-w-0">
                      <div className="ui-eyebrow flex items-center gap-2 text-ink-muted"><Sparkles size={13} className="text-accent-text" /> Ready for review</div>
                      <h2 ref={reviewHeading} tabIndex={-1} id="review-heading" className="mt-2 font-display text-xl font-semibold tracking-tight">Review files by destination</h2>
                      <p className="mt-1.5 text-xs leading-relaxed text-ink-muted">
                        {reviewBatchMode === "large"
                          ? `${preview.files.length.toLocaleString()} files are grouped into ${groups.length.toLocaleString()} destinations. Review exceptions first, then expand only what you need.`
                          : reviewBatchMode === "medium"
                            ? `${preview.files.length.toLocaleString()} files are grouped into ${groups.length.toLocaleString()} destinations. Use search or open a group to review it.`
                            : `Review destinations and names below. Group controls include or exclude every file in that destination.`}
                      </p>
                    </div>
                    <span className="review-selected-count">
                      <strong>{selectedCount}</strong> of {preview.files.length} included
                    </span>
                  </div>

                  {(reviewStats.attention > 0 || reviewStats.security > 0) && (
                    <div className="review-overview">
                      {reviewStats.security > 0 && (
                        <button type="button" className="review-overview-item" data-tone="security" onClick={() => setReviewFilter("security")}>
                          <AlertTriangle size={15} />
                          <span><strong>{reviewStats.security}</strong> {reviewStats.security === 1 ? "file has" : "files have"} potentially sensitive content</span>
                        </button>
                      )}
                      {reviewStats.attention > 0 && (
                        <button type="button" className="review-overview-item" data-tone="attention" onClick={() => setReviewFilter("attention")}>
                          <AlertTriangle size={15} />
                          <span><strong>{reviewStats.attention}</strong> {reviewStats.attention === 1 ? "file needs" : "files need"} a closer look</span>
                        </button>
                      )}
                    </div>
                  )}

                  {showReviewTools && (
                    <div className="review-toolbar">
                      <label className="review-search">
                        <Search size={15} aria-hidden="true" />
                        <span className="sr-only">Search files and destinations</span>
                        <input
                          value={reviewQuery}
                          onChange={(event) => setReviewQuery(event.target.value)}
                          placeholder="Search files or destinations"
                        />
                      </label>
                      <div className="review-filters" role="group" aria-label="Filter files">
                        {([
                          ["all", "All", preview.files.length],
                          ["attention", "Needs review", reviewStats.attention],
                          ["security", "Security", reviewStats.security],
                        ] as const).map(([value, label, count]) => (
                          <button
                            key={value}
                            type="button"
                            data-active={reviewFilter === value}
                            onClick={() => setReviewFilter(value)}
                            disabled={value !== "all" && count === 0}
                          >
                            {label} <span>{count}</span>
                          </button>
                        ))}
                      </div>
                    </div>
                  )}

                  {filteredReviewGroups.length === 0 && (
                    <div className="review-empty">
                      <Search size={20} />
                      <div><strong>No matching files</strong><p>Try another search or clear the current filter.</p></div>
                      <Button variant="secondary" size="sm" onClick={() => { setReviewFilter("all"); setReviewQuery(""); }}>Clear filters</Button>
                    </div>
                  )}

                  <div className="review-groups">
                    {visibleReviewGroups.map(([target, files]) => {
                      const allFiles = groups.find(([name]) => name === target)?.[1] ?? files;
                      const selected = allFiles.every((file) => fileEdits[file.id]?.accepted ?? file.selected);
                      const groupSummary = summarizeGroup(allFiles, fileEdits);
                      const expanded = Boolean(expandedGroups[target]);
                      const panelId = `review-group-${encodeURIComponent(target)}`;
                      return (
                        <section key={target} className="review-group" data-open={expanded}>
                          <div className="review-group-header">
                            <button
                              type="button"
                              className="review-group-toggle"
                              aria-expanded={expanded}
                              aria-controls={panelId}
                              onClick={() => toggleReviewGroup(target)}
                            >
                              <ChevronDown size={15} className="review-group-chevron" />
                              <Folder size={16} className="shrink-0 text-accent-text" />
                              <span className="min-w-0 text-left">
                                <strong title={target}>{target.split("/").join(" › ")}</strong>
                                <span>
                                  {files.length === allFiles.length
                                    ? `${files.length} ${files.length === 1 ? "file" : "files"}`
                                    : `${files.length} shown · ${allFiles.length} total`}
                                </span>
                              </span>
                            </button>
                            <div className="review-group-meta">
                              {groupSummary.security > 0 && <span className="review-group-flag" data-tone="security">{groupSummary.security} sensitive</span>}
                              {groupSummary.attention > 0 && <span className="review-group-flag" data-tone="attention">{groupSummary.attention} to check</span>}
                              <button
                                type="button"
                                role="switch"
                                aria-checked={selected}
                                aria-label={`Include all files in ${target}`}
                                onClick={() => toggleTarget(target)}
                                className="ui-switch"
                                data-checked={selected}
                              >
                                <span className="ui-switch-thumb" />
                              </button>
                            </div>
                          </div>
                          {expanded && (
                            <div id={panelId} className="review-group-files">
                              {files.map((file) => (
                                <FilePreviewRow
                                  key={file.id}
                                  file={file}
                                  edit={fileEdits[file.id]}
                                  onChange={updateFileEdit}
                                />
                              ))}
                            </div>
                          )}
                        </section>
                      );
                    })}
                  </div>

                  {hiddenGroupCount > 0 && (
                    <div className="review-load-more">
                      <span>{hiddenGroupCount.toLocaleString()} more destinations</span>
                      <Button variant="secondary" size="sm" onClick={() => setVisibleGroupLimit((current) => current + (reviewBatchMode === "large" ? 40 : 80))}>
                        Show more destinations
                      </Button>
                    </div>
                  )}

                  <div className="review-footer">
                    <div className="flex items-center gap-2 text-xs text-ink-muted"><LockKeyhole size={14} /> Nothing changes until you review and confirm.</div>
                    <Button onClick={() => setConfirmOpen(true)} disabled={busy !== null || selectedCount === 0 || invalidEditCount > 0 || Boolean(clarification)}>{busy === "apply" && <LoaderCircle size={15} className="animate-spin" />} {busy === "apply" ? "Applying…" : invalidEditCount > 0 ? "Fix required fields" : "Review & organize"}</Button>
                  </div>
                  {invalidEditCount > 0 && (
                    <div className="plan-validation-summary" role="alert">
                      <AlertTriangle size={15} />
                      <span>{invalidEditCount} selected {invalidEditCount === 1 ? "file needs" : "files need"} a non-empty destination and filename before you can continue.</span>
                    </div>
                  )}
                </section>
              )}
            </>

        </section>
        <div hidden={activeView !== "organize" && activeView !== "preview"}>
          {completion && <CompletionSummary completion={completion} busy={busy !== null} onUndo={undo} onOpenFolder={openFolder}
            onViewHistory={() => navigateTo("history")} />}
        </div>

        <section className="sortly-settings" hidden={activeView !== "settings"} aria-labelledby="settings-heading">
          <div className="sortly-settings-heading">
            <h1 id="settings-heading" className="page-title">Settings</h1>
            <Button variant="ghost" size="sm" onClick={() => navigateTo(preview ? "preview" : "organize")}>Back</Button>
          </div>
          <section className="ui-panel p-5" aria-labelledby="analysis-settings-heading">
            <h2 id="analysis-settings-heading" className="font-display text-lg font-semibold">Analysis</h2>
              <fieldset className="organize-options" disabled={busy !== null}>
                <legend className="ui-eyebrow text-ink-muted">Organize options</legend>
                <div className="organize-options-grid">
                  <OptionSwitch label="Include subfolders" checked={recursive} onChange={(checked) => { setRecursive(checked); resetPreview(); }} />
                  <OptionSwitch label="Suggest cleaner file names" checked={rename} onChange={(checked) => { setRename(checked); resetPreview(); }} />
                  <OptionSwitch label="Reorganize already sorted files" checked={reorganize} onChange={(checked) => { setReorganize(checked); resetPreview(); }} />
                  <OptionSwitch label="Allow redacted cloud analysis for sensitive files" checked={allowSensitiveCloud} tone="warning" onChange={(checked) => { setAllowSensitiveCloud(checked); resetPreview(); }} />
                </div>
              </fieldset>
          </section>
          {settingsVisited && <div className="sortly-settings-sections" inert={busy !== null}>
            <details className="ui-panel"><summary>Appearance</summary><SettingsPanel preference={themePreference} onChange={setThemePreference} /></details>
          </div>}
        </section>
        <section className="sortly-providers" hidden={activeView !== "providers"} aria-labelledby="providers-heading">
          <div className="sortly-settings-heading">
            <h1 id="providers-heading" className="page-title">Providers</h1>
            <Button variant="ghost" size="sm" onClick={() => navigateTo(preview ? "preview" : "organize")}>Back</Button>
          </div>
          <div className="ui-panel p-5" inert={busy !== null}>
            {showProviders && (apiReady ? <ProviderStudio open apiBase={apiBase} presentation="page" /> : <ServiceNotice runtime={apiRuntime} subject="Providers" />)}
          </div>
        </section>
        <section className="sortly-history" hidden={activeView !== "history"} aria-labelledby="history-heading">
          <div className="sortly-settings-heading">
            <h1 id="history-heading" className="page-title">History</h1>
            <Button variant="ghost" size="sm" onClick={() => navigateTo(preview ? "preview" : "organize")}>Back</Button>
          </div>
          <div className="mb-4 flex justify-end">
            <Button variant="secondary" onClick={undo} disabled={!undoAvailable || !source.trim() || busy !== null || !apiReady}><RotateCcw size={15} /> Undo last move</Button>
          </div>
          <div className="ui-panel p-5" inert={busy !== null}>
            {showHistory && (apiReady ? <HistoryPanel apiBase={apiBase} onChanged={() => { setUndoAvailable(false); setCompletion(null); }} /> : <ServiceNotice runtime={apiRuntime} subject="History" />)}
          </div>
        </section>
      </main>

      <Dialog.Root open={confirmOpen} onOpenChange={setConfirmOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="ui-overlay fixed inset-0 z-40" />
          <Dialog.Content className="ui-dialog fixed left-1/2 top-1/2 z-50 w-[min(92vw,30rem)] -translate-x-1/2 -translate-y-1/2 p-6 focus:outline-none">
            <div className="grid size-11 place-items-center rounded-lg bg-accent-soft text-accent-text"><FolderOpen size={21} /></div>
            <Dialog.Title className="font-display mt-4 text-lg font-semibold tracking-tight">Organize these files?</Dialog.Title>
            <Dialog.Description className="mt-2 text-sm leading-relaxed text-ink-soft">Sortly will submit {selectedCount} selected files using the edits above. The backend may normalize paths or resolve collisions before moving. Unselected files stay in the source folder.</Dialog.Description>
            {extensionChangeCount > 0 && <p className="mt-3 text-sm text-ink-soft">
              {extensionChangeCount} selected {extensionChangeCount === 1 ? "file will have its extension" : "files will have their extensions"} changed. File contents will not be converted.
            </p>}
            {preview?.requires_confirmation && (
              <label className="mt-4 flex items-start gap-2 rounded-lg border border-warning/30 bg-warning-soft p-3 text-xs text-warning">
                <input type="checkbox" checked={riskAcknowledged} onChange={(event) => setRiskAcknowledged(event.target.checked)} className="mt-0.5 accent-warning" />
                <span>I reviewed the unusual plan warnings and want to continue. The plan has not been rewritten automatically.</span>
              </label>
            )}
            <div className="mt-6 flex gap-3">
              <Dialog.Close asChild><Button variant="secondary" className="flex-1">Cancel</Button></Dialog.Close>
              <Button className="flex-1" onClick={applyPlan} disabled={busy !== null || !apiReady || Boolean(clarification) || invalidEditCount > 0 || Boolean(preview?.requires_confirmation && !riskAcknowledged)}>{busy === "apply" && <LoaderCircle size={16} className="animate-spin" />} Confirm moves</Button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
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

function OptionSwitch({
  label,
  checked,
  tone = "accent",
  onChange,
  disabled = false,
}: {
  label: string;
  checked: boolean;
  disabled?: boolean;
  tone?: "accent" | "warning";
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="organize-option" data-tone={tone}>
      <span>{label}</span>
      <input
        type="checkbox"
        role="switch"
        aria-checked={checked}
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        className="sr-only"
      />
      <span className="ui-switch" data-checked={checked} aria-hidden="true">
        <span className="ui-switch-thumb" />
      </span>
    </label>
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
      <section className="ui-panel page-panel overflow-hidden" aria-labelledby="appearance-heading">
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

      <section className="ui-panel mt-4 p-6">
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

function PreviewSummary({ preview, stats, groupCount }: {
  preview: Preview;
  stats: ReviewStats;
  groupCount: number;
}) {
  const skippedReasons = Object.entries(preview.scan_summary.reasons ?? {}).filter(([, count]) => count > 0);
  const title = stats.security > 0
    ? "Review sensitive files before organizing."
    : stats.attention > 0
      ? "Most files are ready. A few need a closer look."
      : "Your files are ready to review.";
  return (
    <section

      className="review-summary-strip mt-4 overflow-hidden"
      aria-label="Scan and preview summary"
    >
      <div className="review-summary-copy">
        <div>
          <div className="ui-eyebrow text-ink-muted">Review overview</div>
          <h2>{title}</h2>
          <p>{stats.total.toLocaleString()} files are ready to inspect. Nothing moves until you explicitly confirm.</p>
        </div>
        <span>{stats.selected.toLocaleString()} included</span>
      </div>
      <div className="review-summary-metrics">
        <div><span>Ready</span><strong>{stats.ready.toLocaleString()}</strong></div>
        <div data-tone="attention"><span>Needs a look</span><strong>{stats.attention.toLocaleString()}</strong></div>
        <div data-tone="security"><span>Security</span><strong>{stats.security.toLocaleString()}</strong></div>
        <div><span>Excluded</span><strong>{stats.excluded.toLocaleString()}</strong></div>
        <div><span>Destinations</span><strong>{groupCount.toLocaleString()}</strong></div>
      </div>
      <p className="review-scan-note">
        Analyzed {preview.files.length.toLocaleString()} files in {preview.analysis_seconds}s
        {preview.scan_summary.skipped > 0 ? ` · ${preview.scan_summary.skipped.toLocaleString()} skipped by safety checks` : ""}
        {preview.already_organized > 0 ? ` · ${preview.already_organized.toLocaleString()} already organized` : ""}.
      </p>
      {(skippedReasons.length > 0 || preview.scan_summary.truncated) && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 border-t border-warning/20 bg-warning-soft px-5 py-3 text-xs text-warning">
          {preview.scan_summary.truncated && <span>File-count safety limit reached.</span>}
          {skippedReasons.map(([reason, count]) => <span key={reason}>{count} skipped: {skipReasonLabel(reason)}</span>)}
        </div>
      )}
    </section>
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
  const notReported = Math.max(completion.requested - completion.moved, 0);
  const complete = completion.status === "success" && notReported === 0;
  const movedNothing = completion.moved === 0;
  const StatusIcon = complete ? Check : movedNothing ? X : AlertTriangle;
  const toneClass = complete ? "success" : movedNothing ? "danger" : "warning";
  const title = complete
    ? "Organization complete"
    : movedNothing
      ? "No files were moved"
      : "Plan partially completed";

  return (
    <section

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
              : `${completion.failed || notReported} selected ${(completion.failed || notReported) === 1 ? "file was" : "files were"} not moved. Review the safe failure summary before creating a new preview.`}
          </p>
        </div>
      </div>

      <div className="completion-metrics">
        <div className="summary-metric"><span className="summary-label">Requested</span><strong>{completion.requested}</strong></div>
        <div className="summary-metric" data-tone="success"><span className="summary-label">Moved</span><strong>{completion.moved}</strong></div>
        {(completion.failed > 0 || notReported > 0) && <div className="summary-metric" data-tone="warning"><span className="summary-label">Not moved</span><strong>{completion.failed || notReported}</strong></div>}
        <div className="summary-metric"><span className="summary-label">Kept in source</span><strong>{completion.kept_in_root}</strong></div>
      </div>

      {completion.failures.length > 0 && (
        <div className="border-t border-warning/20 bg-warning-soft px-6 py-4">
          <div className="ui-eyebrow mb-2 text-warning">Files not moved</div>
          <ul className="space-y-1 text-sm text-ink-soft">
            {completion.failures.map((failure) => (
              <li key={`${failure.file}-${failure.reason}`}><strong className="font-medium text-ink">{failure.file}</strong>: {failure.reason}</li>
            ))}
          </ul>
        </div>
      )}

      {completion.detail && !complete && (
        <div className="border-t border-warning/20 px-6 py-3 text-sm text-warning">{completion.detail}</div>
      )}

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
    </section>
  );
}

const FilePreviewRow = memo(function FilePreviewRow({ file, edit, onChange }: {
  file: PlanItem;
  edit: FileEdit | undefined;
  onChange: (fileId: string, edit: FileEdit) => void;
}) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const detailsId = useId();
  const validationId = useId();
  const current = edit ?? {
    accepted: file.selected,
    target_path: file.target_path,
    destination_name: file.destination_name,
  };
  const nameChanged = current.destination_name !== file.name;
  // Apply preserves the extension in the backend's plan, including during name edits.
  const extensionChange = getFilenameExtensionChange(file.name, file.destination_name);
  const destinationInvalid = current.accepted && !current.target_path.trim();
  const filenameInvalid = current.accepted && !current.destination_name.trim();
  const status = getFileReviewStatus(file, current);
  const warningSummary = summarizeFileWarning(file, current);
  const warnings = [...new Set(file.warnings.filter(Boolean))];
  const statusLabel: Record<ReviewFileStatus, string> = {
    ready: "Ready",
    attention: "Needs a look",
    security: "Sensitive",
    excluded: "Not included",
  };
  const change = (next: FileEdit) => onChange(file.id, next);
  return (
    <article data-file-id={file.id} aria-label={file.name} className="plan-file review-file" data-excluded={!current.accepted} data-status={status}>
      <div className="review-file-head">
        <label className="review-include">
          <input
            type="checkbox"
            checked={current.accepted}
            aria-label={`Include ${file.name}`}
            onChange={(event) => change({ ...current, accepted: event.target.checked })}
            className="accent-accent"
          />
          <span className="min-w-0">
            <strong title={file.name}>{file.name}</strong>
          </span>
        </label>
        <div className="review-row-actions">
          {status !== "ready" && status !== "excluded" && <span className="review-status-chip" data-tone={status}>
              <AlertTriangle size={12} />{statusLabel[status]}
          </span>}
          <button type="button" className="file-details-trigger" aria-expanded={detailsOpen} aria-controls={detailsId}
            aria-label={detailsOpen ? "Hide details" : "Edit or view details"} onClick={() => setDetailsOpen((open) => !open)}>
            <span>{detailsOpen ? "Close" : "Details"}</span>
            <ChevronDown size={13} className="file-details-chevron" data-open={detailsOpen} />
          </button>
        </div>
      </div>

      {(nameChanged || extensionChange) && <div className="review-file-changes">
        {nameChanged && <p className="review-rename-summary">
          <ArrowRight size={13} aria-hidden="true" /><span className="sr-only">Rename to</span>
          <code title={current.destination_name}>{current.destination_name}</code>
        </p>}
        {extensionChange && <p className="review-extension-change">
          <span>Planned extension:</span>
          <code>{extensionChange.from || "(none)"}</code>
          <ArrowRight size={13} aria-hidden="true" /><span className="sr-only">to</span>
          <code>{extensionChange.to || "(none)"}</code>
        </p>}
      </div>}
      {current.target_path !== file.target_path && <p className="review-edited-destination">
        <Folder size={12} aria-hidden="true" /><span className="sr-only">Destination:</span>
        <span>{current.target_path.split("/").join(" › ")}</span>
      </p>}
      {!current.accepted && <p className="review-kept-note">Stays in source</p>}

      {warningSummary && status !== "ready" && (
        <p className="review-warning-summary" data-tone={status}>
          <AlertTriangle size={14} />
          <span>{warningSummary}</span>
        </p>
      )}

      <>
        {detailsOpen && (
          <div
            id={detailsId}

            className="overflow-hidden"
          >
            <div className="file-details-panel">
              <p className="review-original-name"><span>Original file</span><code>{file.name}</code></p>
              <div className="review-edit-grid">
                <label>Destination folder
                  <input
                    aria-label="Destination folder"
                    value={current.target_path}
                    onChange={(event) => change({ ...current, target_path: event.target.value })}
                    disabled={!current.accepted}
                    className="ui-field"
                    data-invalid={destinationInvalid}
                    aria-invalid={destinationInvalid}
                    aria-describedby={destinationInvalid ? `${validationId}-destination` : undefined}
                  />
                  {destinationInvalid && <span id={`${validationId}-destination`} className="field-error">Destination cannot be empty.</span>}
                </label>
                <label>Proposed filename
                  <input
                    aria-label="Proposed filename"
                    value={current.destination_name}
                    onChange={(event) => change({ ...current, destination_name: event.target.value })}
                    disabled={!current.accepted}
                    className="ui-field"
                    data-invalid={filenameInvalid}
                    aria-invalid={filenameInvalid}
                    aria-describedby={filenameInvalid ? `${validationId}-filename` : undefined}
                  />
                  {filenameInvalid && <span id={`${validationId}-filename`} className="field-error">Filename cannot be empty.</span>}
                </label>
              </div>
              <p className="edit-guidance">Unsafe filename characters are normalized by the local service. Existing files are never overwritten.</p>

              <div className="review-detail-section">
                <h4>Why Sortly suggested this</h4>
                <p>{file.reasoning}</p>
                {file.rename_reason && nameChanged && <p>{file.rename_reason}</p>}
                {file.evidence_excerpt && <blockquote>“{file.evidence_excerpt}”</blockquote>}
              </div>

              {warnings.length > 0 && (
                <div className="review-detail-section">
                  <h4>Warnings and diagnostics</h4>
                  <ul>
                    {warnings.map((warning) => <li key={warning}>{warning}</li>)}
                  </ul>
                </div>
              )}

              <div className="review-detail-diagnostics">
                <span className="break-all">Current location: {file.current_path}</span>
                <span>Detected type: {file.detected_type || "unknown"}</span>
                <span>Extraction status: {file.extraction_status}</span>
                <span>Classification confidence: {file.confidence}</span>
                {file.evidence_sources.length > 0 && <span>Evidence sources: {file.evidence_sources.join(", ")}</span>}
              </div>
            </div>
          </div>
        )}
      </>
    </article>
  );
});

function Notice({ tone, text }: { tone: "success" | "error" | "warning"; text: string }) {
  const success = tone === "success";
  const warning = tone === "warning";
  return <div

    className={`mt-4 flex items-start gap-2 rounded-lg border p-3 text-sm ${success ? "border-success/25 bg-success-soft text-success" : warning ? "border-warning/25 bg-warning-soft text-warning" : "border-danger/25 bg-danger-soft text-danger"}`}
  >
    {success ? <Check size={16} className="mt-0.5" /> : warning ? <ShieldCheck size={16} className="mt-0.5" /> : <X size={16} className="mt-0.5" />}<span>{text}</span>
  </div>;
}

function HierarchyTree({ node, initiallyOpen = true }: { node: HierarchyNode; initiallyOpen?: boolean }) {
  return <div className="rounded-lg border border-line bg-surface-inset p-3 text-sm">
    <div className="flex items-center gap-2 font-medium text-ink"><FolderOpen size={16} className="text-accent-text" />{node.name}</div>
    <div className="ml-3 mt-2 border-l border-line pl-3">
      {node.folders.map((folder) => <details key={folder.name} open={initiallyOpen} className="group py-1">
        <summary className="flex cursor-pointer list-none items-center gap-2 text-ink-soft"><ChevronDown size={14} className="transition group-open:rotate-0 -rotate-90" /><Folder size={15} className="text-accent-text" />{folder.name}</summary>
        <div className="ml-3 mt-1 border-l border-line pl-3"><HierarchyTreeContents node={folder} initiallyOpen={initiallyOpen} /></div>
      </details>)}
      {node.files.map((file) => <div key={file} className="flex items-center gap-2 py-1 text-ink-soft"><FileText size={14} className="text-ink-faint" />{file}</div>)}
    </div>
  </div>;
}

function HierarchyTreeContents({ node, initiallyOpen = true }: { node: HierarchyNode; initiallyOpen?: boolean }) {
  return <>
    {node.folders.map((folder) => <details key={folder.name} open={initiallyOpen} className="group py-1">
      <summary className="flex cursor-pointer list-none items-center gap-2 text-ink-soft"><ChevronDown size={14} className="transition group-open:rotate-0 -rotate-90" /><Folder size={15} className="text-accent-text" />{folder.name}</summary>
      <div className="ml-3 mt-1 border-l border-line pl-3"><HierarchyTreeContents node={folder} initiallyOpen={initiallyOpen} /></div>
    </details>)}
    {node.files.map((file) => <div key={file} className="flex items-center gap-2 py-1 text-ink-soft"><FileText size={14} className="text-ink-faint" />{file}</div>)}
  </>;
}
