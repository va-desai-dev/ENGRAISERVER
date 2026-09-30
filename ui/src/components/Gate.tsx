import { useState, type FormEvent } from "react";
import { motion } from "motion/react";
import { useStore } from "../store/useStore";
import { Button, ErrorNote, Eyebrow, Field } from "./primitives";
import { Mark } from "./Mark";
import { Footer } from "./Footer";

/* The lock screen and the first-run screen are one component because they are
   one moment: the gateway either has an owner or is about to get one. */

export function Gate() {
  const screen = useStore((s) => s.screen);
  return (
    <div className="gate-shell">
      {screen === "setup" ? <FirstRun /> : <Unlock />}
      <Footer />
    </div>
  );
}

function Unlock() {
  const signIn = useStore((s) => s.signIn);
  const offline = useStore((s) => s.offline);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await signIn(password);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not sign in");
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="gate">
      <motion.form
        className="gate-card"
        data-testid="unlock-form"
        onSubmit={submit}
        initial={{ opacity: 0, y: 12, scale: 0.99 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ type: "spring", stiffness: 380, damping: 32 }}
      >
        <Mark size={52} />
        <Eyebrow>Private control plane</Eyebrow>
        <h1>Unlock ENGRAI SERVER</h1>
        <p className="gate-copy">
          Your password is exchanged for a session token that stays in this tab.
        </p>
        <Field label="Admin password">
          <input
            type="password"
            autoComplete="current-password"
            data-testid="unlock-password"
            value={password}
            required
            data-autofocus
            onChange={(event) => setPassword(event.target.value)}
          />
        </Field>
        <ErrorNote>{error || (offline ? "Gateway unreachable." : "")}</ErrorNote>
        <Button variant="primary" type="submit" busy={busy}>
          Enter
        </Button>
      </motion.form>
    </div>
  );
}

function FirstRun() {
  const setup = useStore((s) => s.setup);
  const [form, setForm] = useState({ name: "", email: "", password: "", confirm: "" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const set = (key: keyof typeof form) => (event: { target: { value: string } }) =>
    setForm((current) => ({ ...current, [key]: event.target.value }));

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (form.password !== form.confirm) {
      setError("Those passwords do not match.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await setup(form.name, form.email, form.password);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not create the account");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="gate">
      <motion.form
        className="gate-card gate-wide"
        data-testid="setup-form"
        onSubmit={submit}
        initial={{ opacity: 0, y: 12, scale: 0.99 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ type: "spring", stiffness: 380, damping: 32 }}
      >
        <Mark size={52} />
        <Eyebrow>First run</Eyebrow>
        <h1>Create your account</h1>
        <p className="gate-copy">
          This control plane has no owner yet. What you set here is stored on
          this machine only.
        </p>
        <Field label="Name">
          <input
            type="text" autoComplete="name" required data-autofocus
            data-testid="setup-name" value={form.name} onChange={set("name")}
          />
        </Field>
        <Field label="Email" hint="Optional. Never leaves this machine.">
          <input type="email" autoComplete="email" data-testid="setup-email" value={form.email} onChange={set("email")} />
        </Field>
        <Field label="Password" hint="At least 12 characters.">
          <input
            type="password" autoComplete="new-password" minLength={12} required
            data-testid="setup-password" value={form.password} onChange={set("password")}
          />
        </Field>
        <Field label="Confirm password">
          <input
            type="password" autoComplete="new-password" minLength={12} required
            data-testid="setup-confirm" value={form.confirm} onChange={set("confirm")}
          />
        </Field>
        <ErrorNote>{error}</ErrorNote>
        <Button variant="primary" type="submit" busy={busy}>
          Create account
        </Button>
      </motion.form>
    </div>
  );
}
