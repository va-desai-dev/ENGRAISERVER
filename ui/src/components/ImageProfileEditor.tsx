import { useEffect, useState, type FormEvent } from "react";
import { api } from "../lib/api";
import type { DiscoveredImageFile } from "../lib/types";
import { shortPath } from "../lib/format";
import { useStore } from "../store/useStore";
import { Sheet } from "./Sheet";
import { Button, ErrorNote, Field } from "./primitives";

const CUSTOM = "__custom__";

interface Props {
  profileId: string | null;
  onClose: () => void;
}

interface FormState {
  id: string;
  name: string;
  modelPath: string;
  encoderPath: string;
  vaePath: string;
  selectedModel: string;
  selectedEncoder: string;
  selectedVae: string;
  gpu: number;
  vramLimit: number;
  tiledVae: number;
  offload: boolean;
  defaultSteps: number;
  defaultCfgScale: number;
  defaultSampler: string;
  defaultScheduler: string;
  notes: string;
}

const BLANK: FormState = {
  id: "", name: "", modelPath: "", encoderPath: "", vaePath: "",
  selectedModel: "", selectedEncoder: "", selectedVae: "",
  gpu: 0, vramLimit: 0, tiledVae: 768, offload: true,
  defaultSteps: 4, defaultCfgScale: 1, defaultSampler: "euler", defaultScheduler: "",
  notes: "",
};

const SAMPLERS = [
  ["euler", "Euler — recommended for Klein"],
  ["euler_a", "Euler A"],
  ["heun", "Heun"],
  ["dpm2", "DPM2"],
  ["dpm++2m", "DPM++ 2M"],
  ["lcm", "LCM"],
  ["ddim", "DDIM"],
  ["res_multistep", "Res Multistep"],
  ["res_2s", "Res 2s"],
] as const;

const SCHEDULERS = [
  ["", "Model default — recommended"],
  ["discrete", "Discrete"],
  ["karras", "Karras"],
  ["exponential", "Exponential"],
  ["ays", "AYS"],
  ["gits", "GITS"],
  ["sgm_uniform", "SGM Uniform"],
  ["simple", "Simple"],
  ["smoothstep", "Smoothstep"],
  ["kl_optimal", "KL Optimal"],
  ["lcm", "LCM"],
  ["flux2", "Flux 2"],
  ["flux", "Flux"],
  ["beta", "Beta"],
] as const;

function selection(path: string, files: DiscoveredImageFile[]): string {
  return files.some((file) => file.path === path) ? path : CUSTOM;
}

interface PickerProps {
  label: string;
  hint: string;
  testId: string;
  selected: string;
  customPath: string;
  files: DiscoveredImageFile[];
  onSelected: (value: string) => void;
  onPath: (value: string) => void;
}

function FilePicker(props: PickerProps) {
  return (
    <>
      <Field label={props.label} wide hint={props.hint}>
        <select
          data-testid={props.testId}
          required
          value={props.selected}
          onChange={(event) => props.onSelected(event.target.value)}
        >
          <option value="">Select a file…</option>
          {props.files.map((file) => (
            <option key={file.path} value={file.path}>
              {file.filename} — {shortPath(file.path, 3)}
            </option>
          ))}
          <option value={CUSTOM}>Enter an absolute path…</option>
        </select>
      </Field>
      {props.selected === CUSTOM && (
        <Field label={`${props.label} path`} wide>
          <input
            required
            value={props.customPath}
            placeholder="/absolute/path/to/file"
            onChange={(event) => props.onPath(event.target.value)}
          />
        </Field>
      )}
    </>
  );
}

