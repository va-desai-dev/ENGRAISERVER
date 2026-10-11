// Configuration file for SillyTavern Character Generator
const LOCAL_STORAGE_KEY = "charGeneratorConfig";

// ── Image-model capabilities ────────────────────────────────────────────────
// Which of the image jobs a given model can actually do. Most providers don't
// report this (nano-gpt's /image-models reports input types, which is used for
// the defaults, but nothing about Combine), so the app used to guess from the name alone (_looksEditCapable in
// image-playground.js), which is unreliable: a plain text-to-image model
// silently ignores a source image rather than erroring, so a wrong guess
// costs a credit and returns something with no resemblance to the original.
// The user now marks each model in Settings → Image API, and every model
// dropdown filters to the models marked for that job. The name heuristic
// survives only as the *default* for a model that has never been marked, so an
// existing config keeps working without anyone ticking 40 boxes first.
const IMAGE_MODEL_CAPABILITIES = ["generate", "edit", "combine", "upscale"];

const IMAGE_CAPABILITY_META = {
  generate: { icon: "", label: "Generate", title: "Text-to-image — creates a new image from a prompt alone" },
  edit: { icon: "", label: "Edit", title: "Image-to-image — edits one source image from an instruction" },
  combine: { icon: "", label: "Combine", title: "Accepts two or more reference images in a single call" },
  upscale: { icon: "", label: "Enhance", title: "Good at upscaling / cleaning up an image without changing its content" },
};

// Same markers _looksEditCapable used. Kept deliberately conservative: it only
// decides the starting state of the checkboxes, and being wrong is now a tick
// away from fixed rather than a mystery about why an edit did nothing.
const IMAGE_EDIT_NAME_MARKERS = ["image-to-image", "img2img", "-edit", "edit-", "/edit", "kontext", "inpaint", "instruct"];

// There is no equivalent naming convention for "accepts multiple reference
// images" — qwen-image-3-pro, reve/2.1/remix and xai/…/edit all support it with
// nothing in common in their names — so Combine defaults to off for everything
// and is purely a user decision.
//
// `apiModel` is the model's entry from the provider's model list, when there is
// one. nano-gpt's /image-models says which inputs each model takes, which beats
// guessing from the name: an edit model takes text + image, a text-to-image
// model text only, and a background remover or upscaler an image only.
function guessImageModelCapabilities(modelId, apiModel) {
  const id = (modelId || "").toLowerCase();
  // The proxy's local ComfyUI model is known exactly rather than guessed: one
  // Qwen-Image 2.1 workflow does both text-to-image and single-image edits. Not
  // Enhance: the proxy pins edit output to COMFYUI_EDIT_MEGAPIXELS, so it can't
  // return the larger image an upscale asks for.
  if (id.startsWith("local/qwen-image")) return { generate: true, edit: true, combine: false, upscale: false };
  const looksEdit = IMAGE_EDIT_NAME_MARKERS.some(marker => id.includes(marker));
  // Enhance is an image-to-image job, so anything edit-shaped is a fair
  // starting guess — how *well* a given model upscales is something only the
  // user can judge, which is exactly why it is a separate tickbox.
  const looksUpscale = looksEdit || ["upscal", "enhance", "restor", "super-res", "superres"].some(m => id.includes(m));
  const byName = { generate: !looksEdit, edit: looksEdit, combine: false, upscale: looksUpscale };

  const inputs = apiModel?.architecture?.input_modalities;
  const apiCaps = apiModel?.capabilities;
  if (!Array.isArray(inputs) && !apiCaps) return byName;
  const takesText = Array.isArray(inputs) ? inputs.includes("text") : true;
  const takesImage = !!apiCaps?.image_to_image || (Array.isArray(inputs) && inputs.includes("image"));
  return {
    // Plenty of text+image models (seedream, hidream) also generate from text
    // alone, so an image input only rules out Generate when the name says edit.
    generate: takesText && (!takesImage || !looksEdit),
    edit: takesText && takesImage,
    combine: false,
    upscale: takesImage && looksUpscale,
  };
}

