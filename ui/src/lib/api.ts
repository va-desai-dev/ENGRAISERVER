import type {
  ApiKey, DiscoveredImageFile, DiscoveredModel, ImageModelProfile, Library, Metrics, ModelProfile,
  ModelPullBody, ModelPullPreview, ModelPullStatus, ModelPullTask, HubModel, HubVariants,
  PreflightReport, Profile, Session, Snapshot,
} from "./types";

/** A gateway response that carried a FastAPI `detail`, or a transport failure. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly retryAfter?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
  /** The session is gone or was never valid; the UI must fall back to the lock screen. */
  get isAuthFailure() {
    return this.status === 401 || this.status === 403;
  }
  /** The gateway itself is unreachable, as opposed to refusing the request. */
  get isOffline() {
    return this.status === 0;
  }
}

type Method = "GET" | "POST" | "PUT" | "DELETE";

/** Supplied by the store so the client never owns session state. */
let readToken: () => string | null = () => null;
export function bindToken(reader: () => string | null) {
  readToken = reader;
}

async function request<T>(
  method: Method,
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const headers: Record<string, string> = {};
  const token = readToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
      // The gateway is same-origin behind the tunnel; never attach ambient
      // cookies to what is a bearer-token API.
      credentials: "omit",
    });
  } catch (cause) {
    if ((cause as Error)?.name === "AbortError") throw cause;
    throw new ApiError(0, "Gateway unreachable");
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    const detail =
      (payload as { detail?: unknown } | null)?.detail ??
      (payload as { error?: { message?: string } } | null)?.error?.message;
    const retry = Number(response.headers.get("Retry-After")) || undefined;
    throw new ApiError(
      response.status,
      typeof detail === "string" ? detail : `Request failed (${response.status})`,
      retry,
    );
  }
  return payload as T;
}

export const api = {
  /* Unauthenticated. */
  status: () => request<{ initialized: boolean }>("GET", "/control/status"),
  setup: (body: { name: string; email: string; password: string }) =>
    request<Session & { ok: true; secret: string; profile: Profile }>(
      "POST", "/control/setup", body,
    ),
  signIn: (password: string) =>
    request<Session>("POST", "/control/session", { password }),

  /* Session. */
  signOut: () => request<{ ok: boolean }>("DELETE", "/control/session"),

  /* Live state. */
  state: (signal?: AbortSignal) =>
    request<Snapshot>("GET", "/control/state", undefined, signal),
  metrics: (signal?: AbortSignal) =>
    request<Metrics>("GET", "/control/metrics", undefined, signal),
  preflight: () => request<PreflightReport>("GET", "/control/preflight"),
  logs: () => request<{ log: string }>("GET", "/control/logs"),

  /* Routing. */
  load: (id: string) =>
    request<{ ok: boolean; changed: boolean; state: Snapshot }>(
      "POST", `/control/models/${encodeURIComponent(id)}/load`,
    ),
  unload: () =>
    request<{ ok: boolean; state: Snapshot }>("POST", "/control/models/unload"),
  loadImage: (id: string) =>
    request<{ ok: boolean; changed: boolean; state: Snapshot }>(
      "POST", `/control/image-models/${encodeURIComponent(id)}/load`,
    ),
  unloadImage: () =>
    request<{ ok: boolean; state: Snapshot }>("POST", "/control/image-models/unload"),

  /* Profiles. */
  getProfile: (id: string) =>
    request<{ profile: ModelProfile }>(
      "GET", `/control/profiles/${encodeURIComponent(id)}`,
    ),
  saveProfile: (
    id: string,
    body: {
      name: string;
      model_path: string;
      notes: string;
      prompt: { template: "metadata" | "jinja" | "none"; adapter: string; thinking: boolean };
      deployment: {
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
      };
    },
  ) =>
    request<{ ok: boolean; profile: ModelProfile; state: Snapshot }>(
      "PUT", `/control/profiles/${encodeURIComponent(id)}`, body,
    ),
  getImageProfile: (id: string) =>
    request<{ profile: ImageModelProfile }>(
      "GET", `/control/image-profiles/${encodeURIComponent(id)}`,
    ),
  saveImageProfile: (
    id: string,
    body: {
      name: string; model_path: string; text_encoder_path: string;
      vae_path: string; gpu: number; vram_limit_mib: number;
      tiled_vae: number; offload_to_cpu: boolean;
      default_steps: number; default_cfg_scale: number;
      default_sampler: string; default_scheduler: string; notes: string;
    },
  ) =>
    request<{ ok: boolean; profile: ImageModelProfile; state: Snapshot }>(
      "PUT", `/control/image-profiles/${encodeURIComponent(id)}`, body,
    ),

  /* Library. */
  discover: () =>
    request<{ models: DiscoveredModel[]; library: Library }>(
      "GET", "/control/discovery/models",
    ),
  discoverImageFiles: () =>
    request<{ files: DiscoveredImageFile[]; library: Library }>(
      "GET", "/control/discovery/image-files",
    ),
  setSearchRoots: (roots: string) =>
    request<{ ok: boolean; library: Library; models: DiscoveredModel[] }>(
      "POST", "/control/preferences/search-roots", { roots },
    ),
  modelPulls: () =>
    request<ModelPullStatus>("GET", "/control/model-pulls"),
  searchHubModels: (query: string) =>
    request<{ models: HubModel[] }>("GET", `/control/model-browser/search?q=${encodeURIComponent(query)}`),
  hubVariants: (repo: string) =>
    request<HubVariants>("GET", `/control/model-browser/variants?repo=${encodeURIComponent(repo)}`),
  previewModelPull: (body: ModelPullBody) =>
    request<ModelPullPreview>("POST", "/control/model-pulls/preview", body),
  startModelPull: (body: ModelPullBody) =>
    request<{ task: ModelPullTask }>("POST", "/control/model-pulls", body),
  cancelModelPull: (id: string) =>
    request<{ task: ModelPullTask }>(
      "DELETE", `/control/model-pulls/${encodeURIComponent(id)}`,
    ),

  /* Wall display. */
  displayTokens: () =>
    request<{ tokens: ApiKey[] }>("GET", "/control/display/tokens"),
  createDisplayToken: (label: string) =>
    request<{ ok: boolean; token: ApiKey; secret: string }>(
      "POST", "/control/display/tokens", { label },
    ),
  revokeDisplayToken: (id: string) =>
    request<{ ok: boolean; tokens: ApiKey[] }>(
      "DELETE", `/control/display/tokens/${encodeURIComponent(id)}`,
    ),

  /* Credentials. */
  credentials: () =>
    request<{ keys: ApiKey[]; profile: Profile }>("GET", "/control/credentials"),
  createKey: (label: string) =>
    request<{ ok: boolean; key: ApiKey; secret: string }>(
      "POST", "/control/credentials/keys", { label },
    ),
  revokeKey: (id: string) =>
    request<{ ok: boolean; keys: ApiKey[] }>(
      "DELETE", `/control/credentials/keys/${encodeURIComponent(id)}`,
    ),
  changePassword: (current: string, password: string) =>
    request<Session & { ok: true }>(
      "POST", "/control/credentials/password", { current, password },
    ),
  updateProfile: (name: string, email: string) =>
    request<{ ok: boolean; profile: Profile }>(
      "POST", "/control/profile", { name, email },
    ),
};
