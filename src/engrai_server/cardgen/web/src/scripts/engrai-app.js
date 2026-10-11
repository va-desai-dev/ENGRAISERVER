// ENGRAI integration for the CardGen app class.
//
// Loaded after every CardGen module, so these definitions replace the
// provider-era ones: status reads the console's engines instead of spending a
// generation on a health check, and there is no theme to choose.
Object.assign(CharacterGeneratorApp.prototype, {
  // ENGRAI has one look, and it is dark.
  initTheme() {
    document.documentElement.setAttribute("data-theme", "dark");
  },

  async checkAPIStatus() {
    try {
      // Painted here as well as on change: an unchanged status fires no event.
      this._paintEngineStatus(await window.engrai.refresh());
    } catch (error) {
      this._paintEngineStatus(null, error.message);
    }
  },

  _paintEngineStatus(status, failure) {
    const element = document.getElementById("api-status");
    const indicator = element?.querySelector(".status-indicator");
    const text = element?.querySelector(".status-text");
    const describe = (lane, noun) =>
      lane?.model ? lane.model.name : lane?.loading ? `${noun} loading…` : `no ${noun.toLowerCase()} model`;

    if (indicator && text) {
      if (failure) {
        indicator.className = "status-indicator status-offline";
        text.textContent = `Engines unavailable: ${failure}`;
      } else {
        const ready = !!status?.text?.model;
        indicator.className = `status-indicator ${ready ? "status-online" : "status-offline"}`;
        text.textContent = `${describe(status?.text, "Text")} · ${describe(status?.image, "Image")}`;
      }
    }

    const textItem = document.getElementById("settings-engine-text");
    const imageItem = document.getElementById("settings-engine-image");
    if (textItem) textItem.textContent = status?.text?.model?.name || window.engrai.missing("text");
    if (imageItem) imageItem.textContent = status?.image?.model?.name || window.engrai.missing("image");
  },

  async handleTestConnection() {
    await this.checkAPIStatus();
    const ready = window.engrai.ready("text");
    this.showNotification(
      ready ? "Engines refreshed" : window.engrai.missing("text"),
      ready ? "success" : "warning",
    );
  },

  handleAPIConfig() {
    document.getElementById("api-settings-btn")?.click();
  },

  handleClearConfig() {
    if (confirm("Reset Card Studio settings? Your library is not affected.")) {
      this.config.clearStoredConfig();
      this.showNotification("Settings reset. Reloading…", "success");
      setTimeout(() => location.reload(), 600);
    }
  },
});

// Keep config's mirrored model fields, every model dropdown and the status
// surfaces in step with whatever the console loads or unloads.
window.engrai.onChange((status) => {
  window.config.applyEngine(status);
  const app = window.app;
  if (!app) return;
  app.refreshImageModelDropdowns?.();
  app._paintEngineStatus(status);
});