// The stored marks for one model, falling back to the name guess. Model ids
// contain dots and slashes (`reve/2.1/remix`), so the map is read whole and
// indexed — never via config.get("api.image.modelCapabilities." + id), which
// would split the id on its own dots.
function getImageModelCapabilities(modelId, configInstance, apiModel) {
  const cfg = configInstance || window.config;
  const stored = (cfg?.get("api.image.modelCapabilities") || {})[modelId];
  const guess = guessImageModelCapabilities(modelId, apiModel);
  if (!stored) return guess;
  // Merge rather than replace: a capability added to the app after this model
  // was marked has no stored value and should still get its guessed default.
  return IMAGE_MODEL_CAPABILITIES.reduce((out, cap) => {
    out[cap] = typeof stored[cap] === "boolean" ? stored[cap] : guess[cap];
    return out;
  }, {});
}

// Every model marked as able to do `capability`, in the order they appear in
// the user's model list.
function getImageModelsWithCapability(capability, configInstance) {
  const cfg = configInstance || window.config;
  const models = cfg?.get("api.image.models") || [];
  return models.filter(model => getImageModelCapabilities(model, cfg)[capability]);
}

// The model to reach for when a job needs one and there's no dropdown to ask —
// Roleplay's "visualize this scene" and the auto-defaults applied to a brand-new
// character all just took models[0] before, which silently picked an
// image-to-image model for a text-to-image job if that happened to be first in
// the list. Falls back to the first model overall so an unmarked config still
// generates something rather than nothing.
function firstImageModelWithCapability(capability, configInstance) {
  const cfg = configInstance || window.config;
  const matching = getImageModelsWithCapability(capability, cfg);
  if (matching.length > 0) return matching[0];
  return (cfg?.get("api.image.models") || [])[0] || "";
}

// The "nothing is marked for this job" note shown under a filtered dropdown.
// Shared so the Character Generator, the Playground and anything added later
// explain an unfiltered list the same way.
function renderImageModelHint(hintEl, capability, result, configInstance) {
  if (!hintEl) return;
  const cfg = configInstance || window.config;
  const meta = IMAGE_CAPABILITY_META[capability];
  const total = (cfg?.get("api.image.models") || []).length;

  if (total === 0) {
    hintEl.style.display = "block";
    hintEl.textContent = window.engrai?.missing("image") || "No image model is loaded.";
  } else if (!result.filtered) {
    hintEl.style.display = "block";
    hintEl.textContent = `No model is marked ${meta.label} yet, so all of them are listed. Load an image model in the ENGRAI console.`;
  } else {
    hintEl.style.display = "none";
  }
}

// ── Per-model cost and speed ────────────────────────────────────────────────
// How long a model takes varies enormously (seconds to minutes) and so does
// what it costs, but neither is discoverable from the provider's model list.
// Speed is therefore measured from actual runs — a rolling mean per model,
// recorded by api-image.js after every successful call — and cost is a short
// note the user types once in Settings, since only they can see their billing.
// Both surface in the model dropdowns, where the choice is actually made.

function recordImageModelRun(modelId, elapsedMs, configInstance) {
  const cfg = configInstance || window.config;
  if (!cfg || !modelId || !Number.isFinite(elapsedMs) || elapsedMs <= 0) return;

  const stats = { ...(cfg.get("api.image.modelStats") || {}) };
  const previous = stats[modelId] || { runs: 0, avgMs: 0 };
  const runs = previous.runs + 1;
  // Mean over the last few runs rather than all time: a model's speed changes
  // with provider load, and a number from fifty runs ago is not what the next
  // call will cost in time. Weighting the newest run at 1/min(runs, 10) settles
  // quickly and then tracks recent behaviour.
  const weight = 1 / Math.min(runs, 10);
  stats[modelId] = {
    runs,
    avgMs: Math.round(previous.avgMs + (elapsedMs - previous.avgMs) * weight),
    lastMs: Math.round(elapsedMs),
  };
  cfg.set("api.image.modelStats", stats);
}

// "~18s · $0.04", or "" when nothing is known yet. Deliberately terse — it is
// appended to an <option> label, which has very little room on a phone.
function describeImageModelCostSpeed(modelId, configInstance) {
  const cfg = configInstance || window.config;
  const parts = [];

  const stat = (cfg?.get("api.image.modelStats") || {})[modelId];
  if (stat?.avgMs > 0) {
    const seconds = stat.avgMs / 1000;
    parts.push(seconds >= 60 ? `~${Math.round(seconds / 6) / 10}min` : `~${Math.round(seconds)}s`);
  }

  const costNote = (cfg?.get("api.image.modelSettings") || {})[modelId]?.costNote;
  if (costNote) parts.push(String(costNote).trim().slice(0, 20));

  return parts.join(" · ");
}

