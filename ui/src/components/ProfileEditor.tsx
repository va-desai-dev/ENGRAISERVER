import { useEffect, useState, type FormEvent } from "react";
import { api } from "../lib/api";
import type { DeploymentProfile, DiscoveredModel } from "../lib/types";
import { shortPath } from "../lib/format";
import { useStore } from "../store/useStore";
import { Sheet } from "./Sheet";
import { Button, ErrorNote, Field } from "./primitives";

const CUSTOM = "__custom__";
type Backend = "auto" | "cuda" | "rocm" | "metal" | "vulkan" | "cpu";
type KvCache = "f16" | "q8_0" | "q4_0";
type Template = "metadata" | "jinja" | "none";

interface Props {
  profileId: string | null;
  onClose: () => void;
}

interface FormState {
  id: string;
  name: string;
  modelPath: string;
  selected: string;
  notes: string;
  backend: Backend;
  devices: string;
  gpuLayers: string;
  splitMode: "none" | "layer" | "row";
  mainGpu: string;
  tensorSplit: string;
  context: string;
  kvCache: KvCache;
  flashAttention: boolean;
  kvOffload: boolean;
  contextShift: boolean;
  cacheReuse: string;
  parallelSlots: string;
  template: Template;
  thinking: boolean;
  defaultTokens: string;
  reasoningEffort: "low" | "medium" | "high" | "";
  extraArguments: string;
}

const BLANK: FormState = {
  id: "", name: "", modelPath: "", selected: "", notes: "",
  backend: "auto", devices: "", gpuLayers: "auto", splitMode: "layer", mainGpu: "", tensorSplit: "",
  context: "32768", kvCache: "q8_0", flashAttention: true,
  kvOffload: true, contextShift: true, cacheReuse: "256", parallelSlots: "1",
  template: "jinja", thinking: true, defaultTokens: "2048",
  reasoningEffort: "low", extraArguments: "",
};

function importDeployment(deployment: DeploymentProfile): Partial<FormState> {
  return {
    backend: deployment.compute.backend,
    devices: deployment.compute.devices.join(", "),
    gpuLayers: String(deployment.compute.gpu_layers),
    splitMode: deployment.compute.split_mode,
    mainGpu: deployment.compute.main_gpu === null ? "" : String(deployment.compute.main_gpu),
    tensorSplit: deployment.compute.tensor_split.join(", "),
    context: deployment.memory.context === null ? "" : String(deployment.memory.context),
    kvCache: deployment.memory.kv_cache,
    flashAttention: deployment.compute.flash_attention,
    kvOffload: deployment.memory.kv_offload,
    contextShift: deployment.memory.context_shift,
    cacheReuse: String(deployment.memory.cache_reuse_min_tokens),
    parallelSlots: String(deployment.memory.parallel_slots),
    defaultTokens: deployment.generation.default_tokens === null
      ? "" : String(deployment.generation.default_tokens),
    reasoningEffort: deployment.generation.reasoning_effort ?? "",
    extraArguments: deployment.advanced.extra_arguments.join("\n"),
  };
}

function numbers(value: string): number[] {
  if (!value.trim()) return [];
  return value.split(/[\s,]+/).filter(Boolean).map(Number);
}

function integerOrNull(value: string): number | null {
  return value.trim() ? Number.parseInt(value, 10) : null;
}

function shellWords(value: string): string[] {
  const words: string[] = [];
  let word = "";
  let quote: "'" | '"' | null = null;
  let escaped = false;
  let started = false;
  for (const character of value) {
    if (escaped) {
      word += character;
      escaped = false;
      started = true;
    } else if (character === "\\" && quote !== "'") {
      escaped = true;
      started = true;
    } else if (quote) {
      if (character === quote) quote = null;
      else word += character;
      started = true;
    } else if (character === "'" || character === '"') {
      quote = character;
      started = true;
    } else if (/\s/.test(character)) {
      if (started) words.push(word);
      word = "";
      started = false;
    } else {
      word += character;
      started = true;
    }
  }
  if (escaped) throw new Error("Advanced arguments end with an incomplete escape");
  if (quote) throw new Error("Advanced arguments contain an unclosed quote");
  if (started) words.push(word);
  return words;
}

