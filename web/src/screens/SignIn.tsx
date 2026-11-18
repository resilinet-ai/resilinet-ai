import { useState, type FormEvent } from "react";
import { ApiError, type Api, type Me, type Tenant } from "../api";
import { Brand } from "../components/Brand";

// W10: sign in, then choose a tenant when you belong to several (FR-01.1). The error
// is the server's: the same for an unknown email, a wrong password and a disabled
// account (threat S-01), and a 429 says how long to wait.
export function SignIn({ api, onSignedIn }: { api: Api; onSignedIn: (me: Me, tenant: Tenant) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [me, setMe] = useState<Me | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const who = await api.login(email.trim(), password);
      setPassword("");
      const [only] = who.tenants;
      if (who.tenants.length === 1 && only) onSignedIn(who, only);
      else if (who.tenants.length === 0) setError("You are not a member of any tenant yet.");
      else setMe(who);
    } catch (caught) {
      if (caught instanceof ApiError && caught.problem.retryAfter) {
        setError(`${caught.problem.title}. Try again in ${caught.problem.retryAfter} s.`);
      } else {
        setError(caught instanceof ApiError ? caught.problem.title : "The RAI API cannot be reached.");
      }
    } finally {
      setBusy(false);
    }
  }

  if (me) {
    return (
      <section className="card signin-card" aria-labelledby="tenant-h">
        <h1 id="tenant-h">Choose a tenant</h1>
        <p className="note">
          Signed in as {me.display_name} · {me.email}
        </p>
        {me.tenants.map((t) => (
          <button key={t.tenant_id} type="button" className="box choice" onClick={() => onSignedIn(me, t)}>
            <span>
              <b>{t.name}</b>
              <span className="small block">{t.slug}</span>
            </span>
            <span className="pill neutral">{t.role}</span>
          </button>
        ))}
      </section>
    );
  }

  return (
    <form className="card signin-card" aria-labelledby="signin-h" onSubmit={submit}>
      <Brand />
      <h1 id="signin-h">Sign in</h1>
      <label className="field">
        Email
        <input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
      </label>
      <label className="field">
        Password
        <input
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>
      <button className="btn primary" type="submit" disabled={busy}>
        {busy ? "Signing in…" : "Sign in"}
      </button>
      {error && (
        <div className="box alert" role="alert">
          <span className="pill fail">✕ Not signed in</span> {error}
        </div>
      )}
    </form>
  );
}
