// Main Application Controller
// Core class definition only — feature modules are loaded separately:
//   app-ui.js             — UI helpers (notifications, stream, state buttons)
//   character-display.js  — display, edit, reset, regenerate fields, import
//   image-handler.js      — image generate/upload/select, reference image, model fetch
//   lorebook-handler.js   — lorebook CRUD, download, upload
//   alt-greetings-handler.js — alternate greetings CRUD
//   library-handler.js    — IndexedDB save/load/delete for prompts & cards
//   revision-handler.js   — AI revision, token reduction, consistency check
class CharacterGeneratorApp {
  constructor() {
    this.characterGenerator = window.characterGenerator;
    this.imageGenerator = window.imageGenerator;
    this.pngEncoder = window.pngEncoder;
    this.lorebookGenerator = window.lorebookGenerator;
    this.config = window.config;
    this.apiHandler = window.apiHandler;
    this.storage = window.characterStorage;
    this.storageReady = false;

    this.currentCharacter = null;
    this.originalCharacter = null;
    this.currentImageUrl = null;
    this.imageHistoryUrls = [];
    this.lorebookEntries = [];
    this.altGreetings = [];
    this.lorebookData = null;
    this.referenceImageDataUrl = "";
    this.playgroundImageUrl = "";
    this.playgroundHistory = []; // [{ url, label }] — every version of the Playground working image
    this.combineImage2Url = ""; // Playground's Combine tab — the second reference image
    this.isGenerating = false;
    this.isRevising = false;
    this.lastConsistencyReport = null;

    // Review Queue (temporary card store)
    this._reviewQueue = [];
    this._reviewModalIndex = 0;
    this._reviewModalBound = false;

    this.init();
  }

  async init() {
    const savedConfig = localStorage.getItem("charGeneratorConfig");
    if (savedConfig && (savedConfig.includes('"api":{"baseUrl"') || savedConfig.includes('"textModel"'))) {
      localStorage.removeItem("charGeneratorConfig");
    }
    await this.config.waitForConfig();

    // Patch displayCharacter to load image prompt fields
    const origDisplay = this.displayCharacter;
    if (origDisplay && !this._displayCharacterPatched) {
      this.displayCharacter = function(...args) {
        origDisplay.apply(this, args);
        const customPromptInput = document.getElementById("custom-image-prompt");
        const promptGuidanceInput = document.getElementById("prompt-guidance");
        if (customPromptInput) {
            customPromptInput.value = this.currentCharacter?.imagePrompt || "";
            if (window.updatePromptCharCount) window.updatePromptCharCount();
        }
        if (promptGuidanceInput) {
            promptGuidanceInput.value = this.currentCharacter?.imageGuidance || "";
        }
      };
      this._displayCharacterPatched = true;
    }

    await this.ensureStorageReady();
    this.config.saveToForm();
    this.initTheme();
    this.bindEvents();
    this.checkAPIStatus();
    this.refreshLibraryViews();
  }

  async ensureStorageReady() {
    if (!this.storage) {
      this.storageReady = false;
      this.updateLibraryStatus("Local library unavailable in this session.");
      return;
    }
    try {
      await this.storage.init();
      this.storageReady = true;
      this.updateLibraryStatus("Local library ready.");
    } catch (error) {
      console.error("Failed to initialize IndexedDB:", error);
      this.storageReady = false;
      this.updateLibraryStatus("IndexedDB failed to initialize. Prompt/card saving is disabled.");
    }
  }