export function ProfileEditor({ profileId, onClose }: Props) {
  const applySnapshot = useStore((state) => state.applySnapshot);
  const note = useStore((state) => state.note);
  const [form, setForm] = useState<FormState>(BLANK);
  const [library, setLibrary] = useState<DiscoveredModel[]>([]);
  const [roots, setRoots] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const open = profileId !== null;
  const isNew = profileId === "";
  const [renderedFor, setRenderedFor] = useState(profileId);

  if (profileId !== renderedFor) {
    setRenderedFor(profileId);
    setLoading(true);
  }

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setError("");
    setLoading(true);
    (async () => {
      try {
        const discovery = await api.discover();
        if (cancelled) return;
        setLibrary(discovery.models);
        setRoots(discovery.library.model_search_roots.join(", "));
        if (isNew) {
          setForm(BLANK);
        } else {
          const { profile } = await api.getProfile(profileId);
          if (cancelled) return;
          const imported = profile.deployment ? importDeployment(profile.deployment) : {};
          setForm({
            ...BLANK,
            ...imported,
            id: profile.id,
            name: profile.name,
            modelPath: profile.model_path,
            selected: discovery.models.some((model) => model.path === profile.model_path)
              ? profile.model_path : CUSTOM,
            notes: profile.notes,
            template: profile.prompt?.template ?? imported.template ?? "jinja",
            thinking: profile.prompt?.thinking ?? imported.thinking ?? true,
          });
        }
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "Could not open deployment");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [open, profileId, isNew]);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((current) => ({ ...current, [key]: value }));

  async function rescan() {
    setBusy(true);
    setError("");
    try {
      const result = await api.setSearchRoots(roots);
      setLibrary(result.models);
      setRoots(result.library.model_search_roots.join(", "));
      note(`Library rescanned: ${result.models.length} files`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not rescan");
    } finally {
      setBusy(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const modelPath = form.selected === CUSTOM ? form.modelPath : form.selected;
      const result = await api.saveProfile(form.id, {
        name: form.name,
        model_path: modelPath,
        notes: form.notes,
        prompt: { template: form.template, adapter: "auto", thinking: form.thinking },
        deployment: {
          compute: {
            backend: form.backend,
            devices: numbers(form.devices),
            gpu_layers: form.gpuLayers === "auto" || form.gpuLayers === "all"
              ? form.gpuLayers : Number.parseInt(form.gpuLayers, 10),
            split_mode: form.splitMode,
            main_gpu: integerOrNull(form.mainGpu),
            tensor_split: numbers(form.tensorSplit),
            flash_attention: form.flashAttention,
          },
          memory: {
            context: integerOrNull(form.context),
            kv_cache: form.kvCache,
            kv_offload: form.kvOffload,
            context_shift: form.contextShift,
            cache_reuse_min_tokens: Number.parseInt(form.cacheReuse, 10),
            parallel_slots: Number.parseInt(form.parallelSlots, 10),
          },
          generation: {
            default_tokens: integerOrNull(form.defaultTokens),
            reasoning_effort: form.reasoningEffort || null,
          },
          advanced: { extra_arguments: shellWords(form.extraArguments) },
        },
      });
      applySnapshot(result.state);
      note(`Saved ${form.id}`, "good");
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save deployment");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={isNew ? "New Model Profile" : form.name || form.id || "Profile"}
      eyebrow="Typed model deployment"
      size="lg"
      footer={<>
        <span className="foot-hint">Stored in this host&apos;s XDG configuration</span>
        <Button variant="primary" busy={busy} disabled={loading} onClick={(event) => {
          (event.currentTarget as HTMLElement).closest(".sheet")?.querySelector("form")?.requestSubmit();
        }}>Save deployment</Button>
      </>}
    >
      {loading ? <div className="skeleton-rows" aria-hidden="true"><i /><i /><i /><i /></div> : (
        <form id="profile-form" onSubmit={submit} className="editor-form">
          <Field label="Model ID" wide hint={<>Client devices will use this value as <code>model</code>.</>}>
            <input data-testid="route-id" value={form.id} required readOnly={!isNew}
              pattern="[A-Za-z0-9][A-Za-z0-9._\-]{0,127}" placeholder="my-model-q4"
              data-autofocus={isNew ? "" : undefined}
              onChange={(event) => set("id", event.target.value)} />
          </Field>
          <Field label="Display Name" wide>
            <input value={form.name} placeholder="Model name" onChange={(event) => set("name", event.target.value)} />
          </Field>
          <Field label="Model artifact" wide hint={`${library.length} GGUF files found on this host`}>
            <select data-testid="model-select" value={form.selected} required
              onChange={(event) => set("selected", event.target.value)}>
              <option value="">Select a file…</option>
              {library.map((model) => <option key={model.path} value={model.path}>
                {model.filename} — {shortPath(model.path, 3)}
              </option>)}
              <option value={CUSTOM}>Enter an absolute file path…</option>
            </select>
          </Field>
          {form.selected === CUSTOM && <Field label="Absolute Path" wide>
            <input value={form.modelPath} placeholder="/path/to/model.gguf"
              onChange={(event) => set("modelPath", event.target.value)} />
          </Field>}
          <Field label="Search Directories" wide hint="Host-local model roots; blank restores the default.">
            <div className="inline-field">
              <input data-testid="search-roots" value={roots} onChange={(event) => setRoots(event.target.value)} />
              <Button type="button" busy={busy} onClick={() => { void rescan(); }}>Rescan</Button>
            </div>
          </Field>
          <Field label="Compute backend">
            <select data-testid="backend" value={form.backend} onChange={(event) => set("backend", event.target.value as Backend)}>
              <option value="auto">Auto</option><option value="cuda">CUDA</option>
              <option value="rocm" disabled>ROCm (provider unavailable)</option><option value="metal">Metal</option>
              <option value="vulkan">Vulkan</option><option value="cpu">CPU</option>
            </select>
          </Field>
          <Field label="Devices" hint="Comma-separated indexes; blank asks the ENGRAI planner to select them.">
            <input data-testid="devices" value={form.devices} placeholder="0, 1" onChange={(event) => set("devices", event.target.value)} />
          </Field>
          <Field label="GPU layers" hint="Auto, all, or a non-negative count.">
            <input data-testid="gpu-layers" value={form.gpuLayers} onChange={(event) => set("gpuLayers", event.target.value)} />
          </Field>
          <Field label="Tensor split" hint="One positive value per selected device.">
            <input data-testid="tensor-split" value={form.tensorSplit} placeholder="60, 4" onChange={(event) => set("tensorSplit", event.target.value)} />
          </Field>
          <Field label="Split mode" hint="Layer is safest; row can improve specific multi-GPU layouts.">
            <select value={form.splitMode} onChange={(event) => set("splitMode", event.target.value as FormState["splitMode"])}>
              <option value="layer">Layer</option><option value="row">Row</option><option value="none">None</option>
            </select>
          </Field>
          <Field label="Main GPU" hint="Host device index; primarily used by row split.">
            <input type="number" min="0" value={form.mainGpu} placeholder="Automatic"
              onChange={(event) => set("mainGpu", event.target.value)} />
          </Field>
          <Field label="Context tokens">
            <input data-testid="context" type="number" min="1" value={form.context} onChange={(event) => set("context", event.target.value)} />
          </Field>
          <Field label="KV cache">
            <select data-testid="kv-cache" value={form.kvCache} onChange={(event) => set("kvCache", event.target.value as KvCache)}>
              <option value="f16">F16</option><option value="q8_0">Q8_0</option><option value="q4_0">Q4_0</option>
            </select>
          </Field>
          <Field label="Parallel slots" hint="Concurrent sequences sharing the configured and loaded model (leave to one if you are the only user).">
            <input type="number" min="1" value={form.parallelSlots}
              onChange={(event) => set("parallelSlots", event.target.value)} />
          </Field>
          <Field label="Chunk reuse threshold"
            hint="Advanced chunk reuse; exact-prefix slot caching remains separate. Unsupported architectures ignore this request.">
            <input type="number" min="0" value={form.cacheReuse}
              onChange={(event) => set("cacheReuse", event.target.value)} />
          </Field>
          <Field label="Prompt template">
            <select data-testid="template" value={form.template} onChange={(event) => set("template", event.target.value as Template)}>
              <option value="metadata">Model metadata</option><option value="jinja">Jinja</option><option value="none">None</option>
            </select>
          </Field>
          <Field label="Reasoning Effort">
            <select value={form.reasoningEffort} onChange={(event) => set("reasoningEffort", event.target.value as FormState["reasoningEffort"])}>
              <option value="">Engine Default</option><option value="low">Low</option>
              <option value="medium">Medium</option><option value="high">High</option>
            </select>
          </Field>
          <Field label="Default output tokens">
            <input type="number" min="1" value={form.defaultTokens} onChange={(event) => set("defaultTokens", event.target.value)} />
          </Field>
          <Field label="Flash attention">
            <input type="checkbox" checked={form.flashAttention} onChange={(event) => set("flashAttention", event.target.checked)} />
          </Field>
          <Field label="Offload KV Cache on GPU">
            <input type="checkbox" checked={form.kvOffload}
              onChange={(event) => set("kvOffload", event.target.checked)} />
          </Field>
          <Field label="Context shifting" hint="Requested only; effective support is reported after the model loads.">
            <input type="checkbox" checked={form.contextShift}
              onChange={(event) => set("contextShift", event.target.checked)} />
          </Field>
          <Field label="Enable Thinking">
            <input type="checkbox" checked={form.thinking} disabled={form.template !== "jinja"}
              onChange={(event) => set("thinking", event.target.checked)} />
          </Field>
          <Field label="Notes" wide>
            <input value={form.notes} placeholder="Deployment notes" onChange={(event) => set("notes", event.target.value)} />
          </Field>
          <details className="field-wide">
            <summary>Advanced compatibility arguments</summary>
            <Field label="Unmodeled provider arguments" wide
              hint="Fine tune for experimental model flags that may not be commonly used across models.">
              <textarea className="flags" data-testid="extra-arguments" spellCheck={false} rows={4}
                value={form.extraArguments} onChange={(event) => set("extraArguments", event.target.value)} />
            </Field>
          </details>
          <ErrorNote>{error}</ErrorNote>
        </form>
      )}
      {loading && <ErrorNote>{error}</ErrorNote>}
    </Sheet>
  );
}
