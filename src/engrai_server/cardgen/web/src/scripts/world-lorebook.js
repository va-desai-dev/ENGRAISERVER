// World lorebooks — the Lorebooks tab.
//
// A lorebook that stands on its own: a setting described once, and entries
// any character can use. A character's own lorebook still lives on its card
// (lorebook-handler.js, "Manage Lorebook"); this tab never touches it except
// when asked to copy entries in with "Add to current character".
//
// Entries here are kept in a neutral shape and converted at the edges:
//   { id, title, keys[], secondaryKeys[], content, constant, enabled }
// Export writes SillyTavern's native World Info file; import reads that, a
// V2/V3 character_book, or a whole card (.json or .png).

(function () {
  const DRAFT_KEY = "engrai.cardstudio.worldbook";
  const SUGGEST_LIMIT = 16;

  const splitKeys = (value) =>
    String(value || "").split(",").map((k) => k.trim()).filter(Boolean);

  const newId = () => `wb_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;

  const entryTitle = (entry) => entry.title || entry.keys[0] || "Untitled";

  function blankBook() {
    return { id: null, name: "", setting: "", entries: [] };
  }

  // ── Prompts ─────────────────────────────────────────────────────────────
  // Written for world material, not a character: no {{char}} substitution,
  // and every entry must make sense alone, because it is only inserted when
  // one of its keys comes up in conversation.

  const WRITING_RULES = `An entry is inserted into the story model's context only when one of its keys is mentioned, so it must stand on its own.
- State plain facts in the third person and present tense.
- Open with one sentence saying what the subject is, then what a storyteller needs: its nature or appearance, who is involved, how it works or is used, and the tensions or hooks it offers.
- 80 to 200 words. No headings. Use a short list only if it genuinely reads better.
- Stay consistent with the setting. Invent specifics where the setting is silent, but never contradict it.
- Do not address the reader, do not write dialogue or a scene, and do not use {{char}} or {{user}}.
- Where it is natural, name related topics by their titles so their entries can trigger too.
- Output only the entry text.`;

  function worldContext(book, otherTitles) {
    const others = otherTitles.length ? `\nOther entries in this lorebook: ${otherTitles.join("; ")}` : "";
    return `World: ${book.name || "Untitled"}\nSetting:\n${book.setting || "(no setting given)"}${others}`;
  }

  Object.assign(APIHandler.prototype, {
    async suggestWorldTopics(book, count = 8) {
      count = Math.max(1, Math.min(SUGGEST_LIMIT, Math.floor(Number(count)) || 8));
      const existing = book.entries.map(entryTitle);
      const data = {
        model: this.config.get("api.text.model"),
        messages: [
          {
            role: "system",
            content: `You plan lorebooks (World Info) for AI roleplay. Given a setting, choose the topics most worth an entry: places, factions, notable people, creatures, objects, customs, history, and systems such as magic, technology or law. Prefer what a story set there would actually mention.

For each topic give a short title and 2 to 4 trigger keys: the name, plus the variants and plain words people would use for it in conversation. Never use generic words on their own, such as "the", "city" or "magic".

Return only JSON in this shape, with no other text:
{"topics": [{"title": "The Saltmarket", "keys": ["Saltmarket", "salt market", "fish market"]}]}`,
          },
          {
            role: "user",
            content: `${worldContext(book, [])}${existing.length ? `\n\nAlready covered (do not repeat): ${existing.join("; ")}` : ""}\n\nSuggest ${count} topics.`,
          },
        ],
        temperature: 0.4,
        max_tokens: 1024 + count * 160,
        stream: false,
        chat_template_kwargs: { enable_thinking: false },
        response_format: this._lorebookTopicFormat(count, true),
      };
      const response = await this.makeRequest("/chat/completions", data, false, false);
      const list = this._readLorebookTopics(response);
      const taken = new Set(existing.map((t) => t.toLowerCase()));
      const topics = [];
      for (const item of list) {
        const rawTitle = typeof item === "string" ? item : item?.title || item?.name;
        const title = typeof rawTitle === "string" ? rawTitle.trim() : "";
        if (!title || title.length > 120 || taken.has(title.toLowerCase())) continue;
        const keys = Array.isArray(item?.keys) ? [...new Set(item.keys
          .filter((k) => typeof k === "string" && k.trim() && k.length <= 80)
          .map((k) => k.trim()))].slice(0, 4) : [];
        taken.add(title.toLowerCase());
        topics.push({ title, keys: keys.length ? keys : [title] });
      }
      if (!topics.length) throw new Error("The model did not return any usable topics. Try again, or add more to the setting.");
      return topics.slice(0, count);
    },

    async writeWorldEntry(book, { title, keys, hint = "", related = [] }, onStream) {
      const data = {
        model: this.config.get("api.text.model"),
        messages: [
          { role: "system", content: `You write lorebook (World Info) entries for AI roleplay.\n\n${WRITING_RULES}` },
          {
            role: "user",
            content: `${worldContext(book, related)}\n\nWrite the entry for: ${title}\nKeys: ${keys.join(", ")}${hint ? `\nGuidance: ${hint}` : ""}`,
          },
        ],
        temperature: 0.7,
        max_tokens: 1200,
        stream: true,
      };
      const response = await this.makeRequest("/chat/completions", data, false, true);
      return (await this.handleStreamResponse(response, onStream || (() => {}))).trim();
    },

    async reviseWorldEntry(book, { title, keys, content, instruction }, onStream) {
      const data = {
        model: this.config.get("api.text.model"),
        messages: [
          {
            role: "system",
            content: `You revise lorebook (World Info) entries for AI roleplay. Apply the instruction faithfully, changing only what it asks, and keep every other fact in the existing entry.\n\n${WRITING_RULES}`,
          },
          {
            role: "user",
            content: `${worldContext(book, [])}\n\nEntry: ${title}\nKeys: ${keys.join(", ")}\n\nExisting entry:\n${content}\n\nInstruction: ${instruction}\n\nOutput the revised entry.`,
          },
        ],
        temperature: 0.6,
        max_tokens: 1200,
        stream: true,
      };
      const response = await this.makeRequest("/chat/completions", data, false, true);
      return (await this.handleStreamResponse(response, onStream || (() => {}))).trim();
    },
  });

  // ── Formats ─────────────────────────────────────────────────────────────

  // SillyTavern's own World Info file: entries keyed by uid.
  function toWorldInfo(book) {
    const entries = {};
    book.entries.forEach((entry, index) => {
      entries[index] = {
        uid: index,
        key: entry.keys,
        keysecondary: entry.secondaryKeys,
        comment: entryTitle(entry),
        content: entry.content,
        constant: !!entry.constant,
        vectorized: false,
        selective: entry.secondaryKeys.length > 0,
        selectiveLogic: 0,
        addMemo: true,
        order: 100,
        position: 0,
        disable: entry.enabled === false,
        excludeRecursion: false,
        preventRecursion: false,
        delayUntilRecursion: false,
        probability: 100,
        useProbability: true,
        depth: 4,
        group: "",
        groupOverride: false,
        groupWeight: 100,
        scanDepth: null,
        caseSensitive: null,
        matchWholeWords: null,
        useGroupScoring: null,
        automationId: "",
        role: null,
        sticky: 0,
        cooldown: 0,
        delay: 0,
        displayIndex: index,
      };
    });
    return { name: book.name, description: book.setting, entries };
  }

  function strings(value) {
    if (Array.isArray(value)) return value.map((v) => String(v).trim()).filter(Boolean);
    return typeof value === "string" ? splitKeys(value) : [];
  }

  // Native World Info, a character_book, or a whole V2/V3 card.
  function fromAnyLorebook(json, fallbackName) {
    const card = json?.data && (json.spec || json.data.character_book) ? json.data : null;
    const source = card ? card.character_book : json;
    if (!source || typeof source !== "object" || !source.entries) {
      throw new Error(card ? "That card has no lorebook." : "That file is not a lorebook.");
    }
    const raw = Array.isArray(source.entries) ? source.entries : Object.values(source.entries);
    const entries = raw
      .filter((e) => e && typeof e === "object")
      .map((e) => ({
        id: newId(),
        title: String(e.comment || e.name || "").trim(),
        keys: strings(e.keys ?? e.key),
        secondaryKeys: strings(e.secondary_keys ?? e.keysecondary),
        content: String(e.content || ""),
        constant: !!e.constant,
        enabled: e.enabled !== false && e.disable !== true,
      }))
      .filter((e) => e.keys.length || e.content.trim());
    if (!entries.length) throw new Error("That lorebook has no entries.");
    return {
      id: null,
      name: String(source.name || card?.name || fallbackName || "Imported lorebook").replace(/ Lorebook$/, ""),
      setting: String(source.description || ""),
      entries,
    };
  }

  // ── The tab ─────────────────────────────────────────────────────────────

  const $ = (id) => document.getElementById(id);

  Object.assign(CharacterGeneratorApp.prototype, {
    initWorldbook() {
      if (!$("view-lorebooks") || this._worldbookReady) return;
      this._worldbookReady = true;
      this.worldbook = this._readWorldbookDraft() || blankBook();
      this._worldbookSuggestions = [];

      $("wb-name").addEventListener("input", (e) => { this.worldbook.name = e.target.value; this._saveWorldbookDraft(); });
      $("wb-setting").addEventListener("input", (e) => { this.worldbook.setting = e.target.value; this._saveWorldbookDraft(); });
      $("wb-new-btn").addEventListener("click", () => this.newWorldbook());
      $("wb-import-btn").addEventListener("click", () => $("wb-import-file").click());
      $("wb-import-file").addEventListener("change", (e) => this.importWorldbook(e));
      $("wb-suggest-btn").addEventListener("click", () => this.suggestWorldbookTopics());
      $("wb-draft-btn").addEventListener("click", () => this.draftWorldbookEntries());
      $("wb-stop-btn").addEventListener("click", () => this.stopWorldbook());
      $("wb-save-btn").addEventListener("click", () => this.saveWorldbook());
      $("wb-download-btn").addEventListener("click", () => this.downloadWorldbook());
      $("wb-attach-btn").addEventListener("click", () => this.attachWorldbookToCharacter());
      $("wb-filter").addEventListener("input", () => this.renderWorldbookEntries());
      $("wb-write-btn").addEventListener("click", () => this.writeWorldbookEntry());
      $("wb-revise-btn").addEventListener("click", () => this.reviseWorldbookEntry());
      $("wb-entry-save-btn").addEventListener("click", () => this.saveWorldbookEntry());
      $("wb-entry-cancel-btn").addEventListener("click", () => this.resetWorldbookEditor());

      $("wb-entries").addEventListener("click", (e) => {
        const button = e.target.closest("button[data-wb-action]");
        const row = e.target.closest("[data-wb-id]");
        if (!row) return;
        if (button?.dataset.wbAction === "delete") this.deleteWorldbookEntry(row.dataset.wbId);
        else this.editWorldbookEntry(row.dataset.wbId);
      });
      $("wb-suggestions").addEventListener("click", (e) => {
        const chip = e.target.closest("button[data-wb-topic]");
        if (chip) this.useWorldbookTopic(Number(chip.dataset.wbTopic));
      });
      $("wb-library").addEventListener("click", (e) => {
        const button = e.target.closest("button[data-wb-library]");
        if (!button) return;
        const id = button.closest("[data-wb-book]").dataset.wbBook;
        if (button.dataset.wbLibrary === "load") this.loadWorldbook(id);
        else this.deleteSavedWorldbook(id);
      });
      $("tab-lorebooks")?.addEventListener("click", () => this.refreshWorldbookLibrary());

      this.renderWorldbook();
      this.refreshWorldbookLibrary();
    },

    // ── State ─────────────────────────────────────────────────────────────
    _readWorldbookDraft() {
      try {
        const draft = JSON.parse(localStorage.getItem(DRAFT_KEY) || "null");
        return draft && Array.isArray(draft.entries) ? { ...blankBook(), ...draft } : null;
      } catch {
        return null;
      }
    },

    // Per-browser safety net for unsaved work; the library is the real store.
    _saveWorldbookDraft() {
      try { localStorage.setItem(DRAFT_KEY, JSON.stringify(this.worldbook)); } catch { /* storage full or blocked */ }
    },

    _requireText() {
      if (this.config.engineReady("text")) return true;
      this.showNotification(window.engrai.missing("text"), "warning");
      return false;
    },

    _worldbookBusy(busy, message = "") {
      this._worldbookWorking = busy;
      for (const id of ["wb-suggest-btn", "wb-draft-btn", "wb-write-btn", "wb-revise-btn", "wb-import-btn", "wb-new-btn"]) {
        const el = $(id);
        if (el) el.disabled = busy;
      }
      $("wb-stop-btn").style.display = busy ? "" : "none";
      $("wb-progress").textContent = message;
    },

    stopWorldbook() {
      this._worldbookStop = true;
      this.apiHandler.stopGeneration();
    },

    newWorldbook() {
      if (this.worldbook.entries.length && !confirm("Start a new lorebook? Unsaved changes to this one will be lost.")) return;
      this.worldbook = blankBook();
      this._worldbookSuggestions = [];
      this._saveWorldbookDraft();
      this.renderWorldbook();
    },

    // ── Rendering ─────────────────────────────────────────────────────────
    renderWorldbook() {
      $("wb-name").value = this.worldbook.name;
      $("wb-setting").value = this.worldbook.setting;
      this.renderWorldbookEntries();
      this.renderWorldbookSuggestions();
      this.resetWorldbookEditor();
    },

    renderWorldbookEntries() {
      const list = $("wb-entries");
      const entries = this.worldbook.entries;
      const filter = $("wb-filter").value.trim().toLowerCase();
      $("wb-count").textContent = String(entries.length);

      const tokens = entries.reduce((sum, e) => sum + this.estimateTokens(e.content), 0);
      const constant = entries.filter((e) => e.constant).reduce((sum, e) => sum + this.estimateTokens(e.content), 0);
      $("wb-tokens").textContent = entries.length
        ? `About ${tokens.toLocaleString()} tokens in all${constant ? `, ${constant.toLocaleString()} of them always active` : ""}.`
        : "";

      const shown = filter
        ? entries.filter((e) => `${entryTitle(e)} ${e.keys.join(" ")} ${e.content}`.toLowerCase().includes(filter))
        : entries;
      if (!shown.length) {
        list.innerHTML = `<p class="library-empty">${entries.length ? "No entries match the filter." : "No entries yet. Suggest topics or draft a few to start."}</p>`;
        return;
      }
      const editing = $("wb-entry-id").value;
      list.innerHTML = shown.map((entry) => `
        <div class="library-item worldbook-entry${entry.id === editing ? " is-editing" : ""}" data-wb-id="${escapeHtml(entry.id)}">
          <div class="worldbook-entry-body">
            <div class="library-item-title">${escapeHtml(entryTitle(entry))}${entry.constant ? ' <span class="worldbook-flag">Always active</span>' : ""}</div>
            <div class="worldbook-keys">${entry.keys.map((k) => `<span class="worldbook-key">${escapeHtml(k)}</span>`).join("")}</div>
            <p class="worldbook-snippet">${escapeHtml(entry.content.slice(0, 160))}${entry.content.length > 160 ? "…" : ""}</p>
          </div>
          <div class="library-item-actions">
            <button class="btn-small" type="button" data-wb-action="edit">Edit</button>
            <button class="btn-small" type="button" data-wb-action="delete">Delete</button>
          </div>
        </div>`).join("");
    },

    renderWorldbookSuggestions() {
      const box = $("wb-suggestions");
      const topics = this._worldbookSuggestions;
      box.innerHTML = topics.length
        ? `<span class="worldbook-suggestions-label">Suggested topics, click one to start an entry:</span>` +
          topics.map((t, i) => `<button type="button" class="btn-small worldbook-topic" data-wb-topic="${i}" title="${escapeHtml(t.keys.join(", "))}">${escapeHtml(t.title)}</button>`).join("")
        : "";
    },

    // ── Editor ────────────────────────────────────────────────────────────
    _editorEntry() {
      return {
        id: $("wb-entry-id").value,
        title: $("wb-entry-title").value.trim(),
        keys: splitKeys($("wb-entry-keys").value),
        secondaryKeys: splitKeys($("wb-entry-secondary").value),
        content: $("wb-entry-content").value.trim(),
        constant: $("wb-entry-constant").checked,
      };
    },

    resetWorldbookEditor() {
      for (const id of ["wb-entry-id", "wb-entry-title", "wb-entry-keys", "wb-entry-secondary", "wb-entry-content", "wb-entry-hint"]) $(id).value = "";
      $("wb-entry-constant").checked = false;
      $("wb-editor-title").textContent = "New entry";
      $("wb-revise-btn").style.display = "none";
      this.renderWorldbookEntries();
    },

    editWorldbookEntry(id) {
      const entry = this.worldbook.entries.find((e) => e.id === id);
      if (!entry) return;
      $("wb-entry-id").value = entry.id;
      $("wb-entry-title").value = entry.title;
      $("wb-entry-keys").value = entry.keys.join(", ");
      $("wb-entry-secondary").value = entry.secondaryKeys.join(", ");
      $("wb-entry-content").value = entry.content;
      $("wb-entry-hint").value = "";
      $("wb-entry-constant").checked = !!entry.constant;
      $("wb-editor-title").textContent = `Editing ${entryTitle(entry)}`;
      $("wb-revise-btn").style.display = "";
      this.renderWorldbookEntries();
      if (window.innerWidth < 900) $("wb-editor").scrollIntoView({ behavior: "smooth", block: "start" });
    },

    useWorldbookTopic(index) {
      const topic = this._worldbookSuggestions[index];
      if (!topic) return;
      this.resetWorldbookEditor();
      $("wb-entry-title").value = topic.title;
      $("wb-entry-keys").value = topic.keys.join(", ");
      $("wb-editor-title").textContent = `New entry: ${topic.title}`;
      $("wb-entry-content").focus();
    },

    saveWorldbookEntry() {
      const draft = this._editorEntry();
      if (!draft.keys.length || !draft.content) {
        this.showNotification("An entry needs at least one key and some content.", "warning");
        return;
      }
      const entries = this.worldbook.entries;
      const index = entries.findIndex((e) => e.id === draft.id);
      if (index >= 0) {
        entries[index] = { ...entries[index], ...draft };
      } else {
        entries.push({ ...draft, id: newId(), enabled: true });
        // The suggestion has been used; take it off the list.
        this._worldbookSuggestions = this._worldbookSuggestions.filter(
          (t) => t.title.toLowerCase() !== (draft.title || draft.keys[0]).toLowerCase(),
        );
        this.renderWorldbookSuggestions();
      }
      this._saveWorldbookDraft();
      this.resetWorldbookEditor();
      this.showNotification("Entry saved.", "success");
    },

    deleteWorldbookEntry(id) {
      const entry = this.worldbook.entries.find((e) => e.id === id);
      if (!entry || !confirm(`Delete "${entryTitle(entry)}"?`)) return;
      this.worldbook.entries = this.worldbook.entries.filter((e) => e.id !== id);
      if ($("wb-entry-id").value === id) this.resetWorldbookEditor();
      this._saveWorldbookDraft();
      this.renderWorldbookEntries();
    },

    // ── Generation ────────────────────────────────────────────────────────
    _requireSetting() {
      if (this.worldbook.setting.trim() || this.worldbook.name.trim()) return true;
      this.showNotification("Describe the setting first, so entries have something to agree with.", "warning");
      $("wb-setting").focus();
      return false;
    },

    async suggestWorldbookTopics() {
      if (!this._requireText() || !this._requireSetting()) return;
      this._worldbookBusy(true, "Finding topics…");
      try {
        const topics = await this.apiHandler.suggestWorldTopics(this.worldbook, SUGGEST_LIMIT / 2);
        this._worldbookSuggestions = topics;
        this.renderWorldbookSuggestions();
        this._worldbookBusy(false, `${topics.length} topics suggested.`);
      } catch (error) {
        this._worldbookBusy(false, "");
        if (!this._worldbookStop) this.showNotification(`Could not suggest topics: ${error.message}`, "error");
      } finally {
        this._worldbookStop = false;
      }
    },

    async draftWorldbookEntries() {
      if (!this._requireText() || !this._requireSetting()) return;
      const count = Number($("wb-draft-count").value) || 5;
      this._worldbookStop = false;
      let written = 0;
      try {
        // Unused suggestions go first; ask for more only if they run short.
        let topics = this._worldbookSuggestions.slice(0, count);
        if (topics.length < count) {
          this._worldbookBusy(true, "Planning topics…");
          const more = await this.apiHandler.suggestWorldTopics(
            { ...this.worldbook, entries: [...this.worldbook.entries, ...topics.map((t) => ({ title: t.title, keys: t.keys }))] },
            count - topics.length,
          );
          topics = [...topics, ...more].slice(0, count);
        }
        const related = [...this.worldbook.entries.map(entryTitle), ...topics.map((t) => t.title)];
        for (const [index, topic] of topics.entries()) {
          if (this._worldbookStop) break;
          this._worldbookBusy(true, `Writing ${index + 1} of ${topics.length}: ${topic.title}…`);
          const content = await this.apiHandler.writeWorldEntry(this.worldbook, {
            title: topic.title,
            keys: topic.keys,
            related: related.filter((t) => t !== topic.title),
          });
          if (!content) continue;
          this.worldbook.entries.push({ id: newId(), title: topic.title, keys: topic.keys, secondaryKeys: [], content, constant: false, enabled: true });
          this._worldbookSuggestions = this._worldbookSuggestions.filter((t) => t.title !== topic.title);
          written++;
          this._saveWorldbookDraft();
          this.renderWorldbookEntries();
          this.renderWorldbookSuggestions();
        }
        this._worldbookBusy(false, this._worldbookStop
          ? `Stopped after ${written} ${written === 1 ? "entry" : "entries"}.`
          : `Drafted ${written} ${written === 1 ? "entry" : "entries"}. Review them, then save the lorebook.`);
      } catch (error) {
        this._worldbookBusy(false, written ? `Drafted ${written} before an error.` : "");
        if (!this._worldbookStop) this.showNotification(`Drafting stopped: ${error.message}`, "error");
      } finally {
        this._worldbookStop = false;
      }
    },

    async writeWorldbookEntry() {
      if (!this._requireText()) return;
      const draft = this._editorEntry();
      if (!draft.keys.length) {
        this.showNotification("Add at least one key first; it says what the entry is about.", "warning");
        $("wb-entry-keys").focus();
        return;
      }
      if (draft.content && !confirm("Replace the current content with a new draft?")) return;
      const box = $("wb-entry-content");
      this._worldbookBusy(true, `Writing ${draft.title || draft.keys[0]}…`);
      try {
        const content = await this.apiHandler.writeWorldEntry(
          this.worldbook,
          {
            title: draft.title || draft.keys[0],
            keys: draft.keys,
            hint: $("wb-entry-hint").value.trim(),
            related: this.worldbook.entries.filter((e) => e.id !== draft.id).map(entryTitle),
          },
          (_token, full) => { if (typeof full === "string") box.value = full; },
        );
        box.value = content;
        this._worldbookBusy(false, "Draft written. Edit it if you like, then save the entry.");
      } catch (error) {
        this._worldbookBusy(false, "");
        if (!this._worldbookStop) this.showNotification(`Could not write the entry: ${error.message}`, "error");
      } finally {
        this._worldbookStop = false;
      }
    },

    async reviseWorldbookEntry() {
      if (!this._requireText()) return;
      const draft = this._editorEntry();
      const instruction = $("wb-entry-hint").value.trim();
      if (!draft.content || !instruction) {
        this.showNotification("Revising needs existing content and guidance saying what to change.", "warning");
        $("wb-entry-hint").focus();
        return;
      }
      const box = $("wb-entry-content");
      const before = box.value;
      this._worldbookBusy(true, `Revising ${draft.title || draft.keys[0]}…`);
      try {
        const content = await this.apiHandler.reviseWorldEntry(
          this.worldbook,
          { title: draft.title || draft.keys[0], keys: draft.keys, content: draft.content, instruction },
          (_token, full) => { if (typeof full === "string") box.value = full; },
        );
        box.value = content || before;
        this._worldbookBusy(false, "Revised. Save the entry to keep it.");
      } catch (error) {
        box.value = before;
        this._worldbookBusy(false, "");
        if (!this._worldbookStop) this.showNotification(`Could not revise the entry: ${error.message}`, "error");
      } finally {
        this._worldbookStop = false;
      }
    },

    // ── Files ─────────────────────────────────────────────────────────────
    downloadWorldbook() {
      if (!this.worldbook.entries.length) {
        this.showNotification("There are no entries to download yet.", "warning");
        return;
      }
      const blob = new Blob([JSON.stringify(toWorldInfo(this.worldbook), null, 2)], { type: "application/json" });
      const name = (this.worldbook.name || "lorebook").trim().replace(/[^\w\- ]+/g, "").replace(/\s+/g, "_") || "lorebook";
      this.downloadBlob(blob, `${name}.json`);
    },

    async importWorldbook(event) {
      const file = event.target.files[0];
      event.target.value = "";
      if (!file) return;
      try {
        const isPng = file.type === "image/png" || /\.png$/i.test(file.name);
        const json = isPng ? await this.pngEncoder.extractCharacterData(file) : JSON.parse(await file.text());
        const imported = fromAnyLorebook(json, file.name.replace(/\.(json|png)$/i, ""));
        if (this.worldbook.entries.length && !confirm(`Replace the open lorebook with "${imported.name}" (${imported.entries.length} entries)?`)) return;
        this.worldbook = imported;
        this._worldbookSuggestions = [];
        this._saveWorldbookDraft();
        this.renderWorldbook();
        this.showNotification(`Imported ${imported.entries.length} entries. Save it to keep it in the library.`, "success");
      } catch (error) {
        this.showNotification(`Could not import: ${error.message}`, "error");
      }
    },

    // ── Library ───────────────────────────────────────────────────────────
    async refreshWorldbookLibrary() {
      const box = $("wb-library");
      if (!box) return;
      try {
        const res = await window.authFetch("/cardgen/api/storage/lorebooks");
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const rows = (await res.json()).sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)));
        box.innerHTML = rows.length
          ? rows.map((row) => `
            <div class="library-item" data-wb-book="${escapeHtml(row.id)}">
              <div>
                <div class="library-item-title">${escapeHtml(row.name || "Untitled")}</div>
                <div class="library-item-date">${(row.entries || []).length} entries · ${new Date(row.updatedAt).toLocaleString()}</div>
              </div>
              <div class="library-item-actions">
                <button class="btn-small" type="button" data-wb-library="load">Open</button>
                <button class="btn-small" type="button" data-wb-library="delete">Delete</button>
              </div>
            </div>`).join("")
          : '<p class="library-empty">No saved lorebooks yet.</p>';
      } catch (error) {
        box.innerHTML = `<p class="library-empty">Could not load saved lorebooks: ${escapeHtml(error.message)}</p>`;
      }
    },

    async saveWorldbook() {
      const book = this.worldbook;
      if (!book.name.trim()) {
        this.showNotification("Give the lorebook a name before saving.", "warning");
        $("wb-name").focus();
        return;
      }
      if (!book.entries.length) {
        this.showNotification("Add at least one entry before saving.", "warning");
        return;
      }
      try {
        const res = await window.authFetch("/cardgen/api/storage/lorebooks", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...(book.id ? { id: book.id } : {}), name: book.name.trim(), setting: book.setting, entries: book.entries }),
        });
        const saved = await res.json();
        if (!res.ok) throw new Error(saved.error || `HTTP ${res.status}`);
        book.id = saved.id;
        this._saveWorldbookDraft();
        this.showNotification(`Saved "${book.name.trim()}".`, "success");
        this.refreshWorldbookLibrary();
      } catch (error) {
        this.showNotification(`Could not save: ${error.message}`, "error");
      }
    },

    async loadWorldbook(id) {
      if (this.worldbook.entries.length && this.worldbook.id !== id &&
          !confirm("Open this lorebook? Unsaved changes to the current one will be lost.")) return;
      try {
        const res = await window.authFetch(`/cardgen/api/storage/lorebooks/${encodeURIComponent(id)}`);
        const row = await res.json();
        if (!res.ok) throw new Error(row.error || `HTTP ${res.status}`);
        this.worldbook = {
          id: row.id,
          name: row.name || "",
          setting: row.setting || "",
          entries: (row.entries || []).map((e) => ({
            id: e.id || newId(), title: e.title || "", keys: strings(e.keys), secondaryKeys: strings(e.secondaryKeys),
            content: e.content || "", constant: !!e.constant, enabled: e.enabled !== false,
          })),
        };
        this._worldbookSuggestions = [];
        this._saveWorldbookDraft();
        this.renderWorldbook();
      } catch (error) {
        this.showNotification(`Could not open the lorebook: ${error.message}`, "error");
      }
    },

    async deleteSavedWorldbook(id) {
      if (!confirm("Delete this lorebook from the library?")) return;
      await window.authFetch(`/cardgen/api/storage/lorebooks/${encodeURIComponent(id)}`, { method: "DELETE" });
      if (this.worldbook.id === id) { this.worldbook.id = null; this._saveWorldbookDraft(); }
      this.refreshWorldbookLibrary();
    },

    // ── Into a character ──────────────────────────────────────────────────
    // Copies, not links: the card's lorebook is the card's, and editing it
    // there afterwards must not change this world lorebook, or the reverse.
    attachWorldbookToCharacter() {
      if (!this.currentCharacter) {
        this.showNotification("Open or generate a character on the Characters tab first.", "warning");
        return;
      }
      const have = new Set((this.lorebookEntries || []).map((e) => (e.keys || []).join("|").toLowerCase()));
      const copies = this.worldbook.entries
        .filter((e) => e.enabled !== false && !have.has(e.keys.join("|").toLowerCase()))
        .map((e, n) => ({
          id: `${Date.now()}${n}`,
          keys: e.keys,
          content: e.content,
          enabled: true,
          constant: !!e.constant,
          secondary_keys: e.secondaryKeys,
          selective: e.secondaryKeys.length > 0,
          comment: entryTitle(e),
          name: entryTitle(e),
        }));
      if (!copies.length) {
        this.showNotification("Every entry is already in this character's lorebook.", "info");
        return;
      }
      this.lorebookEntries = [...(this.lorebookEntries || []), ...copies];
      this.currentCharacter.character_book = this.buildCharacterBook();
      this.updateLorebookEntryCount();
      this.saveCardToLibrary();
      this.showNotification(`Added ${copies.length} ${copies.length === 1 ? "entry" : "entries"} to ${this.currentCharacter.name || "the character"}'s lorebook.`, "success");
    },
  });

  // CardGen builds the app after sign-in; join its start-up rather than racing it.
  const baseInit = CharacterGeneratorApp.prototype.init;
  CharacterGeneratorApp.prototype.init = async function (...args) {
    await baseInit.apply(this, args);
    this.initWorldbook();
  };

  // Exposed for tests and for the console, nothing else depends on them.
  window.WorldLorebook = { toWorldInfo, fromAnyLorebook };
})();
