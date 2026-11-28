import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ApiError, type Api, type Environment, type Page, type Target, type TargetKind } from "../api";
import { Failure, useAction } from "./Approvals";
import { duration } from "./Recovery";

// W07: environments and the target allow-list (FR-07.2, NFR-SAF-1). Where faults may run,
// and on what. Every role reads; only admins register, change or retire environments and
// add or remove targets. Production cannot be registered: there is no production type,
// and names containing PROD are refused (docs/api.md, "Environments and targets").

const NAME = /^[A-Z][A-Z0-9-]{1,62}$/;
const TARGET_NAME = /^[a-z0-9][a-z0-9_.-]{0,62}$/;
const LABEL = /^[a-z0-9][a-z0-9_./-]{0,99}=[A-Za-z0-9_.-]{1,100}$/;
const URL_RE = /^https?:\/\/\S{1,490}$/;
const DIGEST = /^sha256:[0-9a-f]{64}$/;
const MAX_CEILING = 3600;
const KINDS: TargetKind[] = ["container", "proxy", "db", "http"];

function message(caught: unknown): string {
  return caught instanceof ApiError ? caught.message : "The RAI API cannot be reached.";
}

/** Why a new environment's name would be refused, or null. */
export function nameProblem(name: string): string | null {
  if (name.includes("PROD")) return "Names may not contain PROD: production cannot be registered.";
  if (!NAME.test(name)) return "Capital letters, digits and hyphens, starting with a letter (e.g. SANDBOX-02).";
  return null;
}

/** Why a target would be refused, or an empty list. The API checks the same rules. */
export function targetProblems(t: { kind: TargetKind; name: string; label: string; image: string;
  health: string; score: string }): string[] {
  const out: string[] = [];
  if (!TARGET_NAME.test(t.name)) out.push("a name in lower case (e.g. ledger-db-proxy)");
  if ((t.kind === "container" || t.kind === "proxy") && !t.label) out.push("the label it carries");
  if (t.label && !LABEL.test(t.label)) out.push("a label as key=value");
  if (t.kind === "container" && !DIGEST.test(t.image)) out.push("its image ID (sha256: and 64 hex)");
  if (t.health && !URL_RE.test(t.health)) out.push("a health URL starting http:// or https://");
  if (t.score && !URL_RE.test(t.score)) out.push("a scoring URL starting http:// or https://");
  return out;
}

function StatusPill({ env }: { env: Environment }) {
  return env.status === "active"
    ? <span className="pill pass">✓ active</span>
    : <span className="pill neutral">⊘ retired</span>;
}

