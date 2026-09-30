import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { api } from "../lib/api";
import type { HubModel, HubVariants, ModelPullBody, ModelPullPreview, ModelPullStatus } from "../lib/types";
import { Button, ErrorNote, Field, StatusDot } from "./primitives";

function patterns(value: string): string[] {
  return value.split(/[\n,]+/).map((item) => item.trim()).filter(Boolean);
}

function bytes(value: number): string {
  if (!value) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const order = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** order).toFixed(order > 2 ? 2 : 1)} ${units[order]}`;
}

export function ModelPuller() {
  const [advanced, setAdvanced] = useState(false);
  const [query, setQuery] = useState("");
  const [models, setModels] = useState<HubModel[] | null>(null);
  const [catalog, setCatalog] = useState<HubVariants | null>(null);
  const [selection, setSelection] = useState("");
  const [searching, setSearching] = useState(false);
  const [loadingVariants, setLoadingVariants] = useState(false);
  const generation = useRef(0);
  const [previewBody, setPreviewBody] = useState<ModelPullBody | null>(null);
  const [repo, setRepo] = useState("");
  const [revision, setRevision] = useState("main");
  const [includes, setIncludes] = useState("*.gguf");
  const [excludes, setExcludes] = useState("");
  const [status, setStatus] = useState<ModelPullStatus | null>(null);
  const [preview, setPreview] = useState<ModelPullPreview | null>(null);
  const [checking, setChecking] = useState(false);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    const next = await api.modelPulls();
    setStatus(next);
    return next;
  }, []);

  useEffect(() => { void refresh().catch(() => {}); }, [refresh]);
  useEffect(() => {
    if (!status?.active) return;
    const timer = window.setInterval(() => { void refresh().catch(() => {}); }, 1500);
    return () => window.clearInterval(timer);
  }, [refresh, status?.active?.id]);

  const chosen = catalog?.variants.filter((variant) => variant.complete &&
    (selection === "all" || variant.id === selection)) ?? [];
  const selectedBytes = chosen.some((variant) => variant.bytes_total === null)
    ? null : chosen.reduce((sum, variant) => sum + (variant.bytes_total ?? 0), 0);
  const body = useMemo<ModelPullBody>(() => advanced ? ({
    repo_id: repo.trim(),
    revision: revision.trim() || "main",
    allow_patterns: patterns(includes),
    ignore_patterns: patterns(excludes),
    force_download: false,
  }) : ({
    repo_id: catalog?.repo_id ?? "",
    revision: catalog?.commit_hash ?? "",
    filenames: catalog?.variants.filter((variant) => variant.complete &&
      (selection === "all" || variant.id === selection)).flatMap((variant) => variant.files) ?? [],
    allow_patterns: ["*.gguf"],
    ignore_patterns: [],
    force_download: false,
  }), [advanced, repo, revision, includes, excludes, catalog, selection]);

  function invalidate() {
    generation.current += 1;
    setPreview(null);
    setPreviewBody(null);
    setError("");
    setChecking(false);
    setSearching(false);
    setLoadingVariants(false);
  }

  function changed(setter: (value: string) => void, value: string) {
    setter(value);
    invalidate();
  }

  async function search(event: FormEvent) {
    event.preventDefault();
    invalidate();
    const current = generation.current;
    setModels(null);
    setCatalog(null);
    setSelection("");
    setSearching(true);
    try {
      const result = await api.searchHubModels(query.trim());
      if (generation.current === current) setModels(result.models);
    } catch (cause) {
      if (generation.current === current) setError(cause instanceof Error ? cause.message : "Search failed");
    } finally {
      if (generation.current === current) setSearching(false);
    }
  }

  async function chooseRepo(id: string) {
    invalidate();
    const current = generation.current;
    setRepo(id);
    setQuery(id);
    setModels(null);
    setCatalog(null);
    setSelection("");
    setLoadingVariants(true);
    try {
      const result = await api.hubVariants(id);
      if (generation.current === current) setCatalog(result);
    } catch (cause) {
      if (generation.current === current) setError(cause instanceof Error ? cause.message : "Could not list quantizations");
    } finally {
      if (generation.current === current) setLoadingVariants(false);
    }
  }

  async function inspect(event?: FormEvent) {
    event?.preventDefault();
    const current = ++generation.current;
    setPreview(null);
    setPreviewBody(null);
    setChecking(true);
    setError("");
    try {
      const result = await api.previewModelPull(body);
      if (generation.current === current) {
        setPreview(result);
        setPreviewBody({ ...body, revision: result.commit_hash });
      }
    } catch (cause) {
      if (generation.current === current) setError(cause instanceof Error ? cause.message : "Could not inspect the repository");
    } finally {
      if (generation.current === current) setChecking(false);
    }
  }

  async function pull() {
    if (!preview || !previewBody) return;
    setStarting(true);
    setError("");
    try {
      // Pull the exact commit that was inspected, even if a branch advances
      // while the user reads the file list.
      await api.startModelPull(previewBody);
      setPreview(null);
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not start a download request");
    } finally {
      setStarting(false);
    }
  }

  const active = status?.active;
  const latest = status?.tasks[0] ?? null;
  const shown = active ?? latest;
  const progress = active && active.files_total
    ? Math.round(active.files_completed / active.files_total * 100)
    : 0;

  return (
    <section className="panel model-puller" data-testid="model-puller">
      <div className="panel-head">
        <div>
          <span className="eyebrow">Hugging Face Hub</span>
          <h2>Download models</h2>
        </div>
        <span className="auth-chip">
          <StatusDot tone={status?.authenticated ? "live" : "cold"} />
          {status?.authenticated ? "Token configured" : "Public access"}
        </span>
      </div>

      <button type="button" className="pull-mode" disabled={starting}
        onClick={() => { invalidate(); setAdvanced(!advanced); setModels(null); }}>
        {advanced ? "Back to GGUF browser" : "Advanced: repository and file filters"}
      </button>
      <form className="pull-form" onSubmit={advanced ? inspect : search}>
        {advanced ? <>
        <Field label="Model repository" hint="Hugging Face owner/name">
          <input
            value={repo}
            required
            placeholder="bartowski/model-GGUF"
            data-testid="pull-repo"
            onChange={(event) => changed(setRepo, event.target.value)}
          />
        </Field>
        <Field label="Revision" hint="Branch, tag, or commit">
          <input
            value={revision}
            required
            onChange={(event) => changed(setRevision, event.target.value)}
          />
        </Field>
        <Field label="Include Files" hint="Comma or newline separated; required as a download guard.">
          <textarea
            value={includes}
            required
            rows={2}
            data-testid="pull-patterns"
            onChange={(event) => changed(setIncludes, event.target.value)}
          />
        </Field>
        <Field label="Exclude Files" hint="Optional glob filters.">
          <textarea
            value={excludes}
            rows={2}
            placeholder="*F16*, *BF16*"
            onChange={(event) => changed(setExcludes, event.target.value)}
          />
        </Field>
        </> : <>
          <div className="pull-search">
            <Field label="Find a GGUF model" hint="Search by name, or paste a Hugging Face model page.">
              <input value={query} required maxLength={200} data-testid="hub-search" disabled={starting}
                placeholder="Search models…" onChange={(event) => {
                  changed(setQuery, event.target.value);
                  setModels(null); setCatalog(null); setSelection("");
                }} />
            </Field>
            <Button type="submit" busy={searching} disabled={!query.trim() || starting}>Search</Button>
            {models !== null && <ul className="pull-results" aria-label="GGUF repositories">
              {models.map((model) => <li key={model.repo_id}>
                <button type="button" onClick={() => { void chooseRepo(model.repo_id); }}>
                  <span>{model.repo_id}</span>
                  <small>{model.downloads.toLocaleString()} downloads</small>
                </button>
              </li>)}
              {models.length === 0 && <li>No GGUF repositories found. Try another name or paste a model page.</li>}
            </ul>}
          </div>
          <Field label="Quantization / variant" hint={loadingVariants ? "Loading available files…" : "Choose one variant; split files are grouped automatically."}>
            <select value={selection} data-testid="hub-quant" disabled={!catalog || starting}
              onChange={(event) => changed(setSelection, event.target.value)}>
              <option value="">{catalog ? "Select a quantization…" : "Choose a model repository first"}</option>
              {catalog?.variants.map((variant) => <option key={variant.id} value={variant.id} disabled={!variant.complete}>
                {variant.quantization} · {variant.bytes_total === null ? "Size unknown" : bytes(variant.bytes_total)} · {variant.id}
                {!variant.complete ? " (incomplete shards)" : ""}
              </option>)}
              {catalog?.variants.some((variant) => variant.complete) && <option value="all">Download all variants (large download)</option>}
            </select>
          </Field>
          <div className="pull-selection" aria-live="polite">
            {catalog && <p>{catalog.repo_id} · GGUF files only. Runtime compatibility is not verified; auxiliary projectors are excluded.</p>}
            {selection && <p>{chosen.length} variant{chosen.length === 1 ? "" : "s"} · {body.filenames?.length} files · {selectedBytes === null ? "Size unknown — check preview" : bytes(selectedBytes)}</p>}
            {selection === "all" && <p className="pull-warning">This downloads every complete model variant, not just one quantization. Review the total size before confirming.</p>}
          </div>
        </>}
        <div className="pull-actions">
          <span className="pull-destination" title={status?.destination}>
            Destination: <code>{status?.destination ?? "…"}</code>
          </span>
          {advanced ? <Button type="submit" busy={checking} disabled={Boolean(active) || starting}>Inspect Files</Button>
            : <Button type="button" busy={checking} disabled={!selection || !chosen.length || Boolean(active) || starting}
                onClick={() => { void inspect(); }}>Review download</Button>}
        </div>
      </form>

      {preview && (
        <div className="pull-preview" data-testid="pull-preview">
          <div>
            <strong>{preview.files_total} files · {bytes(preview.bytes_total)}</strong>
            <small>{bytes(preview.bytes_to_download)} needs downloading · commit {preview.commit_hash.slice(0, 12)}</small>
          </div>
          <ul>
            {preview.files.slice(0, 8).map((file) => (
              <li key={file.filename}>
                <code>{file.filename}</code>
                <span>{file.cached ? "cached" : file.size === null ? "unknown" : bytes(file.size)}</span>
              </li>
            ))}
            {preview.files.length > 8 && <li><span>+ {preview.files.length - 8} more files</span></li>}
          </ul>
          <Button variant="primary" busy={starting} onClick={() => { void pull(); }}>
            Confirm download · {bytes(preview.bytes_to_download)}
          </Button>
        </div>
      )}

      {shown && (
        <div className={`pull-status pull-status-${shown.status}`} role="status">
          <div>
            <strong>{shown.repo_id}</strong>
            <small>
              {shown.status}{active?.files_total
                ? ` · ${active.files_completed}/${active.files_total} Files · ${progress}%`
                : ""}
            </small>
          </div>
          {active && (
            <Button
              variant="ghost"
              disabled={active.status === "cancelling"}
              onClick={() => {
                void api.cancelModelPull(active.id).then(refresh).catch((cause) => {
                  setError(cause instanceof Error ? cause.message : "Could not cancel");
                });
              }}
            >
              {active.status === "cancelling" ? "Stopping…" : "Cancel"}
            </Button>
          )}
          {shown.error && <p>{shown.error}</p>}
        </div>
      )}
      <ErrorNote>{error}</ErrorNote>
      <p className="pull-secret-note">
        Private or gated repositories use the server&apos;s <code>HF_TOKEN</code>. The token never enters this page.
      </p>
    </section>
  );
}
