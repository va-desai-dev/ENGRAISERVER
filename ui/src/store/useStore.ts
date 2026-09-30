import { create } from "zustand";
import { api, ApiError, bindToken } from "../lib/api";
import type { Metrics, Profile, Snapshot } from "../lib/types";

/* The session deliberately lives in sessionStorage: closing the tab ends it,
   which is the promise the lock screen makes. */
const TOKEN_KEY = "engrai.session";

export type Screen = "loading" | "setup" | "locked" | "ready";

export interface LogEntry {
  id: number;
  at: number;
  message: string;
  tone: "info" | "good" | "bad";
}

interface Store {
  screen: Screen;
  token: string | null;
  snapshot: Snapshot | null;
  metrics: Metrics | null;
  profile: Profile | null;
  /* Set when the gateway itself stops answering, so the shell can say so
     instead of silently showing stale numbers. */
  offline: boolean;
  busyModel: string | null;
  busyImage: string | null;
  log: LogEntry[];

  note(message: string, tone?: LogEntry["tone"]): void;
  boot(): Promise<void>;
  signIn(password: string): Promise<void>;
  setup(name: string, email: string, password: string): Promise<string>;
  signOut(revoke?: boolean): Promise<void>;
  adoptToken(token: string): void;
  refresh(): Promise<void>;
  load(id: string): Promise<void>;
  unload(): Promise<void>;
  loadImage(id: string): Promise<void>;
  unloadImage(): Promise<void>;
  applySnapshot(snapshot: Snapshot): void;
}

let logSeq = 0;

/* Poll responses are plain JSON. Retain equal values so fresh response
   objects do not invalidate subscribers when nothing actually changed.
   Key-order differences can cause an extra update, never a missed change. */
function retainEqual<T>(previous: T, incoming: T): T {
  return JSON.stringify(previous) === JSON.stringify(incoming) ? previous : incoming;
}

function liveUpdate(state: Store, incoming: Snapshot, readings = state.metrics) {
  const snapshot = retainEqual(state.snapshot, incoming);
  const metrics = retainEqual(state.metrics, readings);
  const profile = retainEqual(state.profile, incoming.profile ?? state.profile);
  if (snapshot === state.snapshot && metrics === state.metrics &&
      profile === state.profile && !state.offline) return state;
  return { snapshot, metrics, profile, offline: false };
}

export const useStore = create<Store>((set, get) => ({
  screen: "loading",
  token: sessionStorage.getItem(TOKEN_KEY),
  snapshot: null,
  metrics: null,
  profile: null,
  offline: false,
  busyModel: null,
  busyImage: null,
  log: [],

  note(message, tone = "info") {
    set((s) => ({
      log: [{ id: ++logSeq, at: Date.now(), message, tone }, ...s.log].slice(0, 40),
    }));
  },

  applySnapshot(snapshot) {
    set((state) => liveUpdate(state, snapshot));
  },

  adoptToken(token) {
    sessionStorage.setItem(TOKEN_KEY, token);
    set({ token });
  },

  async boot() {
    const { token } = get();
    if (token) {
      try {
        get().applySnapshot(await api.state());
        set({ screen: "ready" });
        get().note("Session resumed", "good");
        return;
      } catch (error) {
        // A stale token from a previous run is expected, not an error worth
        // showing; fall through to whichever entry screen applies.
        if (!(error instanceof ApiError) || !error.isAuthFailure) {
          set({ offline: error instanceof ApiError && error.isOffline });
        }
        sessionStorage.removeItem(TOKEN_KEY);
        set({ token: null });
      }
    }
    try {
      const { initialized } = await api.status();
      set({ screen: initialized ? "locked" : "setup" });
    } catch {
      set({ screen: "locked", offline: true });
    }
  },

  async signIn(password) {
    const session = await api.signIn(password);
    get().adoptToken(session.token);
    get().applySnapshot(await api.state());
    set({ screen: "ready" });
    get().note("Control plane unlocked", "good");
  },

  async setup(name, email, password) {
    const result = await api.setup({ name, email, password });
    get().adoptToken(result.token);
    set({ profile: result.profile });
    get().applySnapshot(await api.state());
    set({ screen: "ready" });
    get().note("Account created", "good");
    return result.secret;
  },

  async signOut(revoke = true) {
    if (revoke) {
      // A dead session is already signed out; never block the UI on this.
      try { await api.signOut(); } catch { /* ignore */ }
    }
    sessionStorage.removeItem(TOKEN_KEY);
    set({
      token: null, screen: "locked", snapshot: null,
      metrics: null, busyModel: null, busyImage: null, log: [],
    });
  },

  async refresh() {
    if (!get().token) return;
    try {
      const [snapshot, metrics] = await Promise.all([api.state(), api.metrics()]);
      set((state) => liveUpdate(state, snapshot, metrics));
    } catch (error) {
      if (error instanceof ApiError && error.isAuthFailure) {
        await get().signOut(false);
        return;
      }
      set({ offline: true });
    }
  },

  async load(id) {
    set({ busyModel: id });
    get().note(`Loading ${id}`);
    try {
      const result = await api.load(id);
      get().applySnapshot(result.state);
      get().note(result.changed ? `${id} is live` : `${id} was already live`, "good");
    } catch (error) {
      get().note(error instanceof Error ? error.message : `Could not load ${id}`, "bad");
      throw error;
    } finally {
      set({ busyModel: null });
    }
  },

  async unload() {
    set({ busyModel: "__unload__" });
    get().note("Unloading active model");
    try {
      const result = await api.unload();
      get().applySnapshot(result.state);
      get().note("Model unloaded", "good");
    } catch (error) {
      get().note(error instanceof Error ? error.message : "Could not unload", "bad");
      throw error;
    } finally {
      set({ busyModel: null });
    }
  },

  async loadImage(id) {
    set({ busyImage: id });
    get().note(`Loading image model ${id}`);
    try {
      const result = await api.loadImage(id);
      get().applySnapshot(result.state);
      get().note(result.changed ? `${id} is ready for images` : `${id} was already ready`, "good");
    } catch (error) {
      get().note(error instanceof Error ? error.message : `Could not load ${id}`, "bad");
      throw error;
    } finally {
      set({ busyImage: null });
    }
  },

  async unloadImage() {
    set({ busyImage: "__unload__" });
    get().note("Unloading image model");
    try {
      const result = await api.unloadImage();
      get().applySnapshot(result.state);
      get().note("Image model unloaded", "good");
    } catch (error) {
      get().note(error instanceof Error ? error.message : "Could not unload image model", "bad");
      throw error;
    } finally {
      set({ busyImage: null });
    }
  },
}));

bindToken(() => useStore.getState().token);