export function Environments({ api, role, environmentId }: { api: Api; role: string; environmentId: string | null }) {
  const admin = role === "admin";
  const [envs, setEnvs] = useState<Environment[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const page = await api.get<Page<Environment>>("/v1/environments?limit=200");
      setEnvs([...page.items].sort((a, b) =>
        a.status === b.status ? a.name.localeCompare(b.name) : a.status === "active" ? -1 : 1));
    } catch (caught) {
      setError(message(caught));
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  const selected = envs?.find((e) => e.environment_id === environmentId) ?? envs?.[0] ?? null;
  return (
    <section className="screen" aria-labelledby="environments-h">
      <div className="head">
        <div>
          <h1 id="environments-h">Environments</h1>
          <p className="sub">Where faults may run, and on what. {admin ? "You can change them as an admin." : "Admins change them."}</p>
        </div>
        <button type="button" className="btn sm" onClick={() => void load()}>Refresh</button>
      </div>
      {error && <div className="box alert" role="alert">{error}</div>}
      {envs === null ? (
        !error && <p className="note" role="status">Loading…</p>
      ) : (
        <div className="grid g21">
          {selected ? (
            <EnvironmentPanel key={`${selected.environment_id}-${selected.revision}`} api={api} env={selected}
              admin={admin} onChange={() => void load()} />
          ) : (
            <article className="card"><p className="note">No environment is registered yet, so no fault can run anywhere.</p></article>
          )}
          <article className="card vstack" aria-labelledby="envs-h">
            <div className="row"><h2 id="envs-h">Environments</h2><span className="tag">NFR-SAF-1</span></div>
            <ul className="envs">
              {envs.map((e) => (
                <li key={e.environment_id}>
                  <a href={`#/environments/${e.environment_id}`}
                    aria-current={e === selected ? "true" : undefined}>
                    <span>
                      <b>{e.name}</b>
                      <span className="small block">
                        {e.type.toLowerCase()} · ceiling {duration(e.max_fault_duration_seconds)}
                        {e.status === "retired" && " · no new targets or runs"}
                      </span>
                    </span>
                    <StatusPill env={e} />
                  </a>
                </li>
              ))}
            </ul>
            {admin && <Register api={api} onChange={() => void load()} />}
            <p className="box small">
              There is no production type, and names containing PROD are refused: production cannot be
              registered. An environment's name and type never change; it is retired, never deleted.
            </p>
          </article>
        </div>
      )}
    </section>
  );
}

function Register({ api, onChange }: { api: Api; onChange: () => void }) {
  const [name, setName] = useState("");
  const [type, setType] = useState<"SANDBOX" | "STAGING">("SANDBOX");
  const [ceiling, setCeiling] = useState(900);
  const { busy, error, run } = useAction(() => {
    setName("");
    onChange();
  });
  const problem = name ? nameProblem(name) : null;
  const ceilingOk = Number.isInteger(ceiling) && ceiling >= 1 && ceiling <= MAX_CEILING;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void run(() => api.post("/v1/environments", { name, type, max_fault_duration_seconds: ceiling }));
  };
  return (
    <form className="stack" onSubmit={submit} aria-labelledby="register-h">
      <h3 id="register-h">Register an environment</h3>
      <label className="field">
        Name
        <input value={name} maxLength={63} aria-invalid={problem ? "true" : undefined}
          aria-describedby={problem ? "name-problem" : undefined}
          onChange={(e) => setName(e.target.value.toUpperCase())} />
      </label>
      {problem && <p id="name-problem" className="small">✕ {problem}</p>}
      <label className="field">
        Type
        <select value={type} onChange={(e) => setType(e.target.value as "SANDBOX" | "STAGING")}>
          <option value="SANDBOX">sandbox</option>
          <option value="STAGING">staging</option>
        </select>
      </label>
      <label className="field">
        Fault ceiling (seconds, at most 3,600)
        <input type="number" min={1} max={MAX_CEILING} step={1} value={ceiling}
          aria-invalid={ceilingOk ? undefined : "true"} onChange={(e) => setCeiling(Number(e.target.value))} />
      </label>
      <Failure error={error} />
      <div>
        <button type="submit" className="btn primary sm" disabled={busy || !name || !!problem || !ceilingOk}>
          Register environment
        </button>
      </div>
    </form>
  );
}