export function ImageProfileEditor({ profileId, onClose }: Props) {
  const applySnapshot = useStore((state) => state.applySnapshot);
  const note = useStore((state) => state.note);
  const [form, setForm] = useState<FormState>(BLANK);
  const [files, setFiles] = useState<DiscoveredImageFile[]>([]);
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
    setLoading(true);
    setError("");
    (async () => {
      try {
        const discovery = await api.discoverImageFiles();
        if (cancelled) return;
        setFiles(discovery.files);
        if (isNew) {
          const checkpoint = discovery.files.find((file) =>
            file.kind === "checkpoint" && /klein/i.test(file.filename));
          const encoder = discovery.files.find((file) =>
            file.kind === "encoder" && /qwen3[-_.]?8b/i.test(file.filename));
          const vae = discovery.files.find((file) =>
            file.kind === "vae" && /flux2vae/i.test(file.filename));
          setForm({
            ...BLANK,
            selectedModel: checkpoint?.path ?? "",
            selectedEncoder: encoder?.path ?? "",
            selectedVae: vae?.path ?? "",
          });
        } else {
          const { profile } = await api.getImageProfile(profileId);
          if (cancelled) return;
          setForm({
            id: profile.id,
            name: profile.name,
            modelPath: profile.model_path,
            encoderPath: profile.text_encoder_path,
            vaePath: profile.vae_path,
            selectedModel: selection(profile.model_path, discovery.files),
            selectedEncoder: selection(profile.text_encoder_path, discovery.files),
            selectedVae: selection(profile.vae_path, discovery.files),
            gpu: profile.gpu,
            vramLimit: profile.vram_limit_mib,
            tiledVae: profile.tiled_vae,
            offload: profile.offload_to_cpu,
            defaultSteps: profile.default_steps,
            defaultCfgScale: profile.default_cfg_scale,
            defaultSampler: profile.default_sampler,
            defaultScheduler: profile.default_scheduler,
            notes: profile.notes,
          });
        }
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "Could not open model file.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [open, profileId, isNew]);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((current) => ({ ...current, [key]: value }));
  const checkpoints = files.filter((file) => file.kind === "checkpoint");
  const encoders = files.filter((file) => file.kind === "encoder");
  const vaes = files.filter((file) => file.kind === "vae");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const result = await api.saveImageProfile(form.id, {
        name: form.name,
        model_path: form.selectedModel === CUSTOM ? form.modelPath : form.selectedModel,
        text_encoder_path: form.selectedEncoder === CUSTOM ? form.encoderPath : form.selectedEncoder,
        vae_path: form.selectedVae === CUSTOM ? form.vaePath : form.selectedVae,
        gpu: form.gpu,
        vram_limit_mib: form.vramLimit,
        tiled_vae: form.tiledVae,
        offload_to_cpu: form.offload,
        default_steps: form.defaultSteps,
        default_cfg_scale: form.defaultCfgScale,
        default_sampler: form.defaultSampler,
        default_scheduler: form.defaultScheduler,
        notes: form.notes,
      });
      applySnapshot(result.state);
      note(`Saved model profile ${form.id}`, "good");
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save model profile.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={isNew ? "New Profile" : form.name || form.id || "Image model"}
      eyebrow="Independent image deployment"
      size="lg"
      footer={
        <>
          <span className="foot-hint">Frontend values override the defaults saved here.</span>
          <Button
            variant="primary"
            busy={busy}
            disabled={loading}
            onClick={(event) => {
              (event.currentTarget as HTMLElement).closest(".sheet")?.querySelector("form")?.requestSubmit();
            }}
          >
            Save image model
          </Button>
        </>
      }
    >
      {loading ? <div className="skeleton-rows" aria-hidden="true"><i /><i /><i /><i /></div> : (
        <form onSubmit={submit} className="editor-form">
          <Field label="Image model ID" wide hint={<>This is the <code>model</code> your apps send.</>}>
            <input
              data-testid="image-route-id"
              required
              readOnly={!isNew}
              pattern="[A-Za-z0-9][A-Za-z0-9._\-]{0,127}"
              placeholder="flux-klein-9b"
              value={form.id}
              onChange={(event) => set("id", event.target.value)}
            />
          </Field>
          <Field label="Display Name" wide>
            <input value={form.name} onChange={(event) => set("name", event.target.value)} />
          </Field>
          <FilePicker
            label="Checkpoint" hint="The FLUX / image GGUF." testId="image-model-select"
            selected={form.selectedModel} customPath={form.modelPath} files={checkpoints}
            onSelected={(value) => set("selectedModel", value)} onPath={(value) => set("modelPath", value)}
          />
          <FilePicker
            label="Text encoder" hint="For Klein 9B: Qwen3-8B Q4_K_M GGUF." testId="image-encoder-select"
            selected={form.selectedEncoder} customPath={form.encoderPath} files={encoders}
            onSelected={(value) => set("selectedEncoder", value)} onPath={(value) => set("encoderPath", value)}
          />
          <FilePicker
            label="VAE" hint="For Klein: flux2vae.safetensors." testId="image-vae-select"
            selected={form.selectedVae} customPath={form.vaePath} files={vaes}
            onSelected={(value) => set("selectedVae", value)} onPath={(value) => set("vaePath", value)}
          />
          <div className="grid-3">
            <Field label="GPU"><input type="number" min={0} max={31} value={form.gpu} onChange={(e) => set("gpu", Number(e.target.value))} /></Field>
            <Field label="VRAM cap (MiB)"><input type="number" min={0} value={form.vramLimit} onChange={(e) => set("vramLimit", Number(e.target.value))} /></Field>
            <Field label="Tile above (px)"><input type="number" min={0} value={form.tiledVae} onChange={(e) => set("tiledVae", Number(e.target.value))} /></Field>
          </div>
          <label className="check-row">
            <input type="checkbox" checked={form.offload} onChange={(e) => set("offload", e.target.checked)} />
            <span>CPU offload (recommended when the image GPU has little VRAM)</span>
          </label>
          <div className="block-head">
            <div>
              <h3>Generation defaults</h3>
              <small>Used when a frontend omits the setting. FLUX.2 Klein distilled prefers 4 steps, CFG 1, and Euler.</small>
            </div>
          </div>
          <div className="grid-2">
            <Field label="Steps" hint="Four is the distilled Klein setting.">
              <input type="number" min={1} max={150} required value={form.defaultSteps} onChange={(e) => set("defaultSteps", Number(e.target.value))} />
            </Field>
            <Field label="CFG" hint="Distilled Klein expects 1.0.">
              <input type="number" min={0} max={50} step={0.1} required value={form.defaultCfgScale} onChange={(e) => set("defaultCfgScale", Number(e.target.value))} />
            </Field>
            <Field label="Sampler">
              <select value={form.defaultSampler} onChange={(e) => set("defaultSampler", e.target.value)}>
                {SAMPLERS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </Field>
            <Field label="Scheduler">
              <select value={form.defaultScheduler} onChange={(e) => set("defaultScheduler", e.target.value)}>
                {SCHEDULERS.map(([value, label]) => <option key={value || "default"} value={value}>{label}</option>)}
              </select>
            </Field>
          </div>
          <Field label="Notes" wide>
            <input value={form.notes} onChange={(event) => set("notes", event.target.value)} />
          </Field>
          <ErrorNote>{error}</ErrorNote>
        </form>
      )}
      {loading && <ErrorNote>{error}</ErrorNote>}
    </Sheet>
  );
}