// Fills a <select> with just the models marked for one job. Shared by the
// Playground's three model dropdowns and Settings' "Image Edit Model", so the
// same list and the same explanation of an empty list appear everywhere.
//
// Falls back to listing every model (each flagged) rather than showing an empty
// dropdown when nothing is marked: an unmarked config must never leave a tool
// unusable, and Combine in particular has no name heuristic to seed itself from
// so it starts out with nothing marked at all.
// `configKey` is both read (to pre-select) and written back when the stored
// model isn't in the list at all and the dropdown has to fall back to another.
// The write-back matters because not every consumer reads the dropdown: the
// Character Generator's Edit Image and the Character Gallery both read
// api.image.editModel straight from config, so a dropdown that silently showed
// one model while config held another would edit with the wrong one.
function populateImageModelSelect(selectEl, capability, configKey, configInstance, options = {}) {
  if (!selectEl) return { count: 0, filtered: false };

  const cfg = configInstance || window.config;
  const currentValue = cfg?.get(configKey) || "";
  const allModels = cfg?.get("api.image.models") || [];
  const meta = IMAGE_CAPABILITY_META[capability];

  if (allModels.length === 0) {
    // `emptyOptionLabel` keeps an empty value meaningful for the Character
    // Generator, where "" means "let the image API pick its own default" — the
    // Playground's dropdowns have no such notion and just explain themselves.
    if (options.emptyOptionLabel) {
      selectEl.innerHTML = `<option value="">${escapeHtml(options.emptyOptionLabel)}</option>`;
      if (currentValue) {
        selectEl.innerHTML += `<option value="${escapeHtml(currentValue)}" selected>${escapeHtml(currentValue)}</option>`;
      }
    } else {
      selectEl.innerHTML = `<option value="${escapeHtml(currentValue)}">${escapeHtml(currentValue || "No image model is loaded in the ENGRAI console")}</option>`;
    }
    return { count: 0, filtered: false };
  }

  const matching = getImageModelsWithCapability(capability, cfg);
  const filtered = matching.length > 0;
  let listed = filtered ? matching : allModels;

  // A model already saved for this job stays selectable even if it isn't
  // marked for it — changing the user's stored model out from under them
  // because of a checkbox default would be worse than showing it with a flag.
  if (currentValue && !listed.includes(currentValue)) listed = [currentValue, ...listed];

  selectEl.innerHTML = listed
    .map(model => {
      const marked = getImageModelCapabilities(model, cfg)[capability];
      const costSpeed = describeImageModelCostSpeed(model, cfg);
      let label = marked ? model : `${model} not marked as ${meta.label}`;
      if (costSpeed) label += ` · ${costSpeed}`;
      return `<option value="${escapeHtml(model)}" ${model === currentValue ? "selected" : ""}>${escapeHtml(label)}</option>`;
    })
    .join("");

  if (!listed.includes(currentValue)) {
    selectEl.value = listed[0];
    if (cfg && selectEl.value) cfg.set(configKey, selectEl.value);
  }
  return { count: matching.length, filtered };
}

class Config {
  constructor() {
    this.config = this.getDefaultConfig();
    this.debugMode = false; // Toggle for verbose logging
    this.isLoaded = false;
    this.loadPromise = this.loadConfig().catch((e) => {
      console.error(e);
      this.isLoaded = true;
    });
  }

  getDefaultConfig() {
    return {
      api: {
        text: {
          model: "",
          visionModel: "",
          // Long on purpose: a local model may be loading or queued behind
          // another generation before it starts on this one.
          timeout: 600000,
        },
        image: {
          model: "",
          models: [],
          size: "",
          style: "",
          aspectRatio: "",
          modelSettings: {},
          modelCapabilities: {},
          upscaleModel: "",
          // Reusable Playground prompts the user has saved:
          // [{ id, tool, name, text }]. The built-in ones are code, not config.
          promptPresets: [],
          // modelId -> { runs, avgMs, lastMs }, measured from real calls.
          modelStats: {},
          timeout: 600000,
        },
      },
      app: {
        maxRetries: 3,
        retryDelay: 1000,
        debugMode: false,
        enableImageGeneration: true,
        creator: "",
        // Cultural default for generated characters — names, nationality and
        // setting. "western" (the default) is Anglo-led with continental
        // Western Europe in the mix; "anglo" narrows to the British Isles,
        // North America and Australasia; "global" restores the old behaviour of
        // drawing from every culture. A concept that names a place, culture,
        // era or real person always overrides this — see CultureSteer in
        // api-character.js.
        characterCulture: "western",
      },
    };
  }

