import * as Dialog from "@radix-ui/react-dialog";
import { ArrowDown, ArrowUp, Check, LoaderCircle, Plus, RefreshCw, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Button } from "./ui/button";

type ProviderDefinition = {
  id: string;
  label: string;
  default_base_url: string;
  requires_key: boolean;
};

type SafeConnection = {
  provider: string;
  model: string;
  base_url: string;
  has_api_key: boolean;
  api_key_source: "stored" | "environment" | "missing";
};

type ProviderConfig = {
  configured: boolean;
  primary: string | null;
  fallbacks: string[];
  connections: Record<string, SafeConnection>;
  providers: ProviderDefinition[];
};

type DraftRow = {
  provider: string;
  model: string;
  base_url: string;
  api_key: string;
  clear_api_key: boolean;
  has_api_key: boolean;
  api_key_source: SafeConnection["api_key_source"];
  models: string[];
  discovering: boolean;
  discovery_message: string;
};

type TestResult = {
  provider: string;
  model: string;
  ok: boolean;
  response?: string;
  error?: string;
};

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  apiBase: string;
}

async function api<T>(base: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await response.json() as T & { detail?: string };
  if (!response.ok) throw new Error(payload.detail ?? `Request failed with HTTP ${response.status}.`);
  return payload;
}

function makeRow(provider: string, config?: ProviderConfig): DraftRow {
  const saved = config?.connections[provider];
  return {
    provider,
    model: saved?.model ?? "",
    base_url: saved?.base_url ?? "",
    api_key: "",
    clear_api_key: false,
    has_api_key: saved?.has_api_key ?? false,
    api_key_source: saved?.api_key_source ?? "missing",
    models: [],
    discovering: false,
    discovery_message: "",
  };
}

function payloadFor(primary: DraftRow, fallbacks: DraftRow[]) {
  const rows = [primary, ...fallbacks];
  return {
    primary: primary.provider,
    fallbacks: fallbacks.map((row) => row.provider),
    connections: Object.fromEntries(rows.map((row) => [row.provider, {
      model: row.model,
      base_url: row.base_url,
      api_key: row.api_key,
      clear_api_key: row.clear_api_key,
    }])),
  };
}

