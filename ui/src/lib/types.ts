/* Transitional gateway response shapes. These mirror the compiled API output
   while the UI moves to generated contracts from @models; if a field moves it must move
   here, which is the point of writing them down. */

export type Phase = "stopped" | "idle" | "starting" | "ready" | "stopping" | string;

export interface Engine {
  reachable: boolean;
  owned: boolean;
  pid: number | null;
  version?: string;
  llm?: boolean;
  error?: string;
  capabilities?: Record<string, {
    requested: boolean;
    effective: boolean;
    detail: string;
  }> | null;
}

export interface ImageEngine extends Engine {
  txt2img?: boolean;
}

export interface RuntimeState {
  active_model: string | null;
  phase: Phase;
  target_model: string | null;
  pid: number | null;
  error: string | null;
  changed_at: number;
}

export interface ModelProfile {
  id: string;
  filename: string;
  name: string;
  model_path: string;
  model_filename: string;
  prompt: {
    template: "metadata" | "jinja" | "none";
    adapter: string;
    thinking: boolean;
  } | null;
  deployment?: DeploymentProfile | null;
  size_gb: number | null;
  context_tokens: number;
  gpu_layers: number;
  quantization: string;
  routable: boolean;
  missing_paths: string[];
  notes: string;
  modified_at: number;
  /* Added by the snapshot, not stored on the profile. */
  active: boolean;
  phase: Phase;
}

export interface DeploymentProfile {
  schema_version: 1;
  id: string;
  model: string;
  engine: "llama.cpp" | "stable-diffusion.cpp";
  engine_version: string | null;
  compute: {
    backend: "auto" | "cuda" | "rocm" | "metal" | "vulkan" | "cpu";
    devices: number[];
    gpu_layers: "auto" | "all" | number;
    split_mode: "none" | "layer" | "row";
    main_gpu: number | null;
    tensor_split: number[];
    flash_attention: boolean;
  };
  memory: {
    context: number | null;
    kv_cache: "f16" | "q8_0" | "q4_0";
    kv_offload: boolean;
    context_shift: boolean;
    cache_reuse_min_tokens: number;
    parallel_slots: number;
  };
  generation: {
    default_tokens: number | null;
    reasoning_effort: "low" | "medium" | "high" | null;
  };
  advanced: { extra_arguments: string[] };
}

export interface ImageModelProfile {
  id: string;
  filename: string;
  name: string;
  model_path: string;
  model_filename: string;
  text_encoder_path: string;
  vae_path: string;
  gpu: number;
  vram_limit_mib: number;
  tiled_vae: number;
  offload_to_cpu: boolean;
  default_steps: number;
  default_cfg_scale: number;
  default_sampler: string;
  default_scheduler: string;
  command: string;
  size_gb: number | null;
  quantization: string;
  routable: boolean;
  missing_paths: string[];
  notes: string;
  modified_at: number;
  active: boolean;
  phase: Phase;
}

/* A load or unload accepted with wait=false and still running. Null whenever
   nothing is detached, which is always the case for this UI: its buttons wait
   for the response. Declared anyway so the type keeps describing what the
   gateway actually sends. */
export interface BackgroundOperation {
  kind: "load" | "unload";
  model: string | null;
}

export interface Snapshot {
  engine: Engine;
  runtime: RuntimeState;
  locked: boolean;
  background: BackgroundOperation | null;
  models: ModelProfile[];
  image_engine: ImageEngine;
  image_runtime: RuntimeState;
  image_locked: boolean;
  image_models: ImageModelProfile[];
  paths: { executable: string; search_roots: string[] };
  profile: Profile;
}

export interface Profile {
  name: string;
  email: string;
  initials: string;
}

export interface Gpu {
  index: number;
  name: string;
  utilization: number;
  used_mib: number;
  total_mib: number;
  percent: number;
  temperature: number;
}

export interface Metrics {
  cpu: { percent: number | null };
  memory: { total_mib: number; used_mib: number; percent: number } | null;
  gpus: Gpu[];
}

export interface ApiKey {
  id: string;
  label: string;
  prefix: string;
  created_at: number;
  last_used_at: number | null;
}

export type CheckStatus = "ok" | "warn" | "fail";

export interface PreflightCheck {
  name: string;
  status: CheckStatus;
  detail: string;
  remedy: string;
}

export interface PreflightReport {
  home: string;
  ok: boolean;
  checks: PreflightCheck[];
}

export interface DiscoveredModel {
  path: string;
  filename: string;
  size_gb?: number | null;
  [key: string]: unknown;
}

export interface DiscoveredImageFile extends DiscoveredModel {
  kind: "checkpoint" | "encoder" | "vae" | "other";
}

export interface Library {
  model_search_roots: string[];
  is_default: boolean;
  default_roots: string[];
}

export interface ModelPullFile {
  filename: string;
  size: number | null;
  cached: boolean;
  will_download: boolean;
}

export interface ModelPullPreview {
  repo_id: string;
  requested_revision: string;
  commit_hash: string;
  files: ModelPullFile[];
  files_total: number;
  bytes_total: number;
  bytes_to_download: number;
  free_bytes: number;
  destination: string;
}

export type ModelPullState =
  | "queued" | "resolving" | "downloading" | "cancelling"
  | "completed" | "failed" | "cancelled" | "interrupted";

export interface ModelPullTask {
  id: string;
  repo_id: string;
  revision: string;
  allow_patterns: string[];
  ignore_patterns: string[];
  force_download: boolean;
  status: ModelPullState;
  created_at: number;
  updated_at: number;
  commit_hash: string | null;
  files_total: number;
  files_completed: number;
  bytes_total: number;
  bytes_completed: number;
  snapshot_path: string | null;
  error: string | null;
  cancel_requested: boolean;
}

export interface ModelPullStatus {
  destination: string;
  authenticated: boolean;
  active: ModelPullTask | null;
  tasks: ModelPullTask[];
}

export interface ModelPullBody {
  filenames?: string[];
  repo_id: string;
  revision: string;
  allow_patterns: string[];
  ignore_patterns: string[];
  force_download: boolean;
}

export interface HubModel {
  repo_id: string;
  downloads: number;
}

export interface HubVariants {
  repo_id: string;
  commit_hash: string;
  variants: {
    id: string;
    quantization: string;
    files: string[];
    bytes_total: number | null;
    complete: boolean;
  }[];
}

export interface Session {
  token: string;
  expires_at: number;
}

/* ── Wall display ─────────────────────────────────────────────────────────
   The reduced snapshot behind /control/display. Deliberately not a subset
   type of Snapshot: the gateway composes it as an allow-list, and mirroring
   that here keeps the two from drifting into sharing fields by accident. */

export interface DisplaySnapshot {
  engine: { reachable: boolean; owned: boolean; version: string | null };
  runtime: { phase: Phase; error: string | null };
  active: {
    id: string;
    name: string;
    quantization: string;
    size_gb: number | null;
    context_tokens: number;
    gpu_layers: number;
  } | null;
  route_count: number;
  metrics: Metrics;
}
