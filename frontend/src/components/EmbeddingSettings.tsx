import { Download, LoaderCircle, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { fetchJson } from "../lib/api";
import { Button } from "./ui/button";

type Device = "auto" | "cpu";
type Model = {
  id: string; label: string; description: string; download_size: string;
  installed: boolean; acceleration: "gpu" | "cpu" | "not_loaded";
};
type DownloadState = {
  id: string; model: string; state: "downloading" | "completed" | "failed";
  total: number; completed: number; message: string;
};
type Config = {
  enabled: boolean; model: string; device: Device; min_files: number;
  service_available: boolean; service_message: string; models: Model[];
  download: DownloadState | null;
};

async function request<T>(base: string, path: string, body?: unknown): Promise<T> {
  return fetchJson<T>(`${base}/api/embeddings/${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  }, "The embedding settings request failed.");
}

export default function EmbeddingSettings({ apiBase, disabled, onSaved }: {
  apiBase: string; disabled: boolean; onSaved: () => void;
}) {
  const [config, setConfig] = useState<Config | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [model, setModel] = useState("all-minilm:22m");
  const [device, setDevice] = useState<Device>("auto");
  const [download, setDownload] = useState<DownloadState | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [startingDownload, setStartingDownload] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function refresh(resetDraft = false) {
    setLoading(true);
    setError("");
    try {
      const state = await request<Config>(apiBase, "config");
      setConfig(state);
      setDownload(state.download);
      if (resetDraft) {
        setEnabled(state.enabled);
        setModel(state.models.some((item) => item.id === state.model) ? state.model : "all-minilm:22m");
        setDevice(state.device);
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not read embedding settings.");
    } finally { setLoading(false); }
  }

  useEffect(() => {
    let active = true;
    setLoading(true);
    void request<Config>(apiBase, "config").then((state) => {
      if (!active) return;
      setConfig(state); setEnabled(state.enabled); setDevice(state.device); setDownload(state.download);
      setModel(state.models.some((item) => item.id === state.model) ? state.model : "all-minilm:22m");
    }).catch((caught: unknown) => {
      if (active) setError(caught instanceof Error ? caught.message : "Could not read embedding settings.");
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [apiBase]);

  useEffect(() => {
    if (download?.state !== "downloading") return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const state = await request<{ download: DownloadState | null }>(apiBase, "download");
        if (!active) return;
        setDownload(state.download);
        if (state.download?.state === "downloading") timer = setTimeout(() => void poll(), 1000);
        else {
          const current = await request<Config>(apiBase, "config");
          if (active) setConfig(current);
        }
      } catch (caught) {
        if (active) setError(caught instanceof Error ? caught.message : "Could not check download progress.");
      }
    }
    timer = setTimeout(() => void poll(), 1000);
    return () => { active = false; clearTimeout(timer); };
  }, [apiBase, download?.id, download?.state]);

  async function save() {
    setSaving(true); setError(""); setNotice("");
    try {
      setConfig(await request<Config>(apiBase, "config", { enabled, model, device }));
      setNotice("Embedding settings saved. They apply to your next analysis.");
      onSaved();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not save embedding settings.");
    } finally { setSaving(false); }
  }

  async function downloadModel() {
    setStartingDownload(true); setError(""); setNotice("");
    try {
      const state = await request<{ download: DownloadState }>(apiBase, "download", { model });
      setDownload(state.download);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not start the download.");
    } finally { setStartingDownload(false); }
  }

  const selected = config?.models.find((item) => item.id === model);
  const downloading = download?.state === "downloading";
  const locked = disabled || loading || saving;
  return <section className="ui-panel mt-4 p-5" aria-labelledby="embedding-settings-heading">
    <h2 id="embedding-settings-heading" className="font-display text-lg font-semibold">Local embeddings</h2>
    <p className="mt-2 text-sm text-ink-muted">MiniLM is the lightweight default. Qwen is optional. Models download separately through Ollama and stay outside the Sortly installer.</p>
    {loading && <p role="status" className="mt-3 text-sm text-ink-muted">Checking local embedding models…</p>}
    {config && <fieldset disabled={locked} className="mt-4 grid gap-3">
      <legend className="sr-only">Embedding preferences</legend>
      <label className="organize-option">
        <span>Use local embeddings</span>
        <input type="checkbox" role="switch" className="sr-only" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
        <span className="ui-switch" data-checked={enabled} aria-hidden="true"><span className="ui-switch-thumb" /></span>
      </label>
      <div className="grid gap-3 md:grid-cols-2">
        <label className="text-xs text-ink-soft">Embedding model
          <select value={model} onChange={(event) => setModel(event.target.value)} className="ui-field mt-1 w-full px-3 py-2 text-sm">
            {config.models.map((item) => <option key={item.id} value={item.id}>{item.label}{item.id === "all-minilm:22m" ? " (default)" : ""}</option>)}
          </select>
        </label>
        <label className="text-xs text-ink-soft">Acceleration
          <select value={device} onChange={(event) => setDevice(event.target.value as Device)} className="ui-field mt-1 w-full px-3 py-2 text-sm">
            <option value="auto">Automatic — GPU when supported</option>
            <option value="cpu">CPU only</option>
          </select>
        </label>
      </div>
      <p className="text-sm text-ink-muted">{selected?.description} {selected?.download_size} download.
        {config.service_available ? selected?.installed ? " Installed locally." : " Not installed yet." : " Installation status cannot be checked until Ollama is running."}
      </p>
      <p className="text-xs text-ink-muted">{selected?.acceleration === "gpu" ? "Currently loaded with GPU acceleration (possibly shared with CPU)."
        : selected?.acceleration === "cpu" ? "Currently loaded on CPU."
        : device === "cpu" ? "CPU only will be used for your next analysis."
        : "Acceleration is reported when Ollama loads the model. Automatic mode uses a supported GPU when available and otherwise uses CPU."}</p>
      <p className="text-xs text-ink-muted">Embeddings run when at least {config.min_files} unresolved files remain. Sensitive files are excluded. The LLM still plans folders and names.</p>
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="secondary" size="sm" onClick={() => void downloadModel()}
          disabled={locked || startingDownload || downloading || !config.service_available || selected?.installed}>
          {startingDownload || downloading ? <LoaderCircle size={14} className="animate-spin" /> : <Download size={14} />}
          {selected?.installed ? "Installed" : downloading ? "Downloading…" : `Download ${selected?.label ?? "model"}`}
        </Button>
        <Button size="sm" onClick={() => void save()} disabled={locked}>
          {saving && <LoaderCircle size={14} className="animate-spin" />} Save embedding settings
        </Button>
      </div>
    </fieldset>}
    <div className="mt-4 flex flex-wrap items-center gap-3">
      <span role="status" className="text-sm text-ink-muted">{config?.service_message}</span>
      <Button variant="ghost" size="sm" disabled={disabled || loading || saving} onClick={() => void refresh(!config)}><RefreshCw size={14} /> Check connection</Button>
      {config && !config.service_available && <a href="https://ollama.com/download" target="_blank" rel="noopener noreferrer" className="text-sm text-accent-text underline">Install Ollama</a>}
    </div>
    {download && <div role="status" className="mt-3 text-sm text-ink-muted">
      <p>{download.message}</p>
      {download.state === "downloading" && download.total > 0 && <>
        <progress className="mt-2 w-full" max={download.total} value={download.completed} aria-label="Model download progress" />
        <p>{(download.completed / 1_000_000).toFixed(1)} / {(download.total / 1_000_000).toFixed(1)} MB</p>
      </>}
    </div>}
    {notice && <p role="status" className="mt-3 text-sm text-ink-muted">{notice}</p>}
    {error && <p role="alert" className="mt-3 text-sm text-danger">{error}</p>}
  </section>;
}
