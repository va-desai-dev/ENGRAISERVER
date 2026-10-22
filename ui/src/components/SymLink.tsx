import { useEffect, useRef, useState } from "react";
import { api, type SymLinkInvitation, type SymLinkPeer } from "../lib/api";
import { since } from "../lib/format";
import { Button, CopyField, ErrorNote, Field } from "./primitives";

export function SymLink({ open }: { open: boolean }) {
  const [peers, setPeers] = useState<SymLinkPeer[]>([]);
  const [invitation, setInvitation] = useState<SymLinkInvitation | null>(null);
  const [label, setLabel] = useState("");
  const [origin, setOrigin] = useState(() =>
    ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname) ? "" : window.location.origin);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(Date.now());
  const current = useRef<SymLinkInvitation | null>(null);
  const generation = useRef(0);

  useEffect(() => {
    const epoch = ++generation.current;
    setInvitation(null);
    setError("");
    setBusy(false);
    if (!open) return;
    let polling = false;
    async function refresh() {
      if (polling) return;
      polling = true;
      const before = current.current;
      try {
        const state = await api.symlink();
        if (epoch !== generation.current) return;
        setPeers(state.peers);
        if (before && current.current === before && !state.invitations.some((i) => i.id === before.invitation_id)) {
          current.current = null;
          setInvitation(null);
        }
      } catch (cause) {
        if (epoch === generation.current) setError(cause instanceof Error ? cause.message : "Could not read paired devices");
      } finally {
        polling = false;
      }
    }
    void refresh();
    const poll = window.setInterval(() => { void refresh(); }, 3000);
    const tick = window.setInterval(() => setNow(Date.now()), 1000);
    return () => {
      ++generation.current;
      window.clearInterval(poll);
      window.clearInterval(tick);
      const previous = current.current;
      current.current = null;
      if (previous) void api.cancelSymLinkInvitation(previous.invitation_id).catch(() => {});
    };
  }, [open]);

  async function generate() {
    const epoch = generation.current;
    setBusy(true);
    setError("");
    try {
      if (current.current) await api.cancelSymLinkInvitation(current.current.invitation_id);
      current.current = null;
      setInvitation(null);
      const result = await api.createSymLinkInvitation(label.trim(), origin.trim());
      if (epoch !== generation.current) {
        void api.cancelSymLinkInvitation(result.invitation_id).catch(() => {});
        return;
      }
      current.current = result;
      setInvitation(result);
      setNow(Date.now());
    } catch (cause) {
      if (epoch === generation.current) setError(cause instanceof Error ? cause.message : "Could not create invitation");
    } finally {
      if (epoch === generation.current) setBusy(false);
    }
  }

  async function revoke(id: string) {
    const epoch = generation.current;
    try {
      const result = await api.revokeSymLinkPeer(id);
      if (epoch === generation.current) setPeers(result.peers);
    } catch (cause) {
      if (epoch === generation.current) setError(cause instanceof Error ? cause.message : "Could not revoke device");
    }
  }

  const remaining = invitation ? Math.max(0, Math.ceil(invitation.expires_at - now / 1000)) : 0;
  return (
    <section className="account-block" data-testid="symlink">
      <div className="block-head">
        <div>
          <h3>ENGRAI SymLink</h3>
          <small>Pair an ENGRAI client for encrypted model access and chat. Requires a client with SymLink v1 support.</small>
        </div>
      </div>
      <div className="grid-2">
        <Field label="Device name">
          <input aria-label="SymLink device name" value={label} maxLength={80} placeholder="My iPhone"
            onChange={(event) => setLabel(event.target.value)} />
        </Field>
        <Field label="Server address" hint="An address your Apple device can reach on your LAN or existing tunnel.">
          <input aria-label="SymLink server address" type="url" value={origin} placeholder="https://your-server.example"
            onChange={(event) => setOrigin(event.target.value)} />
        </Field>
      </div>
      <Button busy={busy} disabled={!label.trim() || !origin.trim()} onClick={() => { void generate(); }}>
        Create pairing code
      </Button>
      {invitation && remaining > 0 && (
        <div className="reveal symlink-pairing">
          <img src={invitation.qr} alt="Scan with ENGRAI to pair this device" width={280} height={280} />
          <small>Scan in ENGRAI · expires in {Math.floor(remaining / 60)}:{String(remaining % 60).padStart(2, "0")} · one device only</small>
          <CopyField value={invitation.uri} label="SymLink invitation" />
          <small>Closing this panel cancels the unused code.</small>
        </div>
      )}
      {invitation && remaining === 0 && <p>Pairing code expired. Create a new one to continue.</p>}
      <ErrorNote>{error}</ErrorNote>
      <ul className="key-list">
        {peers.map((peer) => (
          <li key={peer.id}>
            <div><strong>{peer.label}</strong><small>Paired {since(peer.created_at)} · models and chat</small></div>
            <Button variant="ghost" onClick={() => { void revoke(peer.id); }}>Disconnect device</Button>
          </li>
        ))}
        {peers.length === 0 && <li className="is-empty"><span>No ENGRAI devices paired.</span></li>}
      </ul>
    </section>
  );
}
