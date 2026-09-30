import { useEffect, useState, type FormEvent } from "react";
import { api } from "../lib/api";
import type { ApiKey, PreflightReport } from "../lib/types";
import { since } from "../lib/format";
import { useStore } from "../store/useStore";
import { Sheet } from "./Sheet";
import { Button, CopyField, ErrorNote, Field } from "./primitives";

/* Two separate ways this panel used to overwrite what was being typed, and
   both are closed here.

   The first was the background poll rebuilding the DOM underneath the cursor.
   React removes that one structurally: the poll writes to the store, and no
   field below reads from the store.

   The second survives a framework and had to be designed out: the credential
   fetch resolves a moment after the sheet opens, and seeding the form from a
   response that lands after the user has started typing overwrites them just
   as effectively. So the form is not rendered until its data is in hand —
   there is no interval in which a field exists but its value is still in
   flight. */

export function Account({ open, onClose }: { open: boolean; onClose: () => void }) {
  const profile = useStore((s) => s.profile);
  const adoptToken = useStore((s) => s.adoptToken);
  const signOut = useStore((s) => s.signOut);
  const note = useStore((s) => s.note);

  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [revealed, setRevealed] = useState<string | null>(null);
  const [preflight, setPreflight] = useState<PreflightReport | null>(null);
  const [checking, setChecking] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [keyLabel, setKeyLabel] = useState("");
  const [displayTokens, setDisplayTokens] = useState<ApiKey[]>([]);
  const [displayLabel, setDisplayLabel] = useState("");
  const [displaySecret, setDisplaySecret] = useState<string | null>(null);

  const [details, setDetails] = useState({ name: "", email: "" });
  const [detailsError, setDetailsError] = useState("");
  const [detailsBusy, setDetailsBusy] = useState(false);

  /* Same reasoning as ProfileEditor: reopening the panel must not show one
     frame of the previous visit's values before the effect resets them. */
  const [renderedOpen, setRenderedOpen] = useState(open);
  if (open !== renderedOpen) {
    setRenderedOpen(open);
    if (open) setLoaded(false);
  }

  const [passwords, setPasswords] = useState({ current: "", next: "", confirm: "" });
  const [passwordError, setPasswordError] = useState("");
  const [passwordBusy, setPasswordBusy] = useState(false);

  useEffect(() => {
    // Clearing on close, not just on open, means the plaintext key cannot be
    // read back out of React state after the panel is dismissed.
    if (!open) {
      setRevealed(null);
      setKeyLabel("");
      setDisplaySecret(null);
      setDisplayLabel("");
      return;
    }
    let cancelled = false;
    setLoaded(false);
    setRevealed(null);
    setPasswords({ current: "", next: "", confirm: "" });
    setPasswordError("");
    setDetailsError("");
    (async () => {
      try {
        const [credentials, report, display] = await Promise.all([
          api.credentials(),
          api.preflight(),
          api.displayTokens(),
        ]);
        if (cancelled) return;
        setKeys(credentials.keys);
        setDisplayTokens(display.tokens);
        setDetails({
          name: credentials.profile.name,
          email: credentials.profile.email,
        });
        setPreflight(report);
        setLoaded(true);
      } catch {
        // The sheet still works for sign-out even if these fail.
      }
    })();
    return () => { cancelled = true; };
  }, [open]);

  async function generateKey() {
    try {
      const result = await api.createKey(keyLabel.trim());
      setKeys((current) => [result.key, ...current]);
      setRevealed(result.secret);
      setKeyLabel("");
      note(`Created API Key ${result.key.label}`, "good");
    } catch (cause) {
      note(cause instanceof Error ? cause.message : "Could not create a key", "bad");
    }
  }

  async function revokeKey(id: string) {
    try {
      const result = await api.revokeKey(id);
      setKeys(result.keys);
      note("API Key Revoked");
    } catch (cause) {
      note(cause instanceof Error ? cause.message : "Could not revoke that key", "bad");
    }
  }

  async function mintDisplayToken() {
    try {
      const result = await api.createDisplayToken(displayLabel.trim());
      setDisplayTokens((current) => [result.token, ...current]);
      setDisplaySecret(result.secret);
      setDisplayLabel("");
      note(`Created Display Token ${result.token.label}`, "good");
    } catch (cause) {
      note(cause instanceof Error ? cause.message : "Could not create a token", "bad");
    }
  }

  async function revokeDisplayToken(id: string) {
    try {
      const result = await api.revokeDisplayToken(id);
      setDisplayTokens(result.tokens);
      note("Display Token Revoked");
    } catch (cause) {
      note(cause instanceof Error ? cause.message : "Could not revoke it", "bad");
    }
  }

  async function recheck() {
    setChecking(true);
    try {
      setPreflight(await api.preflight());
    } finally {
      setChecking(false);
    }
  }

  async function saveDetails(event: FormEvent) {
    event.preventDefault();
    setDetailsBusy(true);
    setDetailsError("");
    try {
      const result = await api.updateProfile(details.name, details.email);
      useStore.setState({ profile: result.profile });
      note("Profile Saved", "good");
    } catch (cause) {
      setDetailsError(cause instanceof Error ? cause.message : "Could not save");
    } finally {
      setDetailsBusy(false);
    }
  }

  async function changePassword(event: FormEvent) {
    event.preventDefault();
    if (passwords.next !== passwords.confirm) {
      setPasswordError("Those passwords do not match.");
      return;
    }
    setPasswordBusy(true);
    setPasswordError("");
    try {
      const session = await api.changePassword(passwords.current, passwords.next);
      // The gateway clears every session on a password change and hands back a
      // fresh token, so this tab is not signed out by its own request.
      adoptToken(session.token);
      setPasswords({ current: "", next: "", confirm: "" });
      note("Password Changed: All sessions have been signed out.");
    } catch (cause) {
      setPasswordError(cause instanceof Error ? cause.message : "Could not change it");
    } finally {
      setPasswordBusy(false);
    }
  }

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title="Profile & API Keys"
      eyebrow={profile?.name || "Control plane"}
      size="lg"
      footer={
        <>
          <span className="foot-hint">Session ends when this tab closes.</span>
          <Button variant="danger" onClick={() => { void signOut(); }}>
            Sign out
          </Button>
        </>
      }
    >
      <section className="account-block" data-testid="api-keys">
        <div className="block-head">
          <div>
            <h3>API keys</h3>
            <small>
              Clients send these as <code>Authorization: Bearer</code>. Only a
              hash is stored, so a key is shown once.
            </small>
          </div>
        </div>

        <div className="inline-field key-mint">
          <input
            placeholder="Label — Apple Shortcuts, laptop…"
            aria-label="Label for the new API key"
            data-testid="key-label"
            value={keyLabel}
            onChange={(event) => setKeyLabel(event.target.value)}
          />
          <Button data-testid="mint-key" onClick={() => { void generateKey(); }}>
            Generate
          </Button>
        </div>

        {revealed && (
          <div className="reveal">
            <span className="eyebrow">Copy this now — it will not be shown again</span>
            <CopyField value={revealed} label="API key" />
          </div>
        )}

        <ul className="key-list">
          {keys.map((key) => (
            <li key={key.id}>
              <div>
                <strong className="key-label">{key.label || "Unlabelled"}</strong>
                <code>{key.prefix}…</code>
                <small>
                  created {since(key.created_at)} · last used {since(key.last_used_at ?? 0)}
                </small>
              </div>
              <Button
                variant="ghost"
                data-testid="revoke-key"
                disabled={keys.length < 2}
                title={keys.length < 2 ? "The last key cannot be revoked" : undefined}
                onClick={() => { void revokeKey(key.id); }}
              >
                Revoke
              </Button>
            </li>
          ))}
          {keys.length === 0 && <li className="is-empty"><span>No API keys.</span></li>}
        </ul>
      </section>

      <section className="account-block" data-testid="display-tokens">
        <div className="block-head">
          <div>
            <h3>Wall Display</h3>
            <small>
              A read-only token for a screen on a monitor.
            </small>
          </div>
        </div>

        <span className="field-label">Display URL</span>
        <CopyField value={`${window.location.origin}/display`} label="display URL" />

        {displaySecret && (
          <div className="reveal">
            <span className="eyebrow">Copy this now — it will not be shown again</span>
            <CopyField value={displaySecret} label="display token" />
          </div>
        )}

        <div className="inline-field key-mint">
          <input
            placeholder="Label — studio monitor, rack screen…"
            aria-label="Label for the new display token"
            data-testid="display-label"
            value={displayLabel}
            onChange={(event) => setDisplayLabel(event.target.value)}
          />
          <Button data-testid="mint-display" onClick={() => { void mintDisplayToken(); }}>
            Generate
          </Button>
        </div>

        <ul className="key-list">
          {displayTokens.map((token) => (
            <li key={token.id}>
              <div>
                <strong className="key-label">{token.label || "Unlabelled"}</strong>
                <code>{token.prefix}…</code>
                <small>
                  created {since(token.created_at)} · last seen{" "}
                  {since(token.last_used_at ?? 0)}
                </small>
              </div>
              <Button
                variant="ghost"
                data-testid="revoke-display"
                onClick={() => { void revokeDisplayToken(token.id); }}
              >
                Revoke
              </Button>
            </li>
          ))}
          {displayTokens.length === 0 && (
            <li className="is-empty"><span>No display paired.</span></li>
          )}
        </ul>
      </section>

      <section className="account-block">
        <div className="block-head">
          <div>
            <h3>Profile</h3>
          </div>
        </div>
        {!loaded ? (
          <div className="skeleton-rows" aria-hidden="true"><i /><i /></div>
        ) : (
        <form className="grid-2" onSubmit={saveDetails}>
          <Field label="Name">
            <input
              data-testid="profile-name"
              value={details.name}
              required
              onChange={(event) => setDetails((d) => ({ ...d, name: event.target.value }))}
            />
          </Field>
          <Field label="Email">
            <input
              type="email"
              data-testid="profile-email"
              value={details.email}
              onChange={(event) => setDetails((d) => ({ ...d, email: event.target.value }))}
            />
          </Field>
          <div className="grid-actions">
            <ErrorNote>{detailsError}</ErrorNote>
            <Button variant="primary" type="submit" busy={detailsBusy}>Save profile</Button>
          </div>
        </form>
        )}
      </section>

      <section className="account-block">
        <div className="block-head">
          <div>
            <h3>Admin Password</h3>
          </div>
        </div>
        <form className="grid-2" onSubmit={changePassword}>
          <Field label="Current password">
            <input
              type="password" autoComplete="current-password" required
              data-testid="current-password" value={passwords.current}
              onChange={(event) => setPasswords((p) => ({ ...p, current: event.target.value }))}
            />
          </Field>
          <Field label="New password">
            <input
              type="password" autoComplete="new-password" minLength={12} required
              data-testid="new-password" value={passwords.next}
              onChange={(event) => setPasswords((p) => ({ ...p, next: event.target.value }))}
            />
          </Field>
          <Field label="Confirm new password">
            <input
              type="password" autoComplete="new-password" minLength={12} required
              data-testid="confirm-password" value={passwords.confirm}
              onChange={(event) => setPasswords((p) => ({ ...p, confirm: event.target.value }))}
            />
          </Field>
          <div className="grid-actions">
            <ErrorNote>{passwordError}</ErrorNote>
            <Button variant="primary" type="submit" busy={passwordBusy}>Change password</Button>
          </div>
        </form>
      </section>

      <section className="account-block">
        <div className="block-head">
          <div>
            <h3>Dependencies Status</h3>
          </div>
          <Button busy={checking} onClick={() => { void recheck(); }}>Recheck</Button>
        </div>
        <ul className="check-list">
          {preflight?.checks.map((check) => (
            <li key={check.name} className={`check-${check.status}`}>
              <span className="check-mark" aria-hidden="true" />
              <div>
                <strong>{check.name}</strong>
                <small>{check.detail}</small>
                {check.remedy && <code>{check.remedy}</code>}
              </div>
            </li>
          ))}
        </ul>
      </section>
    </Sheet>
  );
}