  bindEvents() {
    document.getElementById("generate-btn").addEventListener("click", () => this.handleGenerate());
    document.getElementById("stop-btn").addEventListener("click", () => this.handleStop());

    // Mode selector — toggle between Classic and Inspire input blocks
    const modeRadios = document.querySelectorAll('input[name="generation-mode"]');
    if (modeRadios.length > 0) {
      const classicBlock = document.getElementById("classic-mode-inputs");
      const inspireBlock = document.getElementById("inspire-mode-inputs");
      const onModeChange = () => {
        const mode = document.querySelector('input[name="generation-mode"]:checked')?.value || "classic";
        if (classicBlock) classicBlock.style.display = mode === "classic" ? "" : "none";
        if (inspireBlock) inspireBlock.style.display = mode === "inspire" ? "" : "none";
        // "Generate 4" only reads the Classic concept/reference-image fields — in
        // Inspire mode "Generate 1" already runs an idea-spark-then-pick flow off
        // the Inspire filters, and in Search mode there's no concept text for it
        // to brainstorm from. Hide it outside Classic to avoid a second, unrelated
        // "generate ideas" button.
        const batchBtn = document.getElementById("batch-generate-btn");
        if (batchBtn) batchBtn.style.display = mode === "classic" ? "" : "none";
      };
      modeRadios.forEach((r) => r.addEventListener("change", onModeChange));
      onModeChange();
    }

    // Batch generation — Classic-mode-only "4 ideas, pick one" flow
    const batchGenerateBtn = document.getElementById("batch-generate-btn");
    if (batchGenerateBtn) batchGenerateBtn.addEventListener("click", () => this.handleBatchGenerate());

    // Batch modal
    const batchModal = document.getElementById("batch-modal");
    const batchModalCloseBtn = document.getElementById("batch-modal-close-btn");
    const batchGrid = document.getElementById("batch-grid");
    if (batchModalCloseBtn) batchModalCloseBtn.addEventListener("click", () => this.closeBatchModal());
    if (batchModal) batchModal.addEventListener("click", (e) => { if (e.target === batchModal) this.closeBatchModal(); });
    if (batchGrid) batchGrid.addEventListener("click", (e) => this._handleBatchGridClick(e));

    // Inspire Me modal events
    const inspirePickerCloseBtn = document.getElementById("inspire-picker-close-btn");
    const inspirePickerModal = document.getElementById("inspire-picker-modal");
    const inspireGoBtn = document.getElementById("inspire-go-btn");
    if (inspirePickerCloseBtn) inspirePickerCloseBtn.addEventListener("click", () => this.closeInspireModal());
    if (inspirePickerModal) inspirePickerModal.addEventListener("click", (e) => { if (e.target === inspirePickerModal) this.closeInspireModal(); });
    if (inspireGoBtn) inspireGoBtn.addEventListener("click", () => this.handleInspireFullGenerate());

    document.getElementById("download-json-btn").addEventListener("click", () => this.handleDownloadJSON());
    document.getElementById("download-btn").addEventListener("click", () => this.handleDownload());

    const saveCardBtn = document.getElementById("save-card-btn");
    if (saveCardBtn) saveCardBtn.addEventListener("click", () => this.handleSaveCardManual());
    const snapshotHistoryBtn = document.getElementById("snapshot-history-btn");
    if (snapshotHistoryBtn) snapshotHistoryBtn.addEventListener("click", () => this.handleSnapshotToHistory());

    // Import card buttons
    const importCardTopBtn = document.getElementById("import-card-top-btn");
    const importCardTopInput = document.getElementById("import-card-top-input");
    if (importCardTopInput) {
      if (importCardTopBtn) importCardTopBtn.addEventListener("click", () => importCardTopInput.click());
      importCardTopInput.addEventListener("change", (e) => this.handleImportCard(e, false));
    }

    // Import & Remaster buttons
    const importRemasterTopBtn = document.getElementById("import-remaster-top-btn");
    const importRemasterTopInput = document.getElementById("import-remaster-top-input");
    if (importRemasterTopInput) {
      if (importRemasterTopBtn) importRemasterTopBtn.addEventListener("click", () => importRemasterTopInput.click());
      importRemasterTopInput.addEventListener("change", (e) => this.handleImportCard(e, true));
    }

    // Remaster Current Card buttons (triggers remaster on already-loaded card)
    const remasterBtn = document.getElementById("remaster-btn");
    const remasterTopBtn = document.getElementById("remaster-top-btn");
    if (remasterBtn) remasterBtn.addEventListener("click", () => this.handleRemasterCurrentCard());
    if (remasterTopBtn) remasterTopBtn.addEventListener("click", () => this.handleRemasterCurrentCard());

    // Clear / New button — lives inside the collapsible section's <summary> so
    // it stays visible when collapsed; must stop the click from also toggling
    // the <details> open/closed.
    const clearNewBtn = document.getElementById("clear-new-btn");
    if (clearNewBtn) clearNewBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      this.handleClearNew();
    });

    // Character gallery panel
    const galleryGenerateBtn = document.getElementById("gallery-generate-btn");
    if (galleryGenerateBtn) galleryGenerateBtn.addEventListener("click", () => this.handleGalleryGenerate());
    // Saves the card straight from the gallery hint, so the fix is one click
    // from the thing that's blocked rather than a hunt for the Save to Library
    // button further down the page. Sets currentCardId, which unlocks the panel.
    const gallerySaveCardBtn = document.getElementById("gallery-save-card-btn");
    if (gallerySaveCardBtn) gallerySaveCardBtn.addEventListener("click", () => this.handleSaveCardManual());
    const galleryGenerateCount = document.getElementById("gallery-generate-count");
    if (galleryGenerateCount) galleryGenerateCount.addEventListener("change", () => this._renderGalleryInstructionInputs());
    // Renders the initial single instruction box — the container starts
    // empty in index.html since the box count depends on Count above.
    if (typeof this._renderGalleryInstructionInputs === "function") this._renderGalleryInstructionInputs();
    const galleryUploadBtn = document.getElementById("gallery-upload-btn");
    const galleryUploadInput = document.getElementById("gallery-upload-input");
    // Gate before opening the file picker — the button is soft-locked rather
    // than `disabled` on an unsaved card (see _setGalleryPanelLocked), so the
    // click does reach us and must be explained instead of silently ignored.
    if (galleryUploadBtn && galleryUploadInput) {
      galleryUploadBtn.addEventListener("click", () => {
        if (!this._requireSavedCard()) return;
        galleryUploadInput.click();
      });
    }
    if (galleryUploadInput) galleryUploadInput.addEventListener("change", (e) => this.handleGalleryUpload(e));
    const galleryGenerateAcceptAllBtn = document.getElementById("gallery-generate-accept-all-btn");
    if (galleryGenerateAcceptAllBtn) galleryGenerateAcceptAllBtn.addEventListener("click", () => this.handleGalleryGenerateAcceptAll());
    const galleryGenerateDiscardBtn = document.getElementById("gallery-generate-discard-btn");
    if (galleryGenerateDiscardBtn) galleryGenerateDiscardBtn.addEventListener("click", () => this.handleGalleryGenerateDiscard());
    // Per-item Add/Discard buttons (and the preview thumbnail itself) are
    // rendered dynamically (one per pending image, see
    // _renderGalleryGeneratePreview), so this delegates from the static list
    // container instead of binding listeners that don't exist yet.
    const galleryGeneratePreviewList = document.getElementById("gallery-generate-preview-list");
    if (galleryGeneratePreviewList) {
      galleryGeneratePreviewList.addEventListener("click", (e) => {
        const acceptBtn = e.target.closest("[data-gallery-accept-index]");
        if (acceptBtn) {
          this.handleGalleryGenerateAcceptOne(parseInt(acceptBtn.dataset.galleryAcceptIndex, 10));
          return;
        }
        const discardBtn = e.target.closest("[data-gallery-discard-index]");
        if (discardBtn) {
          this.handleGalleryGenerateDiscardOne(parseInt(discardBtn.dataset.galleryDiscardIndex, 10));
          return;
        }
        const viewImg = e.target.closest("[data-gallery-view-index]");
        if (viewImg) {
          this._openGalleryPendingPreview(parseInt(viewImg.dataset.galleryViewIndex, 10));
        }
      });
    }


    // Revision buttons
    const reviseCharacterBtn = document.getElementById("revise-character-btn");
    if (reviseCharacterBtn) reviseCharacterBtn.addEventListener("click", () => this.handleReviseCharacter());
    const reduceTokensBtn = document.getElementById("reduce-tokens-btn");
    if (reduceTokensBtn) reduceTokensBtn.addEventListener("click", () => this.handleReduceTokens());
    const scanForLorebookBtn = document.getElementById("scan-for-lorebook-btn");
    if (scanForLorebookBtn) scanForLorebookBtn.addEventListener("click", () => this.handleScanForLorebook());
    const stopRevisionBtn = document.getElementById("stop-revision-btn");
    if (stopRevisionBtn) stopRevisionBtn.addEventListener("click", () => this.handleStopRevision());

    // Image buttons
    document.getElementById("generate-image-final-btn").addEventListener("click", () => this.handleRegenerateImage());
    const imageHistoryBtn = document.getElementById("image-history-btn");
    if (imageHistoryBtn) imageHistoryBtn.addEventListener("click", () => this.showImageHistory());

    const generateFourPromptsBtn = document.getElementById("generate-four-prompts-btn");
    if (generateFourPromptsBtn) generateFourPromptsBtn.addEventListener("click", () => this.handleGenerateFourPrompts());


    // Edit Image (image-to-image) button
    const editImageBtn = document.getElementById("edit-image-btn");
    if (editImageBtn) {
      editImageBtn.addEventListener("click", () => this.handleEditImage());
    }
    const editReferenceImageBtn = document.getElementById("edit-reference-image-btn");
    if (editReferenceImageBtn) {
      editReferenceImageBtn.addEventListener("click", () => this.handleEditReferenceImage());
    }

    // Image Playground tab (own self-contained setup — see image-playground.js)
    if (typeof this.initPlaygroundTab === "function") this.initPlaygroundTab();

    const imgOptModalClose = document.getElementById("image-options-modal-close-btn");
    const imgOptModal = document.getElementById("image-options-modal");
    if (imgOptModalClose) imgOptModalClose.addEventListener("click", () => this.closeImageOptionsModal());
    if (imgOptModal) imgOptModal.addEventListener("click", (e) => { if (e.target === imgOptModal) this.closeImageOptionsModal(); });

    document.getElementById("draft-prompt-btn").addEventListener("click", () => this.handleRegeneratePrompt());

    const imageStyleSelect = document.getElementById("image-style");
    if (imageStyleSelect) {
      imageStyleSelect.addEventListener("change", (e) => {
        const customStyleContainer = document.getElementById("custom-style-container");
        if (customStyleContainer) customStyleContainer.style.display = e.target.value === "custom" ? "block" : "none";
        this.saveAPISettings();
      });
    }

    const imageMoodSelect = document.getElementById("image-mood");
    if (imageMoodSelect) {
      imageMoodSelect.addEventListener("change", () => this.saveAPISettings());
    }

    const imageAspectRatioSelect = document.getElementById("image-aspect-ratio");
    if (imageAspectRatioSelect) {
      imageAspectRatioSelect.addEventListener("change", () => this.saveAPISettings());
    }

    const customImageStyleInput = document.getElementById("custom-image-style-input");
    if (customImageStyleInput) {
        customImageStyleInput.addEventListener("input", () => this.saveAPISettings());
        customImageStyleInput.addEventListener("paste", async (e) => {
            if (!e.clipboardData || !e.clipboardData.files.length) return;
            const file = e.clipboardData.files[0];
            if (!file.type.startsWith("image/")) return;
            e.preventDefault();
            e.stopPropagation();
            await window.app.handleStyleImageFile(file);
        });
    }

    const extractStyleBtn = document.getElementById("extract-style-btn");
    const styleImageUpload = document.getElementById("style-image-upload");
    if (extractStyleBtn && styleImageUpload) {
        extractStyleBtn.addEventListener("click", () => styleImageUpload.click());
        styleImageUpload.addEventListener("change", async (e) => {
            const file = e.target.files?.[0];
            if (!file) return;
            await window.app.handleStyleImageFile(file);
            e.target.value = "";
        });
    }

    // Character field reset buttons
    const resetNameBtn = document.getElementById("reset-name-btn");
    if (resetNameBtn) resetNameBtn.addEventListener("click", () => this.handleResetField("name"));
    document.getElementById("reset-description-btn").addEventListener("click", () => this.handleResetField("description"));
    document.getElementById("reset-personality-btn").addEventListener("click", () => this.handleResetField("personality"));
    document.getElementById("reset-scenario-btn").addEventListener("click", () => this.handleResetField("scenario"));
    if (document.getElementById("reset-first-message-btn")) document.getElementById("reset-first-message-btn").addEventListener("click", () => this.handleResetField("firstMessage"));
    if (document.getElementById("reset-post-history-btn")) document.getElementById("reset-post-history-btn").addEventListener("click", () => this.handleResetField("postHistoryInstructions"));
    if (document.getElementById("reset-creator-notes-btn")) document.getElementById("reset-creator-notes-btn").addEventListener("click", () => this.handleResetField("creatorNotes"));

    // Character field edit tracking
    const nameInput = document.getElementById("character-generated-name");
    if (nameInput) nameInput.addEventListener("input", () => this.handleCharacterEdit("name"));
    document.getElementById("character-description").addEventListener("input", () => this.handleCharacterEdit("description"));
    document.getElementById("character-personality").addEventListener("input", () => this.handleCharacterEdit("personality"));
    document.getElementById("character-scenario").addEventListener("input", () => this.handleCharacterEdit("scenario"));
    document.getElementById("character-first-message").addEventListener("input", () => this.handleCharacterEdit("firstMessage"));

    const postHistoryEl = document.getElementById("character-post-history");
    if (postHistoryEl) {
        postHistoryEl.addEventListener("input", () => this.handleCharacterEdit("postHistoryInstructions"));
    }

    // Image & file uploads
    document.getElementById("upload-image-btn").addEventListener("click", () => document.getElementById("image-upload-input").click());
    const cropBtn = document.getElementById("crop-image-btn");
    if (cropBtn) {
      cropBtn.addEventListener("click", () => this.openCropModal());
    }
    const cropRefBtn = document.getElementById("crop-reference-image-btn");
    if (cropRefBtn) {
      cropRefBtn.addEventListener("click", () => this.openCropModal('reference'));
    }

    document.getElementById("image-upload-input").addEventListener("change", (e) => this.handleImageUpload(e));
    this.initPasteImageModal();
    document.getElementById("lorebook-file").addEventListener("change", (e) => this.handleLorebookUpload(e));
    const referenceImageInput = document.getElementById("reference-image-file");
    if (referenceImageInput) referenceImageInput.addEventListener("change", (e) => this.handleReferenceImageUpload(e));
    
    // Bound directly on the reference paste zone (not document) so a paste
    // only lands here when this exact field is focused — each paste zone on
    // the page now owns its own scoped listener, see the equivalent
    // comment in image-playground.js's initPlaygroundTab().
    const referencePasteZone = document.getElementById("reference-paste-zone");
    if (referencePasteZone) referencePasteZone.addEventListener("paste", (e) => this.handleReferenceImagePaste(e));

    if (typeof this._initPasteZoneAutoSelect === "function") this._initPasteZoneAutoSelect();

    // Creator's Notes edit tracking
    const creatorNotesTextarea = document.getElementById("creator-notes");
    if (creatorNotesTextarea) creatorNotesTextarea.addEventListener("input", () => {
      if (this.currentCharacter) this.currentCharacter.creatorNotes = creatorNotesTextarea.value;
    });

    const customImagePrompt = document.getElementById("custom-image-prompt");
    if (customImagePrompt) {
      customImagePrompt.addEventListener("input", () => {
        if (this.currentCharacter) this.currentCharacter.imagePrompt = customImagePrompt.value;
      });
    }

    const promptGuidance = document.getElementById("prompt-guidance");
    if (promptGuidance) {
      promptGuidance.addEventListener("input", () => {
        if (this.currentCharacter) this.currentCharacter.imageGuidance = promptGuidance.value;
      });
    }

    // API status & settings
    const apiStatus = document.getElementById("api-status");
    apiStatus.addEventListener("click", () => this.handleAPIConfig());
    apiStatus.style.cursor = "pointer";

    document.querySelectorAll("#image-style, #creator-name")
      .forEach((input) => input.addEventListener("change", () => this.saveAPISettings()));
      
    document.getElementById("clear-config-btn").addEventListener("click", () => this.handleClearConfig());
    document.getElementById("backup-config-btn").addEventListener("click", () => this.handleBackupConfig());
    document.getElementById("restore-config-btn").addEventListener("click", () => document.getElementById("restore-config-file").click());
    document.getElementById("restore-config-file").addEventListener("change", (e) => this.handleRestoreConfig(e));
    document.getElementById("test-connection-btn").addEventListener("click", () => this.handleTestConnection());

    // Card type: force POV to third-person and disable it for group/scenario
    const cardTypeSelect = document.getElementById("card-type-select");
    const povSelect = document.getElementById("pov-select");
    const applyCardTypeConstraints = () => {
      const cardType = cardTypeSelect?.value;
      const nameLabel = document.querySelector('label[for="character-name"]');
      const nameInput = document.getElementById("character-name");
      if (cardType === "group" || cardType === "scenario") {
        if (povSelect) { povSelect.value = "third"; povSelect.disabled = true; }
        if (nameLabel) nameLabel.textContent = cardType === "group" ? "Group Name (optional)" : "Scenario Title (optional)";
        if (nameInput) nameInput.placeholder = cardType === "group" ? "e.g. The Red Cobras" : "e.g. The Haunted Lighthouse";
      } else {
        if (povSelect) povSelect.disabled = false;
        if (nameLabel) nameLabel.textContent = "Character Name (optional)";
        if (nameInput) nameInput.placeholder = "";
      }
    };
    if (cardTypeSelect) cardTypeSelect.addEventListener("change", applyCardTypeConstraints);

    // Ctrl+Enter to generate
    document.getElementById("character-concept").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.ctrlKey) { e.preventDefault(); this.handleGenerate(); }
    });

    // Image generation toggle
    const enableImageGenerationToggle = document.getElementById("enable-image-generation");
    if (enableImageGenerationToggle) {
      enableImageGenerationToggle.addEventListener("change", (e) => {
        this.config.loadFromForm();
        this.config.saveConfig();
        console.log(`Image generation ${e.target.checked ? "enabled" : "disabled"}`);
      });
    }

    // Cultural default — save immediately on change, like the toggles above, so
    // the next generation picks it up without needing the Save button.
    const characterCultureSelect = document.getElementById("character-culture");
    if (characterCultureSelect) {
      characterCultureSelect.addEventListener("change", (e) => {
        this.config.loadFromForm();
        this.config.saveConfig();
        console.log(`Character cultural default: ${e.target.value}`);
      });
    }

    // API Settings Modal
    const apiSettingsBtn = document.getElementById("api-settings-btn");
    const modalOverlay = document.getElementById("api-settings-modal");
    const modalCloseBtn = document.getElementById("modal-close-btn");
    apiSettingsBtn.addEventListener("click", () => { modalOverlay.classList.add("show"); document.body.style.overflow = "hidden"; });
    const closeModal = () => { modalOverlay.classList.remove("show"); document.body.style.overflow = ""; };
    modalCloseBtn.addEventListener("click", closeModal);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && modalOverlay.classList.contains("show")) closeModal(); });
    modalOverlay.addEventListener("click", (e) => { if (e.target === modalOverlay) closeModal(); });

    // Library lists
    const promptList = document.getElementById("stored-prompts-list");
    const cardList = document.getElementById("stored-cards-list");
    const historyList = document.getElementById("history-cards-list");
    if (promptList) promptList.addEventListener("click", (event) => this.handleLibraryPromptClick(event));
    if (cardList) cardList.addEventListener("click", (event) => this.handleLibraryCardClick(event));
    if (historyList) historyList.addEventListener("click", (event) => this.handleLibraryCardClick(event));

    const clearHistoryBtn = document.getElementById("clear-history-btn");
    if (clearHistoryBtn) clearHistoryBtn.addEventListener("click", () => this.handleClearHistory());
    const clearPromptsBtn = document.getElementById("clear-prompts-btn");
    if (clearPromptsBtn) clearPromptsBtn.addEventListener("click", () => this.handleClearPrompts());
    const galleryCardsBtn = document.getElementById("gallery-cards-btn");
    if (galleryCardsBtn) galleryCardsBtn.addEventListener("click", () => this.handleOpenLibraryGallery());

    // Tags
    const autoTagBtn = document.getElementById("auto-tag-btn");
    if (autoTagBtn) autoTagBtn.addEventListener("click", () => this.handleAutoTag());
    this.initTagInput();

    // Example messages
    const exampleMessagesCount = document.getElementById("example-messages-count");
    const regenerateExamplesBtn = document.getElementById("regenerate-examples-btn");
    const regenerateNameBtn = document.getElementById("regenerate-name-btn");
    if (exampleMessagesCount) exampleMessagesCount.addEventListener("change", () => this.handleGenerateExampleMessages());
    if (regenerateExamplesBtn) regenerateExamplesBtn.addEventListener("click", () => this.handleGenerateExampleMessages());
    if (regenerateNameBtn) regenerateNameBtn.addEventListener("click", () => this.handleOpenNamePicker());

    const regenDescBtn = document.getElementById("regenerate-description-btn");
    const regenPersBtn = document.getElementById("regenerate-personality-btn");
    const regenScenBtn = document.getElementById("regenerate-scenario-btn");
    const regenFirstMsgBtn = document.getElementById("regenerate-first-message-btn");
    const regenPostHistoryBtn = document.getElementById("regenerate-post-history-btn");
    const regenCreatorNotesBtn = document.getElementById("regenerate-creator-notes-btn");
    if (regenDescBtn) regenDescBtn.addEventListener("click", () => this.handleRegenerateField("description"));
    if (regenPersBtn) regenPersBtn.addEventListener("click", () => this.handleRegenerateField("personality"));
    if (regenScenBtn) regenScenBtn.addEventListener("click", () => this.handleRegenerateField("scenario"));
    if (regenFirstMsgBtn) regenFirstMsgBtn.addEventListener("click", () => this.handleRegenerateField("firstMessage"));
    if (regenPostHistoryBtn) regenPostHistoryBtn.addEventListener("click", () => this.handleRegeneratePostHistory());
    if (regenCreatorNotesBtn) regenCreatorNotesBtn.addEventListener("click", () => this.handleRegenerateCreatorNotes());

    // Lorebook Manager
    const manageLorebookBtn = document.getElementById("manage-lorebook-btn");
    const lorebookModal = document.getElementById("lorebook-manager-modal");
    const lorebookModalCloseBtn = document.getElementById("lorebook-modal-close-btn");
    const suggestTopicsBtn = document.getElementById("suggest-lorebook-topics-btn");
    const generateContentBtn = document.getElementById("generate-lorebook-content-btn");
    const updateContentBtn = document.getElementById("update-lorebook-content-btn");
    const saveEntryBtn = document.getElementById("save-lorebook-entry-btn");
    const cancelEditBtn = document.getElementById("cancel-lorebook-edit-btn");
    const downloadLorebookBtn = document.getElementById("download-lorebook-btn");
    const injectLorebookBtn = document.getElementById("inject-lorebook-btn");
    const entriesList = document.getElementById("lorebook-entries-list");
    const topicSuggestions = document.getElementById("lorebook-topic-suggestions");
    const lorebookSummary = document.getElementById("lorebook-summary");

    if (manageLorebookBtn) manageLorebookBtn.addEventListener("click", () => this.openLorebookManager());
    if (lorebookModalCloseBtn) lorebookModalCloseBtn.addEventListener("click", () => this.closeLorebookManager());
    if (lorebookModal) lorebookModal.addEventListener("click", (e) => { if (e.target === lorebookModal) this.closeLorebookManager(); });
    if (suggestTopicsBtn) suggestTopicsBtn.addEventListener("click", () => this.handleSuggestLorebookTopics());
    if (generateContentBtn) generateContentBtn.addEventListener("click", () => this.handleGenerateLorebookContent());
    if (updateContentBtn) updateContentBtn.addEventListener("click", () => this.handleUpdateLorebookContent());
    if (saveEntryBtn) saveEntryBtn.addEventListener("click", () => this.handleSaveLorebookEntry());
    if (cancelEditBtn) cancelEditBtn.addEventListener("click", () => this.resetLorebookEditor());
    if (downloadLorebookBtn) downloadLorebookBtn.addEventListener("click", () => this.handleDownloadLorebook());
    if (injectLorebookBtn) injectLorebookBtn.addEventListener("click", () => this.handleInjectLorebookKeys());
    if (entriesList) entriesList.addEventListener("click", (e) => {
      const target = e.target.closest("button[data-action]");
      if (!target) return;
      if (target.dataset.action === "edit-lorebook-entry") this.handleEditLorebookEntry(target.dataset.id);
      if (target.dataset.action === "delete-lorebook-entry") this.handleDeleteLorebookEntry(target.dataset.id);
    });
    if (lorebookSummary) lorebookSummary.addEventListener("click", (e) => {
      const target = e.target.closest("div[data-action='edit-lorebook-entry']");
      if (!target) return;
      this.openLorebookManager();
      // Need a slight delay to ensure the modal is visible and DOM is updated
      setTimeout(() => {
        this.handleEditLorebookEntry(target.dataset.id);
      }, 50);
    });
    if (topicSuggestions) topicSuggestions.addEventListener("click", (e) => {
      const target = e.target.closest("button.topic-suggestion");
      if (!target) return;
      const keysInput = document.getElementById("lorebook-entry-keys");
      keysInput.value = target.textContent;
      keysInput.focus();
    });

    // Supporting Cast
    const supportingCastBtn = document.getElementById("supporting-cast-btn");
    const supportingCastModal = document.getElementById("supporting-cast-modal");
    const supportingCastCloseBtn = document.getElementById("supporting-cast-close-btn");
    const addCastMemberBtn = document.getElementById("add-cast-member-btn");
    const generateCastBtn = document.getElementById("generate-cast-btn");

    if (supportingCastBtn) supportingCastBtn.addEventListener("click", () => this.openSupportingCastModal());
    if (supportingCastCloseBtn) supportingCastCloseBtn.addEventListener("click", () => this.closeSupportingCastModal());
    if (supportingCastModal) supportingCastModal.addEventListener("click", (e) => { if (e.target === supportingCastModal) this.closeSupportingCastModal(); });
    if (addCastMemberBtn) addCastMemberBtn.addEventListener("click", () => this.addSupportingCastRow());
    if (generateCastBtn) generateCastBtn.addEventListener("click", () => this.handleGenerateSupportingCast());


    const activeImageModelSelect = document.getElementById("active-image-model");
    if (activeImageModelSelect) {
      activeImageModelSelect.addEventListener("change", (e) => this.config.set("api.image.model", e.target.value));
    }

    // Alt Greetings Manager
    const manageAltGreetingsBtn = document.getElementById("manage-alt-greetings-btn");
    const altGreetingsModal = document.getElementById("alt-greetings-manager-modal");
    const altGreetingsModalCloseBtn = document.getElementById("alt-greetings-modal-close-btn");
    const generateAltContBtn = document.getElementById("generate-alt-greeting-cont-btn");
    const generateAltRandBtn = document.getElementById("generate-alt-greeting-rand-btn");
    const saveAltGreetingBtn = document.getElementById("save-alt-greeting-btn");
    const cancelAltEditBtn = document.getElementById("cancel-alt-greeting-edit-btn");
    const altGreetingsList = document.getElementById("alt-greetings-list");

    if (manageAltGreetingsBtn) manageAltGreetingsBtn.addEventListener("click", () => this.openAltGreetingsManager());
    if (altGreetingsModalCloseBtn) altGreetingsModalCloseBtn.addEventListener("click", () => this.closeAltGreetingsManager());
    if (altGreetingsModal) altGreetingsModal.addEventListener("click", (e) => { if (e.target === altGreetingsModal) this.closeAltGreetingsManager(); });
    if (generateAltContBtn) generateAltContBtn.addEventListener("click", () => this.handleGenerateAltGreeting("continuation"));
    if (generateAltRandBtn) generateAltRandBtn.addEventListener("click", () => this.handleGenerateAltGreeting("random"));
    if (saveAltGreetingBtn) saveAltGreetingBtn.addEventListener("click", () => this.handleSaveAltGreeting());
    if (cancelAltEditBtn) cancelAltEditBtn.addEventListener("click", () => this.resetAltGreetingsEditor());
    if (altGreetingsList) altGreetingsList.addEventListener("click", (e) => {
      const target = e.target.closest("button[data-action]");
      if (!target) return;
      if (target.dataset.action === "edit-alt-greeting") this.handleEditAltGreeting(target.dataset.id);
      if (target.dataset.action === "delete-alt-greeting") this.handleDeleteAltGreeting(target.dataset.id);
    });

    // Consistency Check
    const checkConsistencyBtn = document.getElementById("check-consistency-btn");
    const consistencyModal = document.getElementById("consistency-report-modal");
    const consistencyModalCloseBtn = document.getElementById("consistency-modal-close-btn");
    const consistencyAutoFixBtn = document.getElementById("consistency-auto-fix-btn");
    if (checkConsistencyBtn) checkConsistencyBtn.addEventListener("click", () => this.handleCheckConsistency());
    if (consistencyModalCloseBtn) consistencyModalCloseBtn.addEventListener("click", () => this.closeConsistencyModal());
    if (consistencyModal) consistencyModal.addEventListener("click", (e) => { if (e.target === consistencyModal) this.closeConsistencyModal(); });
    if (consistencyAutoFixBtn) consistencyAutoFixBtn.addEventListener("click", () => this.handleConsistencyAutoFix());



    // Drag-and-drop import anywhere on the page
    let dragCounter = 0;
    const dropOverlay = document.getElementById("drop-overlay");

    document.addEventListener("dragenter", (e) => {
      if (!e.dataTransfer || !e.dataTransfer.types.includes("Files")) return;
      dragCounter++;
      if (dragCounter === 1) dropOverlay.classList.add("show");
    });

    document.addEventListener("dragleave", () => {
      dragCounter = Math.max(0, dragCounter - 1);
      if (dragCounter === 0) dropOverlay.classList.remove("show");
    });

    document.addEventListener("dragover", (e) => {
      if (!e.dataTransfer || !e.dataTransfer.types.includes("Files")) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
    });

    document.addEventListener("drop", (e) => {
      e.preventDefault();
      dragCounter = 0;
      dropOverlay.classList.remove("show");
      const file = e.dataTransfer.files[0];
      if (!file) return;
      const isPng = file.type === "image/png" || file.name.toLowerCase().endsWith(".png");
      const isJson = file.type === "application/json" || file.name.toLowerCase().endsWith(".json");
      if (!isPng && !isJson) {
        this.showNotification("Only .png or .json character cards can be dropped here", "error");
        return;
      }
      this.showDropImportModal(file);
    });
    
    // Inject API Logs UI
    if (typeof this.injectTechLogsUI === 'function') {
        this.injectTechLogsUI();
    }

    // Init Review Tray
    if (typeof this._initReviewTray === 'function') {
        this._initReviewTray();
    }
  }

  saveAPISettings() {
    this.config.loadFromForm();
    this.config.saveConfig();
    // Every image-model dropdown, not just the active-model one: the Playground's
    // three lists and Settings' Edit Model all filter on the per-model capability
    // marks, so any settings save has to re-filter them.
    this.refreshImageModelDropdowns();
    this.checkAPIStatus();
  }

  /* ── Theme Toggle ─────────────────────────────────────────────────────── */

  async handleBackupConfig() {
    this.config.saveToForm();
    this.config.saveConfig();
    
    const backupData = { cardgen: this.config.config };

    const dataStr = "data:text/json;charset=utf-8," + encodeURIComponent(JSON.stringify(backupData, null, 2));
    const dlAnchorElem = document.createElement('a');
    dlAnchorElem.setAttribute("href", dataStr);
    dlAnchorElem.setAttribute("download", "card-studio-settings.json");
    dlAnchorElem.click();
    this.showNotification("Configuration backed up successfully!", "success");
  }

  handleRestoreConfig(e) {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async (event) => {
      try {
        const importedConfig = JSON.parse(event.target.result);
        
        let cardgenConfig = importedConfig;

        
        // Detect new backup format with separated configs
        if (importedConfig.cardgen !== undefined) {
            cardgenConfig = importedConfig.cardgen;

        }

        // Any JSON file would otherwise be merged in as if it were settings.
        if (!this.config.isObject(cardgenConfig) || !this.config.isObject(cardgenConfig.api)) {
            this.showNotification("That file isn't a Card Studio settings export.", "error");
            e.target.value = "";
            return;
        }

        this.config.config = this.config.deepMerge(this.config.config, cardgenConfig);
        // Awaited: the reload below reads the server copy, which overrides
        // localStorage. Reloading before this POST lands brings the old config
        // straight back.
        const configSaved = await this.config.saveConfig();

        if (!configSaved) {
            this.showNotification("Restore failed: the server didn't save the configuration. Nothing was reloaded.", "error");
            e.target.value = "";
            return;
        }

        this.showNotification("Configuration restored. Reloading...", "success");
        setTimeout(() => window.location.reload(), 1000);
      } catch (error) {
        this.showNotification("Failed to parse configuration file.", "error");
        console.error("Config Import Error:", error);
      }
      e.target.value = ""; // Reset input
    };
    reader.readAsText(file);
  }

  async handleGenerate() {
    if (this.isGenerating) return;

    this.saveAPISettings();

    const errors = this.config.validateConfig();
    if (errors.length > 0) {
      this.showNotification(errors.join(" "), "error");
      return;
    }

    const generationMode = document.querySelector('input[name="generation-mode"]:checked')?.value || "classic";
    const concept = document.getElementById("character-concept").value.trim();
    const characterName = document.getElementById("character-name").value.trim();
    const referenceImageDescription = document.getElementById("reference-image-description")?.value?.trim();

    if (generationMode === "inspire") {
      return this.handleInspireGenerate();
    }

    if (generationMode === "classic" && !concept && !referenceImageDescription) {
      this.showNotification("Please enter a character concept or reference image description", "warning");
      return;
    }

    this.isGenerating = true;
    this.setGeneratingState(true);

    // Generating a fresh character — clear any ST source link
    this.stSourceAvatar = null;

    this.hideResultSection();
    this.currentImageUrl = null;
    if (window.apiHandler) window.apiHandler.lastGeneratedImagePrompt = null;

    const customPromptTextarea = document.getElementById("custom-image-prompt");
    if (customPromptTextarea) { customPromptTextarea.value = ""; window.updatePromptCharCount(); }

    ["description-prompt", "personality-prompt", "scenario-prompt", "first-message-prompt", "example-messages-prompt"]
      .forEach((id) => { const el = document.getElementById(id); if (el) el.value = ""; });

    const guidanceInput = document.getElementById("prompt-guidance");
    if (guidanceInput) guidanceInput.value = "";

    const imageContent = document.getElementById("image-content");
    if (imageContent) {
      imageContent.innerHTML = `<div class="image-placeholder"><div class="loading-spinner"></div></div>`;
    }

    if (typeof this._resetCharacterMediaState === "function") {
      this._resetCharacterMediaState();
    } else {
      this.currentImageUrl = null;
      this.imageHistoryUrls = [];
    }
    if (typeof this._clearCharacterGallery === "function") {
      this._clearCharacterGallery();
    }

    this.lorebookEntries = [];
    this.updateLorebookEntryCount();
    this.altGreetings = [];
    this.updateAltGreetingsCount();
    this.clearStream();

    try {
      const pov = document.getElementById("pov-select").value;
      const cardType = document.getElementById("card-type-select")?.value || "single";
      let effectiveConcept = concept;
      if (generationMode === "classic") {
        if (concept && referenceImageDescription) {
          effectiveConcept += `\n\nReference appearance guidance:\n${referenceImageDescription}`;
        } else if (!concept && referenceImageDescription) {
          effectiveConcept = `Create a complete, original character based on and inspired by this visual reference description:\n${referenceImageDescription}`;
        }
      }

      const saveConcept = generationMode === "classic" ? (concept || referenceImageDescription) : searchQuery;
      const promptSaved = await this.savePromptToLibrary({
        concept: saveConcept, characterName, pov, cardType,
        lorebookData: this.lorebookData,
        referenceImageDescription: referenceImageDescription || "",
        referenceImageDataUrl: this.referenceImageDataUrl || "",
      });
      await this.refreshLibraryViews();
      if (!promptSaved) this.showStreamMessage("Prompt could not be saved to local library.\n");

      this.showStreamMessage("Starting character generation...\n\n");
      this.currentCharacter = await this.characterGenerator.generateCharacter(
        effectiveConcept, characterName,
        (token, fullContent) => this.handleCharacterStream(token, fullContent),
        pov, this.lorebookData, cardType,
      );

      // Stamp cardType on the character and auto-tag non-single types
      this.currentCharacter.cardType = cardType;
      if (cardType === "group" || cardType === "scenario") {
        if (!Array.isArray(this.currentCharacter.tags)) this.currentCharacter.tags = [];
        if (!this.currentCharacter.tags.includes(cardType)) this.currentCharacter.tags.push(cardType);
      }

      this.showStreamMessage("\n\nGenerating example messages...\n");
      // The player's own words, carried into the follow-up steps so play-style
      // requirements survive even if the card itself didn't capture them.
      const brief = concept;
      await this.handleGenerateExampleMessages(true, brief);

      this.showStreamMessage("Generating creator's notes...\n");
      try {
        const notes = await this.apiHandler.generateCreatorNotes(this.currentCharacter, "", brief);
        if (notes) {
          this.currentCharacter.creatorNotes = notes;
        }
      } catch (notesError) {
        console.warn("Creator notes generation failed (non-fatal):", notesError);
      }

      this.showStreamMessage("Generating post-history instructions...\n");
      try {
        const postHistory = this.currentCharacter.postHistoryInstructions || await this.apiHandler.generatePostHistoryInstructions(this.currentCharacter, "", brief);
        if (postHistory) {
          this.currentCharacter.postHistoryInstructions = postHistory;
        }
      } catch (phError) {
        console.warn("Post-History Instructions generation failed (non-fatal):", phError);
      }

      this.originalCharacter = JSON.parse(JSON.stringify(this.currentCharacter));

      // Auto-generate supporting cast if requested
      const autoGenerateCast = document.getElementById("auto-generate-cast")?.checked;
      if (autoGenerateCast) {
        this.showStreamMessage("\n\nAuto-generating supporting cast...\n");
        try {
          await this.handleAutoGenerateSupportingCast();
          this.showStreamMessage("Supporting cast generated and injected!\n");
        } catch (castError) {
          console.error("Auto-generate cast failed:", castError);
          this.showStreamMessage(`Supporting cast generation failed: ${castError.message}\n`);
        }
      }

      this.showStreamMessage("\nCharacter generation complete!\n");
      this.displayCharacter();

      const imageReady = this.config.engineReady("image");
      const enableImageGeneration = this.config.get("app.enableImageGeneration");
      const hasReferenceImage = !!this.referenceImageDataUrl;

      if (hasReferenceImage) {
        this.currentImageUrl = this.referenceImageDataUrl;
        document.getElementById("image-content").innerHTML = `
          <div class="image-container">
            <img src="${this.currentImageUrl}" alt="${this.currentCharacter.name || "Reference image"}" class="generated-image">
          </div>`;
        this.showStreamMessage("Using uploaded reference image as final card image (skipped image API generation)\n");
      } else if (imageReady && enableImageGeneration) {
        try {
          this.showStreamMessage("Generating character image...\n");
          
          // Apply sensible defaults for a brand new character
          // First model marked Generate, not just the first model in the
          // list — this is a text-to-image portrait, and an image-to-image
          // model here produces a portrait with no source image to work from.
          const defaultImageModel = firstImageModelWithCapability("generate", this.config);
          if (defaultImageModel) {
            this.config.set("api.image.model", defaultImageModel);
            const activeImageModelSelect = document.getElementById("active-image-model");
            if (activeImageModelSelect) activeImageModelSelect.value = defaultImageModel;
          }
          this.config.set("api.image.style", "realistic");
          const styleSelect = document.getElementById("image-style");
          if (styleSelect) styleSelect.value = "realistic";
          
          await this.generateImage(true);
          this.showStreamMessage("Image generation complete!\n");
        } catch (imageError) {
          console.error("Image generation error:", imageError);
          this.showStreamMessage(`Image generation failed: ${imageError.message}\n`);
          this.showStreamMessage("Continuing with character data only...\n");
          document.getElementById("image-content").innerHTML = `
            <div style="text-align:center;padding:2rem;color:var(--text-secondary);">
              <p>Image generation failed</p>
              <p style="font-size:0.875rem;margin-top:0.5rem;color:var(--error);">${imageError.message}</p>
              <p style="font-size:0.875rem;margin-top:0.5rem;">You can upload your own image</p>
            </div>`;
        }
      } else {
        this.showStreamMessage("Skipping image generation (image generation disabled or no API configured)\n");
        document.getElementById("image-content").innerHTML = `
          <div style="text-align:center;padding:2rem;color:var(--text-secondary);">
            <p style="font-weight:500;margin-bottom:0.5rem;">Image Generation Disabled</p>
            <p style="font-size:0.875rem;margin-bottom:1rem;">Enable image generation in settings or upload your own image</p>
            <button onclick="document.getElementById('upload-image-btn').click()" style="padding:0.5rem 1rem;background:var(--accent);color:white;border:none;border-radius:0.375rem;cursor:pointer;">
              Upload Image
            </button>
          </div>`;
      }

      this.showResultSection();
      document.getElementById("image-controls").style.display = "block";
      document.getElementById("image-buttons-row").style.display = "";

      if (imageReady) {
        const promptEditor = document.getElementById("image-prompt-editor");
        const customPromptTextarea = document.getElementById("custom-image-prompt");
        const referenceDescription = document.getElementById("reference-image-description")?.value?.trim();

        if (promptEditor) {
          promptEditor.style.display = "block";

          if (customPromptTextarea && referenceDescription && !customPromptTextarea.value.trim()) {
            customPromptTextarea.value = `Character portrait of ${this.currentCharacter.name || "the character"}, based on this reference description: ${referenceDescription}. High quality, detailed features, cinematic lighting, coherent anatomy, expressive face, fitting background.`;
            this.currentCharacter.imagePrompt = customPromptTextarea.value;
            window.updatePromptCharCount();
          }

          if (customPromptTextarea && window.apiHandler.lastGeneratedImagePrompt) {
            customPromptTextarea.value = window.apiHandler.lastGeneratedImagePrompt;
            this.currentCharacter.imagePrompt = window.apiHandler.lastGeneratedImagePrompt;
            window.updatePromptCharCount();
          } else if (!hasReferenceImage && customPromptTextarea && !customPromptTextarea.value.trim()) {
            try {
              customPromptTextarea.value = await window.apiHandler.generateImagePrompt(
                this.currentCharacter.description, this.currentCharacter.name,
              );
            } catch (error) {
              console.error("Failed to generate image prompt:", error);
              customPromptTextarea.value = window.apiHandler.buildDirectImagePrompt(
                this.currentCharacter.description, this.currentCharacter.name,
              );
            }
            this.currentCharacter.imagePrompt = customPromptTextarea.value;
            window.updatePromptCharCount();
          }
        }
      }

      await this.saveCardToLibrary();
      await this.refreshLibraryViews();

      this.showNotification("Character generated successfully!", "success");
    } catch (error) {
      console.error("Generation error:", error);
      if (error.message.includes("Generation stopped by user")) {
        this.showStreamMessage(`\nGeneration stopped.\n`);
      } else {
        this.showStreamMessage(`Error: ${error.message}\n`);
        this.showNotification(`Generation failed: ${error.message}`, "error");
      }
      this.hideResultSection();
    } finally {
      this.isGenerating = false;
      this.setGeneratingState(false);
      const inputSectionDetails = document.getElementById("input-section-details");
      if (inputSectionDetails) inputSectionDetails.open = false;
    }
  }

  syncCardEditor() {
    if (!this.currentCharacter) throw new Error("No character to save or export");
    const fields = { 'character-generated-name': 'name', 'character-description': 'description',
      'character-personality': 'personality', 'character-scenario': 'scenario',
      'character-first-message': 'firstMessage', 'character-post-history': 'postHistoryInstructions',
      'creator-notes': 'creatorNotes' };
    for (const [id, key] of Object.entries(fields)) {
      const el = document.getElementById(id);
      if (el) this.currentCharacter[key] = el.value.trim();
    }
    const examples = document.getElementById('example-messages-output');
    if (examples && ('value' in examples || examples.isContentEditable)) {
      this.currentCharacter.mesExample = (examples.value ?? examples.innerText).trim();
    }
    this.currentCharacter.creator ||= this.config.get('app.creator') || '';
    this.currentCharacter.character_book = this.buildCharacterBook();
    this.syncAltGreetingsToCharacter();
    return this.characterGenerator.toSpecV2Format(this.currentCharacter);
  }

  handleDownloadJSON() {
    try {
      const card = this.syncCardEditor();
      const name = card.data.name.replace(/[^a-zA-Z0-9_-]+/g, '_') || 'character';
      this.downloadBlob(new Blob([JSON.stringify(card, null, 2)], { type: 'application/json' }), `${name}.json`);
      this.showNotification('Character Card V2 JSON exported.', 'success');
    } catch (error) { this.showNotification(error.message, 'error'); }
  }

  async handleDownload() {
    if (!this.currentCharacter || !this.currentImageUrl) {
      this.showNotification("PNG export needs a portrait. JSON export does not.", "warning");
      return;
    }
    try {
      const card = this.syncCardEditor();
      let imageBlob = await this.imageGenerator.convertToBlob(this.currentImageUrl);
      imageBlob = await this.imageGenerator.optimizeImageForCard(imageBlob);

      const cardBlob = await this.pngEncoder.createCharacterCard(imageBlob, card);
      this.pngEncoder.downloadCharacterCard(cardBlob, this.currentCharacter.name);

      const finalSize = this.imageGenerator.formatFileSize(cardBlob.size);
      this.showNotification(`Character card downloaded! Size: ${finalSize}`, "success");
      await this.saveCardToLibrary();
      await this.refreshLibraryViews();
    } catch (error) {
      console.error("Download error:", error);
      this.showNotification(`Download failed: ${error.message}`, "error");
    }
  }

  handleStop() {
    // Stop batch generation if it's running
    if (this._batchIsGenerating) {
      this.handleBatchStop();
      return;
    }
    if (this.isGenerating) {
      this.showStreamMessage("\n\nStopping generation...\n");
      window.apiHandler.stopGeneration();
      this.isGenerating = false;
      this.setGeneratingState(false);
      this.showNotification("Generation stopped by user", "warning");
    }
  }

  handleStopRevision() {
    if (this.isRevising) {
      window.apiHandler.stopGeneration();
      this.isRevising = false;
      this.setRevisionState(false);
      this.showNotification("Revision stopped by user", "warning");
    }
  }

  async handleRemasterCurrentCard() {
    if (!this.currentCharacter) {
      this.showNotification("No card loaded to remaster", "warning");
      return;
    }
    this.showNotification("Starting AI remaster...", "info");
    await this.handleAutoRemaster();
  }
}