  // The console decides which models exist; these fields mirror it so the
  // rest of CardGen, which reads model names from config, keeps working.
  applyEngine(status) {
    const text = status?.text?.model?.id || "";
    const image = status?.image?.model?.id || "";
    Object.assign(this.config.api.text, { model: text, visionModel: text });
    Object.assign(this.config.api.image, {
      model: image,
      models: image ? [image] : [],
      editModel: image,
      generateModel: image,
      combineModel: image,
      upscaleModel: image,
      // stable-diffusion.cpp runs every job: img2img for Edit and Enhance,
      // reference images for Combine. How well depends on the architecture.
      modelCapabilities: image ? { [image]: { generate: true, edit: true, combine: true, upscale: true } } : {},
    });
  }

  engineReady(kind) {
    return !!window.engrai?.ready(kind);
  }

  getDebugMode() {
    return this.debugMode || this.config.app.debugMode || false;
  }

  log(...args) {
    if (this.getDebugMode()) {
      console.log(...args);
    }
  }

  async loadConfig() {
    // Load from local storage first so it's available immediately
    const savedConfig = localStorage.getItem(LOCAL_STORAGE_KEY);
    if (savedConfig) {
      try {
        const saved = JSON.parse(savedConfig);
        this.config = this.deepMerge(this.config, saved);
        this.logRedacted("Loaded config from storage:", saved);
      } catch (error) {
        console.warn("Failed to load saved config:", error);
      }
    }

    // Then sync/override with server config if available
    try {
      // Skip if no auth token yet — authFetch would get a 401 and
      // could disrupt the login flow. Config is reloaded after login.
      if (!(window.cardgenAuth && window.cardgenAuth.getToken())) {
        this.debugMode = this.config.app.debugMode || false;
        this.applyEngine(window.engrai?.status);
        this.isLoaded = true;
        return;
      }
      const res = await (window.authFetch || fetch)("/cardgen/api/config");
      if (res.ok) {
        const serverConfig = await res.json();
        if (Object.keys(serverConfig).length > 0) {
          this.config = this.deepMerge(this.config, serverConfig);
          this.logRedacted("Loaded config from server:", serverConfig);
        }
      }
    } catch (error) {
      console.warn("Failed to load config from server:", error);
    }

    // Load debug mode setting
    this.debugMode = this.config.app.debugMode || false;

    this.applyEngine(window.engrai?.status);
    this.logRedacted("Final config:", this.config);
    this.isLoaded = true;
  }

  loadFromForm() {
    const value = (id) => document.getElementById(id)?.value;
    const imageStyle = value("image-style");
    const imageMood = value("image-mood");
    const customImageStyle = value("custom-image-style-input");
    const imageAspectRatio = value("image-aspect-ratio");
    if (imageStyle !== undefined) this.config.api.image.style = imageStyle;
    if (imageMood !== undefined) this.config.api.image.mood = imageMood;
    if (customImageStyle !== undefined) this.config.api.image.customStyleText = customImageStyle;
    if (imageAspectRatio !== undefined) this.config.api.image.aspectRatio = imageAspectRatio;

    const enableImageGeneration = document.getElementById("enable-image-generation")?.checked;
    if (enableImageGeneration !== undefined) this.config.app.enableImageGeneration = enableImageGeneration;

    const characterCulture = value("character-culture");
    if (characterCulture) this.config.app.characterCulture = characterCulture;

    const creator = value("creator-name")?.trim();
    if (creator !== undefined) this.config.app.creator = creator;
  }

  get(path) {
    return path.split(".").reduce((obj, key) => obj && obj[key], this.config);
  }

  set(path, value) {
    const keys = path.split(".");
    const lastKey = keys.pop();
    const target = keys.reduce((obj, key) => {
      if (!obj[key]) obj[key] = {};
      return obj[key];
    }, this.config);
    target[lastKey] = value;
    this.saveConfig();
  }

