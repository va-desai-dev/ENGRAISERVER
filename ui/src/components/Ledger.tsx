import { useEffect, useState, type FormEvent } from "react";
import { api } from "../lib/api";
import type {
  DiscoveredModel, LedgerEntry, LedgerPreview, LedgerSettings, LedgerState,
} from "../lib/types";
import { useStore } from "../store/useStore";
import { Sheet } from "./Sheet";
import { Button, ErrorNote, Field } from "./primitives";

/* The ledger is what the models know about the person running this gateway:
   dated facts written here, added to chat requests on the routes switched on
   below. Pinned entries go out every time; the rest are recalled by an
   embedding model when a conversation touches them.

   As in Account, nothing is rendered until its data has arrived, so a
   response landing late can never overwrite what is being typed. */

const BLANK: LedgerEntry = {
  title: "", content: "", when: null, until: null, pinned: false, enabled: true, tags: [],
};

function message(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

function dateLabel(entry: LedgerEntry) {
  if (!entry.when) return "Undated";
  return entry.until ? `${entry.when} – ${entry.until}` : entry.when;
}

export function Ledger({ open, onClose }: { open: boolean; onClose: () => void }) {
  const routes = useStore((s) => s.snapshot?.models) ?? [];
  const note = useStore((s) => s.note);

  const [state, setState] = useState<LedgerState | null>(null);
  const [settings, setSettings] = useState<LedgerSettings | null>(null);
  const [library, setLibrary] = useState<DiscoveredModel[]>([]);
  const [draft, setDraft] = useState<LedgerEntry | null>(null);
  const [tags, setTags] = useState("");
  const [entryError, setEntryError] = useState("");
  const [settingsError, setSettingsError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState<"entry" | "settings" | "index" | "preview" | null>(null);
  const [probe, setProbe] = useState("");
  const [preview, setPreview] = useState<LedgerPreview | null>(null);

  const [renderedOpen, setRenderedOpen] = useState(open);
  if (open !== renderedOpen) {
    setRenderedOpen(open);
    if (open) {
      setState(null);
      setSettings(null);
    }
  }

  useEffect(() => {
    if (!open) {
      // A life story should not sit in memory behind a closed panel.
      setState(null);
      setSettings(null);
      setDraft(null);
      setPreview(null);
      setProbe("");
      return;
    }
    let cancelled = false;
    setLoadError("");
    void Promise.all([api.ledger(), api.discover()])
      .then(([ledger, discovered]) => {
        if (cancelled) return;
        setState(ledger);
        setSettings(ledger.settings);
        setLibrary(discovered.models);
      })
      .catch((error) => { if (!cancelled) setLoadError(message(error, "Could not load the ledger")); });
    return () => { cancelled = true; };
  }, [open]);

  /* Indexing runs in the background after a save; poll while it does so the
     counts settle without a manual refresh. Only status is adopted, never the
     form fields. */
  const indexing = state?.status.indexing ?? false;
  useEffect(() => {
    if (!open || !indexing) return;
    const timer = window.setInterval(() => {
      void api.ledger().then((ledger) => setState(ledger)).catch(() => {});
    }, 1500);
    return () => window.clearInterval(timer);
  }, [open, indexing]);

  function edit(entry: LedgerEntry) {
    setDraft({ ...entry });
    setTags(entry.tags.join(", "));
    setEntryError("");
  }

  async function saveEntry(event: FormEvent) {
    event.preventDefault();
    if (!draft) return;
    setBusy("entry");
    setEntryError("");
    try {
      const saved = await api.saveLedgerEntry({
        ...draft,
        when: draft.when?.trim() || null,
        until: draft.until?.trim() || null,
        tags: tags.split(",").map((tag) => tag.trim()).filter(Boolean),
      });
      setState(saved);
      setDraft(null);
      note(`Ledger entry saved: ${saved.entry.title}`, "good");
    } catch (error) {
      setEntryError(message(error, "Could not save the entry"));
    } finally {
      setBusy(null);
    }
  }

  async function removeEntry(entry: LedgerEntry) {
    if (!entry.id || !window.confirm(`Delete "${entry.title}" from the ledger?`)) return;
    setBusy("entry");
    try {
      setState(await api.removeLedgerEntry(entry.id));
      setDraft(null);
    } catch (error) {
      setEntryError(message(error, "Could not delete the entry"));
    } finally {
      setBusy(null);
    }
  }

  async function persist(next: LedgerSettings) {
    setBusy("settings");
    setSettingsError("");
    try {
      const saved = await api.saveLedgerSettings(next);
      setState(saved);
      setSettings(saved.settings);
      return true;
    } catch (error) {
      setSettingsError(message(error, "Could not save ledger settings"));
      return false;
    } finally {
      setBusy(null);
    }
  }

  /* Route switches save at once: they are the off switch, and an off switch
     that waits for a Save button is one that gets forgotten. */
  async function toggleRoute(id: string, on: boolean) {
    if (!settings) return;
    const enabled = new Set(settings.enabled_routes);
    if (on) enabled.add(id); else enabled.delete(id);
    const next = { ...settings, enabled_routes: [...enabled].sort() };
    setSettings(next);
    if (await persist(next)) note(`Ledger ${on ? "on" : "off"} for ${id}`, "good");
  }

  async function saveSettings(event: FormEvent) {
    event.preventDefault();
    if (settings && await persist(settings)) note("Ledger settings saved", "good");
  }

  async function reindex() {
    setBusy("index");
    try {
      setState(await api.reindexLedger(true));
    } catch (error) {
      setSettingsError(message(error, "Could not rebuild the index"));
    } finally {
      setBusy(null);
    }
  }

  async function runPreview(event: FormEvent) {
    event.preventDefault();
    if (!probe.trim()) return;
    setBusy("preview");
    try {
      setPreview(await api.previewLedger(probe));
    } catch (error) {
      setPreview({
        pinned_block: "", recalled_block: "", recalled: [],
        error: message(error, "Preview failed"),
      });
    } finally {
      setBusy(null);
    }
  }

  const set = <K extends keyof LedgerSettings>(key: K, value: LedgerSettings[K]) =>
    setSettings((current) => (current ? { ...current, [key]: value } : current));
  const status = state?.status;
  const encoders = library.filter(
    (model) => model.kind === "embedding" || model.path === settings?.embedding_model_path,
  );

  return (
    <Sheet open={open} onClose={onClose} title="Ledger" eyebrow="What the models know about you" size="lg">
      <ErrorNote>{loadError}</ErrorNote>
      {!state || !settings ? (
        !loadError && <div className="skeleton-rows" aria-hidden="true"><i /><i /><i /></div>
      ) : (
        <>
          <section className="account-block" data-testid="ledger-routes">
            <div className="block-head">
              <div>
                <h3>Routes</h3>
                <small>
                The memory journal is added to the selected chat models across all API served applications. Anyone on any chat-app will have the chat model recall entries written in here.
                </small>
              </div>
            </div>
            {routes.length === 0 ? (
              <p className="ledger-status">No text routes yet.</p>
            ) : (
              <div className="ledger-routes">
                {routes.map((route) => (
                  <label className="check-row" key={route.id}>
                    <input
                      type="checkbox"
                      data-testid={`ledger-route-${route.id}`}
                      checked={settings.enabled_routes.includes(route.id)}
                      disabled={busy === "settings"}
                      onChange={(event) => { void toggleRoute(route.id, event.target.checked); }}
                    />
                    <span>{route.name || route.id}</span>
                  </label>
                ))}
              </div>
            )}
          </section>

          <section className="account-block" data-testid="ledger-entries">
            <div className="block-head">
              <div>
                <h3>Entries</h3>
                <small>
                  {status?.pinned ?? 0} pinned (about {status?.pinned_tokens ?? 0} tokens on
                  every request) · {status?.episodes ?? 0} recalled when relevant
                </small>
              </div>
              {!draft && (
                <Button variant="primary" data-testid="ledger-new" onClick={() => edit(BLANK)}>
                  New entry
                </Button>
              )}
            </div>

            {draft && (
              <form className="ledger-editor" onSubmit={saveEntry} data-testid="ledger-editor">
                <Field label="Title">
                  <input
                    data-autofocus
                    data-testid="ledger-title"
                    value={draft.title}
                    required
                    maxLength={200}
                    placeholder="Moved to BigCollegeName"
                    onChange={(event) => setDraft({ ...draft, title: event.target.value })}
                  />
                </Field>
                <div className="grid-2">
                  <Field label="When" hint="2019, 2019-06 or 2019-06-14. Blank for an undated fact.">
                    <input
                      data-testid="ledger-when"
                      value={draft.when ?? ""}
                      placeholder="2019-08"
                      onChange={(event) => setDraft({ ...draft, when: event.target.value })}
                    />
                  </Field>
                  <Field label="Until" hint="Only for something that lasted.">
                    <input
                      value={draft.until ?? ""}
                      placeholder="2021-05"
                      onChange={(event) => setDraft({ ...draft, until: event.target.value })}
                    />
                  </Field>
                </div>
                <Field label="What happened" hint="Write it as you would want it read back: third person, concrete.">
                  <textarea
                    data-testid="ledger-content"
                    rows={5}
                    value={draft.content}
                    required
                    maxLength={8000}
                    onChange={(event) => setDraft({ ...draft, content: event.target.value })}
                  />
                </Field>
                <Field label="Tags" hint="Comma-separated; for your own filing.">
                  <input value={tags} onChange={(event) => setTags(event.target.value)} />
                </Field>
                <label className="check-row">
                  <input
                    type="checkbox"
                    data-testid="ledger-pinned"
                    checked={draft.pinned}
                    onChange={(event) => setDraft({ ...draft, pinned: event.target.checked })}
                  />
                  <span>Pinned: send on every request</span>
                </label>
                <label className="check-row">
                  <input
                    type="checkbox"
                    checked={draft.enabled}
                    onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })}
                  />
                  <span>Enabled</span>
                </label>
                <div className="grid-actions">
                  <ErrorNote>{entryError}</ErrorNote>
                  {draft.id && (
                    <Button
                      type="button"
                      variant="danger"
                      disabled={busy === "entry"}
                      onClick={() => { void removeEntry(draft); }}
                    >
                      Delete
                    </Button>
                  )}
                  <Button type="button" variant="ghost" onClick={() => setDraft(null)}>Cancel</Button>
                  <Button type="submit" variant="primary" busy={busy === "entry"} data-testid="ledger-save">
                    Save entry
                  </Button>
                </div>
              </form>
            )}

            <ul className="key-list ledger-entries">
              {state.entries.map((entry) => (
                <li key={entry.id} className={entry.enabled ? "" : "is-off"}>
                  <div>
                    <span className="ledger-when">{dateLabel(entry)}</span>
                    <strong className="key-label">
                      {entry.title}
                      {entry.pinned && <span className="ledger-tag">Pinned</span>}
                      {!entry.enabled && <span className="ledger-tag">Off</span>}
                    </strong>
                    <small className="ledger-excerpt">{entry.content}</small>
                  </div>
                  <Button variant="ghost" onClick={() => edit(entry)}>Edit</Button>
                </li>
              ))}
              {state.entries.length === 0 && (
                <li className="is-empty"><span>Nothing written yet.</span></li>
              )}
            </ul>
          </section>

          <section className="account-block" data-testid="ledger-preview">
            <div className="block-head">
              <div>
                <h3>Try It Here</h3>
                <small>Shows what a message would pull in, with similarity scores, without generating anything.</small>
              </div>
            </div>
            <form className="inline-field" onSubmit={runPreview}>
              <input
                data-testid="ledger-probe"
                value={probe}
                placeholder="When did I move to college?"
                aria-label="Message to test recall with"
                onChange={(event) => setProbe(event.target.value)}
              />
              <Button type="submit" busy={busy === "preview"}>Test</Button>
            </form>
            {preview && (
              <>
                <ErrorNote>{preview.error}</ErrorNote>
                {preview.recalled.length === 0 && !preview.error && (
                  <p className="ledger-status">No episode scored above the threshold.</p>
                )}
                {preview.recalled.length > 0 && (
                  <ul className="key-list">
                    {preview.recalled.map((hit) => (
                      <li key={hit.id}>
                        <div>
                          <strong className="key-label">{hit.title}</strong>
                          <small>{hit.when ?? "Undated"}</small>
                        </div>
                        <code>{hit.score.toFixed(3)}</code>
                      </li>
                    ))}
                  </ul>
                )}
                {preview.pinned_block && <pre className="ledger-block">{preview.pinned_block}</pre>}
                {preview.recalled_block && <pre className="ledger-block">{preview.recalled_block}</pre>}
              </>
            )}
          </section>

          <section className="account-block" data-testid="ledger-settings">
            <div className="block-head">
              <div>
                <h3>Recall</h3>
                <small>
                  {status?.indexing
                    ? "Indexing…"
                    : `${status?.indexed ?? 0} of ${status?.entries ?? 0} entries indexed`}
                  {status?.dimension ? ` · ${status.dimension} dimensions` : ""}
                  {status?.worker.running ? " · embedding worker running" : ""}
                </small>
              </div>
              <Button
                variant="ghost"
                busy={busy === "index"}
                disabled={!state.settings.embedding_model_path}
                onClick={() => { void reindex(); }}
              >
                Rebuild index
              </Button>
            </div>
            <ErrorNote>{status?.error}</ErrorNote>
            <form className="editor-form" onSubmit={saveSettings}>
              <Field label="Your name" hint="How you want the journal & chat model to refer to the subject (aka you in most cases). Blank reads as “the user.”">
                <input
                  data-testid="ledger-subject"
                  value={settings.subject_name}
                  onChange={(event) => set("subject_name", event.target.value)}
                />
              </Field>
              <Field
                label="Embedding model"
                hint="A GGUF encoder in your model folders whose filename contains “embed”. Without one, only pinned entries are sent."
              >
                <select
                  data-testid="ledger-model"
                  value={settings.embedding_model_path}
                  onChange={(event) => set("embedding_model_path", event.target.value)}
                >
                  <option value="">None (pinned entries only)</option>
                  {encoders.map((model) => (
                    <option key={model.path} value={model.path}>{model.filename}</option>
                  ))}
                </select>
              </Field>
              <Field label="Query prefix" hint="Some encoders want one, such as “task: search result | query:”">
                <input value={settings.query_prefix} onChange={(event) => set("query_prefix", event.target.value)} />
              </Field>
              <Field label="Entry prefix" hint="Such as “title: {title} | text: ”; {title} becomes the entry’s title and date. Changing it rebuilds the index.">
                <input value={settings.document_prefix} onChange={(event) => set("document_prefix", event.target.value)} />
              </Field>
              <Field label="Episodes per message" hint="The most that one message can recall.">
                <input
                  type="number" min="1" max="50" value={settings.top_k}
                  onChange={(event) => set("top_k", Number(event.target.value))}
                />
              </Field>
              <Field label="Similarity threshold" hint="Raise it if unrelated episodes appear; lower it if relevant ones are missed.">
                <input
                  type="number" min="-1" max="1" step="0.01" value={settings.min_score}
                  onChange={(event) => set("min_score", Number(event.target.value))}
                />
              </Field>
              <Field label="Recall budget (tokens)" hint="An estimate; the best matches that fit are kept.">
                <input
                  type="number" min="64" max="32768" value={settings.recall_budget_tokens}
                  onChange={(event) => set("recall_budget_tokens", Number(event.target.value))}
                />
              </Field>
              <Field label="Recall placement" hint="Use “inside your message” for models that ignore a system turn late in a chat.">
                <select
                  value={settings.recall_placement}
                  onChange={(event) => set("recall_placement", event.target.value as LedgerSettings["recall_placement"])}
                >
                  <option value="system">System turn before your message</option>
                  <option value="user">Inside your message</option>
                </select>
              </Field>
              <Field label="Encoder GPU layers" hint="0 keeps the encoder on the CPU, away from your chat model's VRAM.">
                <input
                  type="number" min="0" max="999" value={settings.embedding_gpu_layers}
                  onChange={(event) => set("embedding_gpu_layers", Number(event.target.value))}
                />
              </Field>
              <div className="grid-actions">
                <ErrorNote>{settingsError}</ErrorNote>
                <Button type="submit" variant="primary" busy={busy === "settings"} data-testid="ledger-save-settings">
                  Save settings
                </Button>
              </div>
            </form>
          </section>
        </>
      )}
    </Sheet>
  );
}
