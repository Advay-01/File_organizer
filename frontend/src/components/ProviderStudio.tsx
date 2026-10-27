import * as Dialog from "@radix-ui/react-dialog";
import { AlertTriangle, ArrowDown, ArrowUp, Check, LoaderCircle, Plus, RefreshCw, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { fetchJson } from "../lib/api";
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
  discovery_state: "" | "success" | "error";
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
  onOpenChange?: (open: boolean) => void;
  apiBase: string;
  presentation?: "dialog" | "page";
}

interface StudioNotice {
  tone: "success" | "warning";
  text: string;
}

const API_KEY_SOURCE_LABELS: Record<SafeConnection["api_key_source"], string> = {
  stored: "Stored securely",
  environment: "Environment",
  missing: "Not set",
};

async function api<T>(base: string, path: string, body?: unknown): Promise<T> {
  return fetchJson<T>(`${base}${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  }, "The Provider Studio request failed.");
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
    discovery_state: "",
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

function draftSignature(primary: DraftRow, fallbacks: DraftRow[]) {
  const fields = (row: DraftRow) => ({
    provider: row.provider,
    model: row.model,
    base_url: row.base_url,
    clear_api_key: row.clear_api_key,
    has_new_api_key: Boolean(row.api_key),
  });
  return JSON.stringify({
    primary: fields(primary),
    fallbacks: fallbacks.map(fields),
  });
}

function credentialSourceLabel(source: string | undefined) {
  if (source === "stored" || source === "environment" || source === "missing") {
    return API_KEY_SOURCE_LABELS[source];
  }
  return "Unavailable";
}

export default function ProviderStudio({ open, onOpenChange, apiBase, presentation = "dialog" }: Props) {
  const [definitions, setDefinitions] = useState<ProviderDefinition[]>([]);
  const [savedConfig, setSavedConfig] = useState<ProviderConfig | null>(null);
  const [primary, setPrimary] = useState<DraftRow>(() => makeRow("openai"));
  const [fallbacks, setFallbacks] = useState<DraftRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<StudioNotice | null>(null);
  const [results, setResults] = useState<TestResult[]>([]);
  const [savedDraftSignature, setSavedDraftSignature] = useState<string | null>(null);

  function setPrimaryRow(next: DraftRow) {
    setPrimary(next);
    setFallbacks((current) => current.filter((row) => row.provider !== next.provider));
  }

  useEffect(() => {
    if (!open || !apiBase) return;
    let active = true;
    setLoading(true);
    setError("");
    setNotice(null);
    setResults([]);
    setSavedConfig(null);
    setSavedDraftSignature(null);
    api<ProviderConfig>(apiBase, "/api/providers/config")
      .then((config) => {
        if (!active) return;
        setDefinitions(config.providers);
        setSavedConfig(config);
        const primaryProvider = config.primary ?? config.providers[0]?.id ?? "openai";
        const nextPrimary = makeRow(primaryProvider, config);
        const nextFallbacks = config.fallbacks.map((provider) => makeRow(provider, config));
        setPrimary(nextPrimary);
        setFallbacks(nextFallbacks);
        setSavedDraftSignature(draftSignature(nextPrimary, nextFallbacks));
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
    setNotice(null);
    try {
      const config = await api<ProviderConfig>(apiBase, "/api/providers/config", payloadFor(primary, fallbacks));
      setDefinitions(config.providers);
      setSavedConfig(config);
      const nextPrimary = makeRow(config.primary ?? primary.provider, config);
      const nextFallbacks = config.fallbacks.map((provider) => makeRow(provider, config));
      setPrimary(nextPrimary);
      setFallbacks(nextFallbacks);
      setSavedDraftSignature(draftSignature(nextPrimary, nextFallbacks));
      setNotice({ tone: "success", text: "Provider configuration saved securely." });
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
      setNotice({
        tone: result.ok ? "success" : "warning",
        text: result.ok ? "Active provider configuration verified." : "One or more configured models failed verification.",
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not test the active provider configuration.");
    } finally {
      setTesting(false);
    }
  }

  async function discover(row: DraftRow, setRow: (row: DraftRow) => void) {
    setRow(updateRow(row, { discovering: true, discovery_message: "", discovery_state: "" }));
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
        discovery_state: "success",
      }));
    } catch (caught) {
      setRow(updateRow(row, {
        discovering: false,
        models: [],
        discovery_message: caught instanceof Error ? caught.message : "Model discovery failed.",
        discovery_state: "error",
      }));
    }
  }

  const used = new Set([primary.provider, ...fallbacks.map((row) => row.provider)]);
  const unused = definitions.filter((provider) => !used.has(provider.id));
  const currentDraftSignature = useMemo(() => draftSignature(primary, fallbacks), [fallbacks, primary]);
  const hasUnsavedChanges = savedDraftSignature !== null && currentDraftSignature !== savedDraftSignature;

  const studio = (
    <>
      <div className="provider-heading">
        <div>
          {presentation === "page"
            ? <h2 id="provider-configuration-heading" className="font-display text-lg font-semibold tracking-tight">Provider configuration</h2>
            : <Dialog.Title className="font-display text-xl font-semibold tracking-tight">Provider Studio</Dialog.Title>}
          {presentation === "page"
            ? <p className="mt-2 max-w-2xl text-sm leading-relaxed text-ink-soft">Choose the provider and model used by Analyze. API keys stay private and use your system’s secure credential storage.</p>
            : <Dialog.Description className="mt-2 max-w-2xl text-sm leading-relaxed text-ink-soft">Choose the provider and model used by Analyze. API keys stay private and use your system’s secure credential storage.</Dialog.Description>}
        </div>
        <div className="provider-heading-actions">
          {!loading && savedConfig && (
            <span className="provider-config-state" data-configured={savedConfig.configured} data-dirty={hasUnsavedChanges}>
              {hasUnsavedChanges ? <AlertTriangle size={13} /> : savedConfig.configured ? <Check size={13} /> : null}
              {hasUnsavedChanges ? "Unsaved changes" : savedConfig.configured ? "Configuration saved" : "Not configured"}
            </span>
          )}
          {presentation === "dialog" && (
            <Dialog.Close asChild><Button variant="ghost" size="sm" aria-label="Close Provider Studio"><X size={17} /></Button></Dialog.Close>
          )}
        </div>
      </div>

      {error && <div className="provider-feedback" data-tone="danger" role="alert"><X size={16} /><span>{error}</span></div>}
      {notice && <div className="provider-feedback" data-tone={notice.tone} role="status">{notice.tone === "success" ? <Check size={16} /> : <X size={16} />}<span>{notice.text}</span></div>}

      {loading ? (
        <div className="provider-loading" role="status">
          <LoaderCircle className="animate-spin" size={18} />
          <div><strong>Loading saved configuration</strong><span>Reading provider settings from the local service.</span></div>
        </div>
      ) : (
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
            <div key={`${row.provider}-${index}`} className="provider-fallback">
              <div className="mb-3 flex items-center justify-between">
                <div>
                  <span className="ui-eyebrow text-ink-muted">Fallback {index + 1}</span>
                  <span className="provider-fallback-name">{definitions.find((item) => item.id === row.provider)?.label ?? row.provider}</span>
                </div>
                <div className="flex gap-1">
                  <Button variant="ghost" size="sm" aria-label={`Move fallback ${index + 1} up`} disabled={index === 0} onClick={() => setFallbacks((current) => {
                    const copy = [...current]; [copy[index - 1], copy[index]] = [copy[index], copy[index - 1]]; return copy;
                  })}><ArrowUp size={14} /></Button>
                  <Button variant="ghost" size="sm" aria-label={`Move fallback ${index + 1} down`} disabled={index === currentLast(fallbacks)} onClick={() => setFallbacks((current) => {
                    const copy = [...current]; [copy[index + 1], copy[index]] = [copy[index], copy[index + 1]]; return copy;
                  })}><ArrowDown size={14} /></Button>
                  <Button variant="ghost" size="sm" aria-label={`Remove fallback ${index + 1}`} onClick={() => setFallbacks((current) => current.filter((_, i) => i !== index))}><Trash2 size={14} /></Button>
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

          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4">
            <Button variant="secondary" size="sm" disabled={!unused.length} onClick={() => {
              const first = unused[0];
              if (first) setFallbacks((current) => [...current, makeRow(first.id, savedConfig ?? undefined)]);
            }}><Plus size={14} /> Add explicit fallback</Button>
            <div className="flex gap-2">
              <Button variant="secondary" onClick={() => void save()} disabled={saving || testing}>{saving && <LoaderCircle size={15} className="animate-spin" />} Save configuration</Button>
              <Button onClick={() => void testConnection()} disabled={saving || testing}>
                {testing && <LoaderCircle size={15} className="animate-spin" />} Test saved configuration
              </Button>
            </div>
          </div>
        </div>
      )}

      {results.length > 0 && <div className="provider-results">{results.map((result) => (
        <div key={`${result.provider}/${result.model}`} className="provider-result" data-ok={result.ok}>
          <div className="flex items-center gap-2">
            {result.ok ? <Check size={15} className="text-success" /> : <X size={15} className="text-danger" />}
            <span className="font-medium text-ink">{result.provider}/{result.model}</span>
            <span className={result.ok ? "text-success" : "text-danger"}>{result.ok ? "verified" : "failed"}</span>
          </div>
          <p className="mt-1 break-words text-xs text-ink-muted">{result.ok ? result.response : result.error}</p>
        </div>
      ))}</div>}
    </>
  );

  if (presentation === "page") {
    return <div aria-labelledby="provider-configuration-heading">{studio}</div>;
  }

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="ui-overlay fixed inset-0 z-40" />
        <Dialog.Content className="ui-dialog fixed left-1/2 top-1/2 z-50 max-h-[90vh] w-[min(94vw,58rem)] -translate-x-1/2 -translate-y-1/2 overflow-y-auto p-6 focus:outline-none">
          {studio}
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
  const credentialNote = !definition?.requires_key
    ? "This provider does not require an API key."
    : row.has_api_key
      ? row.api_key_source === "environment"
        ? "Using a key from the environment. Leave blank to keep this configuration."
        : row.api_key_source === "stored"
          ? "A key is stored securely. Leave blank to keep it."
          : "The credential source is unavailable. Leave blank to keep the current configuration."
      : "No key is stored. Enter one to use this provider.";
  return (
    <div className="provider-editor">
      {title && (
        <div className="provider-section-heading">
          <p className="ui-eyebrow text-ink-muted">{title}</p>
          <span className="provider-name">{definition?.label ?? row.provider}</span>
        </div>
      )}
      <div className="grid gap-3 md:grid-cols-2">
        <label className="text-xs text-ink-soft">Provider
          <select
            value={row.provider}
            onChange={(event) => onChange(makeRow(event.target.value, savedConfig ?? undefined))}
            className="ui-field mt-1 w-full px-3 py-2 text-sm"
          >
            {definitions.filter((item) => item.id === row.provider || !used.has(item.id)).map((item) => (
              <option key={item.id} value={item.id}>{item.label}</option>
            ))}
          </select>
        </label>
        <label className="text-xs text-ink-soft">Model ID
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
            className="ui-field mt-1 w-full px-3 py-2 text-sm"
          />
          <datalist id={listId}>{row.models.map((model) => <option key={model} value={model} />)}</datalist>
        </label>
        <label className="text-xs text-ink-soft">API endpoint <span className="text-ink-faint">(optional)</span>
          <input
            value={row.base_url}
            onChange={(event) => onChange({ ...row, base_url: event.target.value })}
            placeholder={definition?.default_base_url ?? ""}
            className="ui-field mt-1 w-full px-3 py-2 text-sm"
          />
        </label>
        <label className="text-xs text-ink-soft">API key
          <span className="provider-field-note">Write-only</span>
          <input
            type="password"
            autoComplete="off"
            value={row.api_key}
            onChange={(event) => onChange({ ...row, api_key: event.target.value })}
            placeholder={row.has_api_key ? "Stored securely; leave blank to keep" : definition?.requires_key ? "Enter provider API key" : "Not required for Ollama"}
            disabled={!definition?.requires_key}
            className="ui-field mt-1 w-full px-3 py-2 text-sm"
          />
          <span className="provider-field-help">{credentialNote}</span>
        </label>
      </div>
      <div className="provider-editor-actions">
        <Button variant="secondary" size="sm" onClick={onDiscover} disabled={row.discovering}>
          {row.discovering ? <LoaderCircle size={13} className="animate-spin" /> : <RefreshCw size={13} />}
          {row.discovering ? "Refreshing models…" : "Refresh models"}
        </Button>
        {row.has_api_key && row.api_key_source === "stored" && <label className="provider-checkbox"><input type="checkbox" checked={row.clear_api_key} onChange={(event) => onChange({ ...row, clear_api_key: event.target.checked })} /> Clear stored key</label>}
        {row.has_api_key && <span className="provider-source">Source: {credentialSourceLabel(row.api_key_source)}</span>}
      </div>
      {row.discovery_message && (
        <div className="provider-discovery" data-state={row.discovery_state} role="status">
          {row.discovery_state === "success" ? <Check size={14} /> : <X size={14} />}
          <span>{row.discovery_message}</span>
        </div>
      )}
    </div>
  );
}