export default function ProviderStudio({ open, onOpenChange, apiBase }: Props) {
  const [definitions, setDefinitions] = useState<ProviderDefinition[]>([]);
  const [savedConfig, setSavedConfig] = useState<ProviderConfig | null>(null);
  const [primary, setPrimary] = useState<DraftRow>(() => makeRow("openai"));
  const [fallbacks, setFallbacks] = useState<DraftRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [results, setResults] = useState<TestResult[]>([]);

  function setPrimaryRow(next: DraftRow) {
    setPrimary(next);
    setFallbacks((current) => current.filter((row) => row.provider !== next.provider));
  }

  useEffect(() => {
    if (!open || !apiBase) return;
    let active = true;
    setLoading(true);
    setError("");
    api<ProviderConfig>(apiBase, "/api/providers/config")
      .then((config) => {
        if (!active) return;
        setDefinitions(config.providers);
        setSavedConfig(config);
        const primaryProvider = config.primary ?? config.providers[0]?.id ?? "openai";
        setPrimary(makeRow(primaryProvider, config));
        setFallbacks(config.fallbacks.map((provider) => makeRow(provider, config)));
      })
      .catch((caught) => {
        if (active) setError(caught instanceof Error ? caught.message : "Could not load provider settings.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [apiBase, open]);

  function updateRow(row: DraftRow, patch: Partial<DraftRow>): DraftRow {
    return { ...row, ...patch, api_key: patch.api_key ?? row.api_key };
  }

  async function save(): Promise<boolean> {
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const config = await api<ProviderConfig>(apiBase, "/api/providers/config", payloadFor(primary, fallbacks));
      setDefinitions(config.providers);
      setSavedConfig(config);
      setPrimary((row) => makeRow(row.provider, config));
      setFallbacks((rows) => rows.map((row) => makeRow(row.provider, config)));
      setNotice("Provider configuration saved securely.");
      return true;
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not save provider settings.");
      return false;
    } finally {
      setSaving(false);
    }
  }

  async function testConnection() {
    if (!await save()) return;
    setTesting(true);
    setError("");
    setResults([]);
    try {
      const result = await api<{ ok: boolean; results: TestResult[] }>(apiBase, "/api/providers/test", {});
      setResults(result.results);
      setNotice(result.ok ? "Active provider configuration verified." : "One or more configured models failed verification.");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not test the active provider configuration.");
    } finally {
      setTesting(false);
    }
  }

  async function discover(row: DraftRow, setRow: (row: DraftRow) => void) {
    setRow(updateRow(row, { discovering: true, discovery_message: "" }));
    try {
      const result = await api<{ models: string[]; verified: boolean }>(apiBase, "/api/providers/models", {
        provider: row.provider,
        model: row.model,
        base_url: row.base_url,
        api_key: row.api_key,
      });
      setRow(updateRow(row, {
        discovering: false,
        models: result.models,
        discovery_message: `${result.models.length} model IDs retrieved from the provider API. Use Test Connection to verify this selection.`,
      }));
    } catch (caught) {
      setRow(updateRow(row, {
        discovering: false,
        models: [],
        discovery_message: caught instanceof Error ? caught.message : "Model discovery failed.",
      }));
    }
  }

  const used = new Set([primary.provider, ...fallbacks.map((row) => row.provider)]);
  const unused = definitions.filter((provider) => !used.has(provider.id));

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/75 backdrop-blur-sm" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 max-h-[90vh] w-[min(94vw,58rem)] -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-2xl border border-white/10 bg-[#18181f] p-6 shadow-2xl focus:outline-none">
          <div className="flex items-start justify-between gap-6">
            <div>
              <Dialog.Title className="text-xl font-semibold">Provider Studio</Dialog.Title>
              <Dialog.Description className="mt-2 max-w-2xl text-sm leading-relaxed text-zinc-400">
                Choose the exact provider and model used by Analyze. Credentials stay in the Python backend and are encrypted with Windows DPAPI.
              </Dialog.Description>
            </div>
            <Dialog.Close asChild><Button variant="ghost" size="sm" aria-label="Close Provider Studio"><X size={17} /></Button></Dialog.Close>
          </div>

          {loading ? <div className="mt-10 flex items-center gap-2 text-sm text-zinc-400"><LoaderCircle className="animate-spin" size={16} /> Loading saved configuration…</div> : (
            <div className="mt-6 space-y-5">
              <ConnectionEditor
                title="Primary provider"
                row={primary}
                savedConfig={savedConfig}
                definitions={definitions}
                used={used}
                onChange={setPrimaryRow}
                onDiscover={() => discover(primary, setPrimaryRow)}
              />

              {fallbacks.map((row, index) => (
                <div key={`${row.provider}-${index}`} className="rounded-xl border border-white/[0.08] p-4">
                  <div className="mb-3 flex items-center justify-between">
                    <span className="text-xs font-medium uppercase tracking-wide text-zinc-500">Fallback {index + 1}</span>
                    <div className="flex gap-1">
                      <Button variant="ghost" size="sm" disabled={index === 0} onClick={() => setFallbacks((current) => {
                        const copy = [...current]; [copy[index - 1], copy[index]] = [copy[index], copy[index - 1]]; return copy;
                      })}><ArrowUp size={14} /></Button>
                      <Button variant="ghost" size="sm" disabled={index === currentLast(fallbacks)} onClick={() => setFallbacks((current) => {
                        const copy = [...current]; [copy[index + 1], copy[index]] = [copy[index], copy[index + 1]]; return copy;
                      })}><ArrowDown size={14} /></Button>
                      <Button variant="ghost" size="sm" onClick={() => setFallbacks((current) => current.filter((_, i) => i !== index))}><Trash2 size={14} /></Button>
                    </div>
                  </div>
                  <ConnectionEditor
                    row={row}
                    savedConfig={savedConfig}
                    definitions={definitions}
                    used={used}
                    onChange={(next) => setFallbacks((current) => current.map((item, i) => i === index ? next : item))}
                    onDiscover={() => discover(row, (next) => setFallbacks((current) => current.map((item, i) => i === index ? next : item)))}
                  />
                </div>
              ))}

              <div className="flex items-center justify-between border-t border-white/[0.07] pt-4">
                <Button variant="secondary" size="sm" disabled={!unused.length} onClick={() => {
                  const first = unused[0];
                  if (first) setFallbacks((current) => [...current, makeRow(first.id, savedConfig ?? undefined)]);
                }}><Plus size={14} /> Add explicit fallback</Button>
                <div className="flex gap-2">
                  <Button variant="secondary" onClick={() => void save()} disabled={saving || testing}>{saving && <LoaderCircle size={15} className="animate-spin" />} Save</Button>
                  <Button onClick={() => void testConnection()} disabled={saving || testing}>
                    {testing && <LoaderCircle size={15} className="animate-spin" />} Test active configuration
                  </Button>
                </div>
              </div>
            </div>
          )}

          {error && <p role="alert" className="mt-4 rounded-lg border border-red-400/20 bg-red-400/[0.06] p-3 text-sm text-red-300">{error}</p>}
          {notice && <p role="status" className="mt-4 text-sm text-violet-300">{notice}</p>}
          {results.length > 0 && <div className="mt-4 space-y-2">{results.map((result) => (
            <div key={`${result.provider}/${result.model}`} className="rounded-lg border border-white/[0.08] bg-black/20 p-3 text-sm">
              <div className="flex items-center gap-2">
                {result.ok ? <Check size={15} className="text-emerald-400" /> : <X size={15} className="text-red-400" />}
                <span className="font-medium">{result.provider}/{result.model}</span>
                <span className={result.ok ? "text-emerald-300" : "text-red-300"}>{result.ok ? "verified" : "failed"}</span>
              </div>
              <p className="mt-1 break-words text-xs text-zinc-500">{result.ok ? result.response : result.error}</p>
            </div>
          ))}</div>}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function currentLast(rows: DraftRow[]) {
  return rows.length - 1;
}

function ConnectionEditor({ title, row, savedConfig, definitions, used, onChange, onDiscover }: {
  title?: string;
  row: DraftRow;
  savedConfig: ProviderConfig | null;
  definitions: ProviderDefinition[];
  used: Set<string>;
  onChange: (row: DraftRow) => void;
  onDiscover: () => void;
}) {
  const definition = definitions.find((item) => item.id === row.provider);
  const listId = `provider-models-${row.provider}-${row.model}`;
  return (
    <div className="space-y-3">
      {title && <p className="text-xs font-medium uppercase tracking-wide text-zinc-500">{title}</p>}
      <div className="grid gap-3 md:grid-cols-2">
        <label className="text-xs text-zinc-400">Provider
          <select
            value={row.provider}
            onChange={(event) => onChange(makeRow(event.target.value, savedConfig ?? undefined))}
            className="mt-1 w-full rounded-lg border border-white/10 bg-[#0e0e12] px-3 py-2 text-sm text-zinc-100 outline-none focus:border-violet-400/60"
          >
            {definitions.filter((item) => item.id === row.provider || !used.has(item.id)).map((item) => (
              <option key={item.id} value={item.id}>{item.label}</option>
            ))}
          </select>
        </label>
        <label className="text-xs text-zinc-400">Model ID
          <input
            value={row.model}
            onChange={(event) => onChange({ ...row, model: event.target.value })}
            onFocus={(event) => {
              const input = event.currentTarget;
              window.requestAnimationFrame(() => {
                if (document.activeElement === input) input.select();
              });
            }}
            list={listId}
            placeholder="Enter the provider's model ID"
            className="mt-1 w-full rounded-lg border border-white/10 bg-[#0e0e12] px-3 py-2 text-sm text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-violet-400/60"
          />
          <datalist id={listId}>{row.models.map((model) => <option key={model} value={model} />)}</datalist>
        </label>
        <label className="text-xs text-zinc-400">API endpoint <span className="text-zinc-600">(optional)</span>
          <input
            value={row.base_url}
            onChange={(event) => onChange({ ...row, base_url: event.target.value })}
            placeholder={definition?.default_base_url ?? ""}
            className="mt-1 w-full rounded-lg border border-white/10 bg-[#0e0e12] px-3 py-2 text-sm text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-violet-400/60"
          />
        </label>
        <label className="text-xs text-zinc-400">API key
          <input
            type="password"
            autoComplete="off"
            value={row.api_key}
            onChange={(event) => onChange({ ...row, api_key: event.target.value })}
            placeholder={row.has_api_key ? "Stored securely; leave blank to keep" : definition?.requires_key ? "Enter provider API key" : "Not required for Ollama"}
            disabled={!definition?.requires_key}
            className="mt-1 w-full rounded-lg border border-white/10 bg-[#0e0e12] px-3 py-2 text-sm text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-violet-400/60 disabled:opacity-50"
          />
        </label>
      </div>
      <div className="flex flex-wrap items-center gap-3 text-xs text-zinc-500">
        <Button variant="secondary" size="sm" onClick={onDiscover} disabled={row.discovering}>
          {row.discovering ? <LoaderCircle size={13} className="animate-spin" /> : <RefreshCw size={13} />} Refresh models
        </Button>
        {row.has_api_key && <label className="flex items-center gap-2"><input type="checkbox" checked={row.clear_api_key} onChange={(event) => onChange({ ...row, clear_api_key: event.target.checked })} /> Clear stored key</label>}
        {row.has_api_key && <span>Credential source: {row.api_key_source}</span>}
        {row.discovery_message && <span className="min-w-full break-words">{row.discovery_message}</span>}
      </div>
    </div>
  );
}