// Update prompt character counter
window.updatePromptCharCount = function () {
  const textarea = document.getElementById("custom-image-prompt");
  const counter = document.getElementById("prompt-char-count");
  if (textarea && counter) {
    const length = textarea.value.length;
    counter.textContent = `${length}/1000`;
    counter.style.color = length >= 950 ? "#f59e0b" : "#9ca3af";
  }
};

// Bootstrap — wait for DOM and all modules
document.addEventListener("DOMContentLoaded", async () => {
  await new Promise((resolve) => setTimeout(resolve, 100));

  if (!window.config || !window.apiHandler || !window.characterGenerator || !window.imageGenerator || !window.pngEncoder) {
    console.error("Missing modules:", {
      config: !!window.config, apiHandler: !!window.apiHandler,
      characterGenerator: !!window.characterGenerator, imageGenerator: !!window.imageGenerator, pngEncoder: !!window.pngEncoder,
    });
    return;
  }

  // Initialise auth — if already authenticated this resolves immediately
  // and shows the app; otherwise the login overlay stays visible and
  // onAuthSuccess is called after a successful login.
  async function startApp() {
    if (!window.app) {
      // Reload server config now that we have a valid token so the
      // shared API keys/settings are available to this user.
      if (window.config) {
        await window.config.loadConfig().catch(() => {});
        window.config.saveToForm();
      }
      window.app = new CharacterGeneratorApp();
    }
  }

  window.onAuthSuccess = startApp;

  const authenticated = await window.cardgenAuth.initAuth();
  if (authenticated) {
    startApp();
  }

  const style = document.createElement("style");
  style.textContent = `
        .tags { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.5rem; }
        .tag { background: var(--bg-tertiary); color: var(--text-secondary); padding: 0.25rem 0.75rem; border-radius: 20px; font-size: 0.875rem; font-weight: 500; }
        .character-section { margin-bottom: 1.5rem; }
        .character-section strong { color: var(--text-primary); display: block; margin-bottom: 0.5rem; }
        .image-container { text-align: center; }
        .generated-image { max-width: 100%; height: auto; border-radius: var(--radius); box-shadow: var(--shadow-sm); }
        .form-section { background: var(--bg-tertiary); padding: 1rem; border-radius: calc(var(--radius) / 2); margin-bottom: 1rem; }
        .form-section-title { font-weight: 600; margin-bottom: 1rem; color: var(--text-primary); }

        /* Dark mode button fixes */
        [data-theme="dark"] button:not(.btn-primary):not(.btn-secondary):not(.btn-stop):not([style*="background: none"]):not([style*="background:none"]) {
            background: var(--surface, #1c2128);
            color: var(--text-primary, #c9d1d9);
            border: 1px solid var(--border-strong, #4b5563);
        }
        [data-theme="dark"] button:not(.btn-primary):not(.btn-secondary):not(.btn-stop):not([style*="background: none"]):not([style*="background:none"]):hover {
            background: var(--surface-muted, #111820);
        }

          /* Form inputs need explicit color assignment to avoid black text on dark backgrounds */
          input, textarea, select, .content-box, .input {
              color: var(--text-primary);
          }

          /* Global Mobile Layout Fixes */
          /* overflow-x: clip (not hidden) — hidden turns html/body into a scroll
             container, which breaks position: sticky descendants in WebKit/Safari
             (e.g. the story mode button bar just scrolls away instead of sticking). */
          html, body { max-width: 100vw; overflow-x: clip; margin: 0; padding: 0; }
          *, *::before, *::after { box-sizing: border-box; }
          .container, .main, #app-root, #view-cardgen { max-width: 100%; }
          
          @media (max-width: 768px) {
              /* Force all elements inside the generator to respect screen width */
              #view-cardgen * {
                  max-width: 100% !important;
              }
              
              /* Ensure main sections can shrink and don't push off-screen */
              #view-cardgen, #view-cardgen .form-section, #view-cardgen .result-section, #view-cardgen .content-box {
                  width: 100% !important;
                  min-width: 0 !important;
                  box-sizing: border-box !important;
              }
              
              #view-cardgen textarea, #view-cardgen input, #view-cardgen select {
                  width: 100%;
              }
              
              #view-cardgen img, #view-cardgen video, #view-cardgen canvas {
                  height: auto;
              }
              
              /* Break long unbroken strings */
              #view-cardgen p, #view-cardgen div, #view-cardgen span, #view-cardgen pre, #view-cardgen code, #view-cardgen button, #view-cardgen label {
                  overflow-wrap: break-word;
                  word-wrap: break-word;
              }
              
              /* Stack flex containers that are side-by-side on desktop */
              #view-cardgen div[style*="display: flex"], #view-cardgen div[style*="display:flex"] {
                  flex-wrap: wrap !important;
              }
          }
    `;
  document.head.appendChild(style);

  console.log("%cSillyTavern Character Generator", "font-size: 20px; font-weight: bold; color: #0066cc;");
  console.log("%cCreate amazing characters with AI!", "font-size: 14px; color: #666;");
  console.log("%cTip: Press Ctrl+Enter to generate a character", "font-size: 12px; color: #999;");
});