  // Resolves true once the server has stored the config. Most callers fire and
  // forget; a restore awaits it, because it reloads the page straight after and
  // the server copy wins over localStorage on load.
  saveConfig() {
    if (!this.isLoaded) {
      console.warn("Attempted to save config before it was fully loaded. Ignoring save to prevent data loss.");
      return Promise.resolve(false);
    }

    const stored = this.persistable();
    localStorage.setItem(LOCAL_STORAGE_KEY, JSON.stringify(stored));

    // Also save to server for persistence across devices/sessions
    return (window.authFetch || fetch)("/cardgen/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(stored)
    })
      .then(res => res.ok)
      .catch(e => {
        console.error("Failed to sync config to server", e);
        return false;
      });
  }

  // Everything but what applyEngine() mirrors from the console, plus the
  // provider fields older CardGen configs carried.
  persistable() {
    const copy = JSON.parse(JSON.stringify(this.config));
    for (const key of ["baseUrl", "apiKey", "model", "visionModel"]) delete copy.api?.text?.[key];
    for (const key of ["baseUrl", "apiKey", "model", "models", "editModel", "generateModel",
      "combineModel", "upscaleModel", "modelCapabilities", "localForge"]) delete copy.api?.image?.[key];
    delete copy.st;
    return copy;
  }

  saveToForm() {
    const imageStyle = document.getElementById("image-style");
    const imageMood = document.getElementById("image-mood");
    const customImageStyle = document.getElementById("custom-image-style-input");
    const customStyleContainer = document.getElementById("custom-style-container");
    const imageAspectRatio = document.getElementById("image-aspect-ratio");
    if (imageStyle) {
      imageStyle.value = this.config.api.image.style || "";
      if (customStyleContainer) customStyleContainer.style.display = imageStyle.value === "custom" ? "block" : "none";
    }
    if (imageMood) imageMood.value = this.config.api.image.mood || "";
    if (customImageStyle) customImageStyle.value = this.config.api.image.customStyleText || "";
    if (imageAspectRatio) imageAspectRatio.value = this.config.api.image.aspectRatio || "";

    const creatorName = document.getElementById("creator-name");
    if (creatorName) creatorName.value = this.config.app.creator || "";

    if (window.app && typeof window.app.updateActiveModelsDropdown === "function") {
      window.app.updateActiveModelsDropdown();
    }

    const enableImageGeneration = document.getElementById("enable-image-generation");
    if (enableImageGeneration) enableImageGeneration.checked = this.config.app.enableImageGeneration !== false;

    const characterCultureEl = document.getElementById("character-culture");
    if (characterCultureEl) characterCultureEl.value = this.config.app.characterCulture || "western";
  }

  deepMerge(target, source) {
    const output = { ...target };
    if (this.isObject(target) && this.isObject(source)) {
      Object.keys(source).forEach((key) => {
        if (this.isObject(source[key])) {
          if (!(key in target)) {
            Object.assign(output, { [key]: source[key] });
          } else {
            output[key] = this.deepMerge(target[key], source[key]);
          }
        } else {
          Object.assign(output, { [key]: source[key] });
        }
      });
    }
    return output;
  }

  isObject(item) {
    return item && typeof item === "object" && !Array.isArray(item);
  }

  async waitForConfig() {
    if (this.loadPromise) {
      await this.loadPromise;
    }
    return this.config;
  }

  validateConfig() {
    return this.engineReady("text") ? [] : [window.engrai?.missing("text") || "No text model is loaded"];
  }

  clearStoredConfig() {
    localStorage.removeItem(LOCAL_STORAGE_KEY);
    if (window.cardgenAuth && window.cardgenAuth.getToken()) {
      (window.authFetch || fetch)("/cardgen/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({})
      }).catch(e => console.error("Failed to clear config on server", e));
    }
  }

  redactSensitiveData(data) {
    if (Array.isArray(data)) {
      return data.map((item) => this.redactSensitiveData(item));
    }

    if (!this.isObject(data)) {
      return data;
    }

    const redacted = {};
    Object.keys(data).forEach((key) => {
      if (key.toLowerCase().includes("apikey")) {
        redacted[key] = data[key] ? "[REDACTED]" : "";
      } else {
        redacted[key] = this.redactSensitiveData(data[key]);
      }
    });
    return redacted;
  }

  logRedacted(message, data) {
    if (this.getDebugMode()) {
      console.log(message, this.redactSensitiveData(data));
    }
  }
}

// Export singleton instance
window.config = new Config();