function EnvironmentPanel({ api, env, admin, onChange }: {
  api: Api; env: Environment; admin: boolean; onChange: () => void;
}) {
  const [targets, setTargets] = useState<Target[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const page = await api.get<Page<Target>>(`/v1/environments/${env.environment_id}/targets?limit=200`);
      setTargets([...page.items].sort((a, b) => a.name.localeCompare(b.name)));
    } catch (caught) {
      setError(message(caught));
    }
  }, [api, env.environment_id]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <article className="card vstack" aria-labelledby="env-h">
      <div className="row">
        <h2 id="env-h">{env.name} · target allow-list</h2>
        <span className="tag">FR-07.2 · NFR-SAF-1</span>
      </div>
      <dl className="kv">
        <dt>Type</dt><dd>{env.type.toLowerCase()} · never production</dd>
        <dt>Status</dt><dd><StatusPill env={env} /></dd>
        <dt>Fault ceiling</dt><dd>{duration(env.max_fault_duration_seconds)} per fault</dd>
      </dl>
      {error && <div className="box alert" role="alert">{error}</div>}
      {targets === null ? (
        !error && <p className="note" role="status">Loading targets…</p>
      ) : targets.length === 0 ? (
        <p className="note">No target is allowed here yet, so no fault can be injected.</p>
      ) : (
        <div className="scroll">
          <table className="tbl wrap">
            <thead>
              <tr>
                <th>Target</th><th>Kind</th><th>Required label</th><th>Image</th><th>Health URL</th>
                {admin && <th><span className="sr-only">Actions</span></th>}
              </tr>
            </thead>
            <tbody>
              {targets.map((t) => (
                <TargetRow key={t.target_id} api={api} env={env} target={t} admin={admin} onChange={() => void load()} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="small">Targets are added or removed, never edited. Every change is audited.</p>
      {admin && (env.status === "active" ? (
        <AddTarget api={api} env={env} onChange={() => void load()} />
      ) : (
        <div className="stack">
          <div><button type="button" className="btn sm" disabled>Add target</button></div>
          <p className="small">A retired environment takes no new targets.</p>
        </div>
      ))}
      {admin && <Settings api={api} env={env} onChange={onChange} />}
    </article>
  );
}

function TargetRow({ api, env, target: t, admin, onChange }: {
  api: Api; env: Environment; target: Target; admin: boolean; onChange: () => void;
}) {
  const [confirm, setConfirm] = useState(false);
  const { busy, error, run } = useAction(onChange);
  return (
    <tr>
      <td>{t.name}</td>
      <td>{t.kind}</td>
      <td className="mono">{t.required_label ?? "—"}</td>
      <td className="mono" title={t.image_digest ?? undefined}>
        {t.image_digest ? `${t.image_digest.slice(0, 19)}…` : "—"}
      </td>
      <td className="mono anywhere">
        {t.health_url ?? "—"}
        {t.score_url && <span className="small block">scores rows at {t.score_url}</span>}
      </td>
      {admin && (
        <td>
          {confirm ? (
            <span className="row">
              <button type="button" className="btn sm danger" disabled={busy}
                onClick={() => void run(() => api.delete(`/v1/environments/${env.environment_id}/targets/${t.target_id}`))}>
                Yes, remove {t.name}
              </button>
              <button type="button" className="btn sm" onClick={() => setConfirm(false)}>Keep</button>
            </span>
          ) : (
            <button type="button" className="btn sm danger" aria-label={`Remove ${t.name}`}
              onClick={() => setConfirm(true)}>Remove</button>
          )}
          <Failure error={error} />
        </td>
      )}
    </tr>
  );
}

function AddTarget({ api, env, onChange }: { api: Api; env: Environment; onChange: () => void }) {
  const [kind, setKind] = useState<TargetKind>("container");
  const [name, setName] = useState("");
  const [label, setLabel] = useState("");
  const [image, setImage] = useState("");
  const [health, setHealth] = useState("");
  const [score, setScore] = useState("");
  const { busy, error, run } = useAction(() => {
    setName("");
    setLabel("");
    setImage("");
    setHealth("");
    setScore("");
    onChange();
  });
  const fields = { kind, name: name.trim(), label: label.trim(), image: image.trim(),
    health: health.trim(), score: kind === "http" ? score.trim() : "" };
  const problems = targetProblems({ ...fields, image: kind === "container" ? fields.image : "" });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const body: Record<string, string> = { kind, name: fields.name };
    if (fields.label) body.required_label = fields.label;
    if (kind === "container") body.image_digest = fields.image;
    if (fields.health) body.health_url = fields.health;
    if (fields.score) body.score_url = fields.score;
    void run(() => api.post(`/v1/environments/${env.environment_id}/targets`, body));
  };
  return (
    <form className="stack" onSubmit={submit} aria-labelledby="add-target-h">
      <h3 id="add-target-h">Allow a target</h3>
      <div className="grid g2">
        <label className="field">
          Kind
          <select value={kind} onChange={(e) => setKind(e.target.value as TargetKind)}>
            {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
        </label>
        <label className="field">
          Target name
          <input value={name} maxLength={63} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="field">
          Required label{kind === "container" || kind === "proxy" ? "" : " (optional)"}
          <input value={label} placeholder="rai.target=ledger-db" maxLength={201} onChange={(e) => setLabel(e.target.value)} />
        </label>
        {kind === "container" && (
          <label className="field">
            Image ID
            <input value={image} placeholder="sha256:…" maxLength={71} onChange={(e) => setImage(e.target.value)} />
          </label>
        )}
        <label className="field">
          Health URL (optional)
          <input value={health} maxLength={500} onChange={(e) => setHealth(e.target.value)} />
        </label>
        {kind === "http" && (
          <label className="field">
            Scoring URL (optional, for robustness tests)
            <input value={score} maxLength={500} onChange={(e) => setScore(e.target.value)} />
          </label>
        )}
      </div>
      {kind === "container" && (
        <p className="small">
          The image ID is <span className="mono">docker inspect --format {"'{{.Image}}'"} &lt;container&gt;</span>.
          Runs pin it, and the chaos agent refuses a container running another image.
        </p>
      )}
      {name && problems.length > 0 && <p className="small" id="target-problems">Still needed: {problems.join("; ")}.</p>}
      <Failure error={error} />
      <div>
        <button type="submit" className="btn sm" disabled={busy || problems.length > 0}
          aria-describedby={name && problems.length > 0 ? "target-problems" : undefined}>
          Add target
        </button>
      </div>
    </form>
  );
}

function Settings({ api, env, onChange }: { api: Api; env: Environment; onChange: () => void }) {
  const [ceiling, setCeiling] = useState(env.max_fault_duration_seconds);
  const [confirm, setConfirm] = useState(false);
  const { busy, error, run } = useAction(onChange);
  const path = `/v1/environments/${env.environment_id}`;
  const tag = `"v${env.revision}"`;
  const ceilingOk = Number.isInteger(ceiling) && ceiling >= 1 && ceiling <= MAX_CEILING;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void run(() => api.patch(path, { max_fault_duration_seconds: ceiling }, tag));
  };
  return (
    <form className="stack" onSubmit={submit} aria-labelledby="settings-h">
      <h3 id="settings-h">Change {env.name}</h3>
      <label className="field">
        Fault ceiling (seconds, at most 3,600)
        <input type="number" min={1} max={MAX_CEILING} step={1} value={ceiling}
          aria-invalid={ceilingOk ? undefined : "true"} onChange={(e) => setCeiling(Number(e.target.value))} />
      </label>
      <Failure error={error} />
      <div className="row">
        <button type="submit" className="btn sm"
          disabled={busy || !ceilingOk || ceiling === env.max_fault_duration_seconds}>Save ceiling</button>
        {env.status === "retired" ? (
          <button type="button" className="btn sm" disabled={busy}
            onClick={() => void run(() => api.patch(path, { status: "active" }, tag))}>Reactivate</button>
        ) : confirm ? (
          <span className="row">
            <button type="button" className="btn sm danger" disabled={busy}
              onClick={() => void run(() => api.patch(path, { status: "retired" }, tag))}>Yes, retire {env.name}</button>
            <button type="button" className="btn sm" onClick={() => setConfirm(false)}>Keep active</button>
          </span>
        ) : (
          <button type="button" className="btn sm danger" onClick={() => setConfirm(true)}>Retire environment</button>
        )}
      </div>
      <p className="small">A retired environment takes no new targets, and no new run starts there.</p>
    </form>
  );
}
