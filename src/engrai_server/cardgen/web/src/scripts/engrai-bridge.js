// ENGRAI bridge — replaces CardGen's own accounts and provider settings.
//
// Card Studio runs inside the gateway, so the console session *is* the login
// and the console's loaded models *are* the providers. This file owns both:
// it signs requests with the console session, sends the visitor to the
// console's lock screen when there is none, and tracks which text and image
// models are loaded so the rest of the app can ask `engrai.ready("image")`.
(function () {
  // Must match ui/src/store/useStore.ts. The console keeps its session in
  // sessionStorage (closing the tab ends it), so Card Studio opens in the
  // same tab as the console rather than a new one.
  const SESSION_KEY = "engrai.session";
  const RETURN_KEY = "engrai.return";
  const POLL_MS = 5000;

  function getToken() {
    try { return sessionStorage.getItem(SESSION_KEY); } catch { return null; }
  }

  function clearToken() {
    try { sessionStorage.removeItem(SESSION_KEY); } catch { /* ignore */ }
  }

  // Back to the console's lock screen, which returns here after sign-in.
  function signIn() {
    try { sessionStorage.setItem(RETURN_KEY, location.pathname + location.search + location.hash); } catch { /* ignore */ }
    location.replace("/");
  }

  // CardGen fetched remote images through its server to dodge CORS. ENGRAI
  // makes no outbound requests on a page's behalf, and everything this app
  // produces is a data: URL or same-origin, so those reads resolve here.
  function resolveProxyImage(url) {
    const parsed = new URL(url, location.origin);
    if (parsed.pathname !== "/cardgen/api/proxy-image") return null;
    const target = parsed.searchParams.get("url") || "";
    return fetch(target, { credentials: "omit" });
  }

  window.authFetch = async function authFetch(url, options = {}) {
    const proxied = typeof url === "string" ? resolveProxyImage(url) : null;
    if (proxied) return proxied;

    const token = getToken();
    const headers = { ...(options.headers || {}) };
    if (token) headers.Authorization = `Bearer ${token}`;
    const res = await fetch(url, { ...options, headers, credentials: "omit" });

    // Only the gateway's own session failure means "sign in again"; it marks
    // those so an engine error can never bounce the user to the lock screen.
    if (res.status === 401 && res.headers.get("X-Session-Expired") === "1" && getToken() === token) {
      clearToken();
      signIn();
    }
    return res;
  };

  // ── Engine state ──────────────────────────────────────────────────────
  const listeners = new Set();
  let pollTimer = null;

  const engrai = {
    status: null,

    model(kind) {
      return this.status?.[kind]?.model || null;
    },

    ready(kind) {
      return !!this.model(kind);
    },

    // A sentence for when something needs a model that is not loaded.
    missing(kind) {
      const lane = this.status?.[kind];
      const noun = kind === "image" ? "image" : "text";
      if (lane?.loading) return `The ${noun} model "${lane.loading.name}" is still loading in the ENGRAI console.`;
      return `No ${noun} model is loaded. Load one from the ENGRAI console.`;
    },

    onChange(fn) {
      listeners.add(fn);
      if (this.status) fn(this.status);
      return () => listeners.delete(fn);
    },

    async refresh() {
      const res = await window.authFetch("/cardgen/api/engine");
      if (!res.ok) throw new Error(`Engine status failed (${res.status})`);
      const next = await res.json();
      const changed = JSON.stringify(next) !== JSON.stringify(this.status);
      this.status = next;
      if (changed) listeners.forEach((fn) => { try { fn(next); } catch (e) { console.error(e); } });
      return next;
    },
  };
  window.engrai = engrai;

  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      if (document.visibilityState === "visible") {
        try { await engrai.refresh(); } catch { /* the next tick retries */ }
      }
      schedulePoll();
    }, POLL_MS);
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") engrai.refresh().catch(() => {});
  });

  // ── Header chips ──────────────────────────────────────────────────────
  function paintChip(id, lane) {
    const chip = document.getElementById(id);
    if (!chip) return;
    const name = chip.querySelector(".engine-name");
    let state = "off";
    let label = "Not loaded";
    if (lane?.model) {
      state = lane.busy ? "busy" : "on";
      label = lane.model.name;
    } else if (lane?.loading) {
      state = "busy";
      label = `Loading ${lane.loading.name}…`;
    } else if (lane?.phase === "error") {
      state = "error";
      label = "Failed to load";
    }
    chip.dataset.state = state;
    if (name) name.textContent = label;
    chip.title = lane?.model ? `${lane.model.name} (${lane.model.id})` : label;
  }

  engrai.onChange((status) => {
    paintChip("engine-chip-text", status.text);
    paintChip("engine-chip-image", status.image);
  });

  // CardGen's bootstrap awaits this before starting the app.
  async function initAuth() {
    if (!getToken()) {
      signIn();
      return false;
    }
    try {
      await engrai.refresh();
    } catch (error) {
      if (!getToken()) return false; // authFetch is already redirecting
      console.warn("[engrai] engine status unavailable:", error);
    }
    // Library <img> tags cannot send the Authorization header, so the gateway
    // trades the session for an HttpOnly, read-only cookie scoped to this
    // app's API. "same-origin" rather than authFetch's "omit": an omitted
    // credentials mode discards the Set-Cookie.
    try {
      await fetch("/cardgen/api/session/image-cookie", {
        method: "POST",
        headers: { Authorization: `Bearer ${getToken()}` },
        credentials: "same-origin",
      });
    } catch (error) {
      console.warn("[engrai] library images may not load:", error);
    }
    schedulePoll();
    return true;
  }

  window.cardgenAuth = { getToken, clearToken, initAuth };
})();
