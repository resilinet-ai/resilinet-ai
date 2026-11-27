import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ApiError, type Api, type Member, type Page, type Project, type Scenario,
  type ScenarioApproval, type ScenarioDetail } from "../api";
import { expiresAt, utcDay } from "./Approvals";
import { duration } from "./Recovery";

// W03: chaos scenarios, their definition and review (FR-07.1, FR-07.2, NFR-SAF-1,
// NFR-AUD-1). Each version's blast radius, faults in order and recovery plan, and its
// review: a reviewer other than the author approves (until an expiry) or rejects it.
// Decisions are append-only and the latest counts; only an approved version in force
// can run. Publishing a version stays with the API and CLI, where definitions are kept
// as files and reviewed as code.

const REVIEWERS = new Set(["reviewer", "admin"]);
const DAY = 86_400_000;
const DEFAULT_APPROVAL_DAYS = 30;
const MAX_APPROVAL_DAYS = 90;

type Names = Record<string, string>;

function message(caught: unknown): string {
  return caught instanceof ApiError ? caught.message : "The RAI API cannot be reached.";
}

function when(value: string | null): string {
  return value ? value.slice(0, 16).replace("T", " ") : "—";
}

function name(names: Names, id: string): string {
  return names[id] ?? id.slice(-8);
}

export function StatusPill({ scenario, approvals }: { scenario: Scenario; approvals?: ScenarioApproval[] }) {
  const latest = approvals?.at(-1);
  switch (scenario.status) {
    case "approved":
      return (
        <span className="pill pass">
          ✓ approved{latest?.expires_at ? ` to ${utcDay(latest.expires_at)}` : ""}
        </span>
      );
    case "rejected":
      return <span className="pill fail">✕ rejected</span>;
    case "expired":
      return <span className="pill neutral">◷ approval expired</span>;
    default:
      return <span className="pill warn">◷ pending review</span>;
  }
}

/** What changed from the previous version, by part. */
export function changes(before: Scenario, after: Scenario): string[] {
  const parts: [string, unknown, unknown][] = [
    ["blast radius", [...before.blast_radius].sort(), [...after.blast_radius].sort()],
    ["faults", before.faults, after.faults],
    ["recovery plan", before.recovery, after.recovery],
    ["description", before.description, after.description],
  ];
  return parts.filter(([, a, b]) => JSON.stringify(a) !== JSON.stringify(b)).map(([part]) => part);
}

interface Props {
  api: Api;
  project: Project | null;
  userId: string;
  role: string;
  scenarioId: string | null;
}

export function Scenarios({ api, project, userId, role, scenarioId }: Props) {
  const [scenarios, setScenarios] = useState<Scenario[] | null>(null);
  const [more, setMore] = useState(false);
  const [names, setNames] = useState<Names>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api.get<Member[]>("/v1/members")
      .then((members) => live && setNames(Object.fromEntries(members.map((m) => [m.user_id, m.display_name]))))
      .catch(() => undefined); // ids stand in for names
    return () => {
      live = false;
    };
  }, [api]);

  const load = useCallback(async () => {
    if (!project) return;
    setError(null);
    try {
      const page = await api.get<Page<Scenario>>(`/v1/projects/${project.project_id}/scenarios?limit=200`);
      // by name, newest version first
      setScenarios([...page.items].sort((a, b) => a.name.localeCompare(b.name) || b.version - a.version));
      setMore(page.next_cursor !== null);
    } catch (caught) {
      setError(message(caught));
    }
  }, [api, project]);

  useEffect(() => {
    setScenarios(null);
    void load();
  }, [load]);

  if (!project) {
    return (
      <section className="screen">
        <h1>Chaos scenarios</h1>
        <p className="note">This tenant has no projects yet. Create one through the API (docs/api.md).</p>
      </section>
    );
  }

  if (scenarioId) {
    return (
      <ScenarioView key={scenarioId} api={api} scenarioId={scenarioId} versions={scenarios ?? []}
        names={names} userId={userId} role={role} />
    );
  }

  return (
    <section className="screen" aria-labelledby="scenarios-h">
      <div className="head">
        <div>
          <h1 id="scenarios-h">Chaos scenarios</h1>
          <p className="sub">{project.name}: every version, newest first by name</p>
        </div>
        <div className="row">
          <button type="button" className="btn sm" onClick={() => void load()}>Refresh</button>
        </div>
      </div>
      {error && <div className="box alert" role="alert">{error}</div>}
      <article className="card" aria-labelledby="versions-h">
        <div className="row">
          <h2 id="versions-h">Versions</h2>
          {scenarios && <span className="small">{scenarios.length}{more ? " (the first 200)" : ""}</span>}
        </div>
        {scenarios === null ? (
          !error && <p className="note" role="status">Loading…</p>
        ) : scenarios.length === 0 ? (
          <p className="note">
            No scenarios yet. Publish one with <span className="mono">POST /v1/projects/{"{id}"}/scenarios</span>.
          </p>
        ) : (
          <div className="scroll">
            <table className="tbl">
              <thead>
                <tr><th>Scenario</th><th>Review</th><th>Faults</th><th>Blast radius</th><th>Author</th><th>Created (UTC)</th></tr>
              </thead>
              <tbody>
                {scenarios.map((s) => (
                  <tr key={s.scenario_id}>
                    <td><a href={`#/scenarios/${s.scenario_id}`}>{s.name}@{s.version}</a></td>
                    <td><StatusPill scenario={s} /></td>
                    <td className="num">{s.faults.length}</td>
                    <td>{s.blast_radius.join(", ")}</td>
                    <td>{name(names, s.created_by)}</td>
                    <td className="mono">{when(s.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="small">
          A version runs only while a reviewer's approval of it is in force; the latest
          decision counts, and approvals lapse at their expiry (FR-07.2, B-43).
        </p>
      </article>
    </section>
  );
}

function ScenarioView({ api, scenarioId, versions, names, userId, role }: {
  api: Api; scenarioId: string; versions: Scenario[]; names: Names; userId: string; role: string;
}) {
  const [scenario, setScenario] = useState<ScenarioDetail | null>(null);
  const [previous, setPrevious] = useState<ScenarioDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let live = true;
    api.get<ScenarioDetail>(`/v1/scenarios/${scenarioId}`)
      .then((s) => live && setScenario(s))
      .catch((caught) => live && setError(message(caught)));
    return () => {
      live = false;
    };
  }, [api, scenarioId, version]);

  const before = scenario
    ? versions.find((v) => v.name === scenario.name && v.version === scenario.version - 1)
    : undefined;
  useEffect(() => {
    if (!before) return;
    let live = true;
    api.get<ScenarioDetail>(`/v1/scenarios/${before.scenario_id}`)
      .then((s) => live && setPrevious(s))
      .catch(() => live && setPrevious(null)); // the comparison is a help, not a requirement
    return () => {
      live = false;
    };
  }, [api, before]);

  const back = <a href="#/scenarios">← All scenarios</a>;
  if (error) {
    return (
      <section className="screen">
        {back}
        <div className="box alert" role="alert">{error}</div>
      </section>
    );
  }
  if (!scenario) return <section className="screen">{back}<p className="note" role="status">Loading…</p></section>;
  return (
    <section className="screen" aria-labelledby="scenario-h">
      {back}
      <div className="head">
        <div>
          <h1 id="scenario-h">{scenario.name} · version {scenario.version}</h1>
          <p className="sub">Author {name(names, scenario.created_by)} · created {when(scenario.created_at)} UTC</p>
        </div>
        <StatusPill scenario={scenario} approvals={scenario.approvals} />
      </div>
      <div className="grid split">
        <Definition scenario={scenario} />
        <Review api={api} scenario={scenario} previous={previous} names={names} userId={userId} role={role}
          onChange={() => setVersion((v) => v + 1)} />
      </div>
    </section>
  );
}

function Definition({ scenario }: { scenario: ScenarioDetail }) {
  const r = scenario.recovery;
  return (
    <article className="card" aria-labelledby="radius-h">
      <div className="row"><h2 id="radius-h">Blast radius</h2><span className="tag">FR-07.2 · NFR-SAF-1</span></div>
      <ul className="chips" aria-label="Targets">
        {scenario.blast_radius.map((t) => <li key={t} className="pill neutral">⊘ {t}</li>)}
      </ul>
      {scenario.description && <p className="note">{scenario.description}</p>}
      <h2>Faults, in order</h2>
      <div className="scroll">
        <table className="tbl">
          <thead><tr><th>#</th><th>Type</th><th>Target</th><th>Duration</th><th>Settings</th></tr></thead>
          <tbody>
            {scenario.faults.map((f, n) => (
              <tr key={n}>
                <td className="num">{n + 1}</td>
                <td className="mono">{f.type}</td>
                <td className="mono">{f.target}</td>
                <td>{duration(f.duration_seconds)}</td>
                <td>
                  {[
                    f.latency_ms != null && `${f.latency_ms} ms${f.jitter_ms ? ` ± ${f.jitter_ms} ms` : ""}`,
                    f.network && `from ${f.network}`,
                  ].filter(Boolean).join(" · ") || "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h2>Recovery plan</h2>
      {r ? (
        <dl className="kv">
          <dt>Probes</dt><dd>{r.probes.join(", ")} (2xx)</dd>
          <dt>Ledger head</dt><dd>{r.ledger ?? "— (no RPO)"}</dd>
          <dt>Objectives</dt>
          <dd>
            RTO {r.rto_seconds == null ? "none" : `≤ ${duration(r.rto_seconds)}`} · RPO{" "}
            {r.rpo_seconds == null ? "none" : `≤ ${duration(r.rpo_seconds)}`}
          </dd>
          <dt>Window</dt><dd>{duration(r.window_seconds)} after the last revert</dd>
          <dt>Stability</dt><dd>{r.stable_checks} consecutive healthy rounds</dd>
        </dl>
      ) : (
        <p className="note">No recovery plan: this version measures no RTO or RPO.</p>
      )}
      <p className="small">
        Every fault of this scenario keeps this radius, including faults already reverted;
        the chaos agent refuses anything wider (docs/refusals.md).
      </p>
    </article>
  );
}

function Review({ api, scenario, previous, names, userId, role, onChange }: {
  api: Api; scenario: ScenarioDetail; previous: ScenarioDetail | null; names: Names; userId: string;
  role: string; onChange: () => void;
}) {
  const changed = previous ? changes(previous, scenario) : null;
  const earlier = previous?.approvals.at(-1);
  return (
    <article className="card" aria-labelledby="review-h">
      <div className="row"><h2 id="review-h">Review</h2><span className="tag">FR-07.2 · NFR-AUD-1</span></div>
      <p className="small">The latest decision counts; each version is reviewed. Decisions are append-only.</p>
      {previous && (
        <div className="sunk small">
          <p>
            v{previous.version}:{" "}
            {earlier
              ? `${earlier.decision === "approve" ? "✓ approved" : "✕ rejected"} by ${name(names, earlier.reviewer_id)}, ${utcDay(earlier.created_at)}${earlier.comment ? ` — “${earlier.comment}”` : ""}`
              : "never decided"}
          </p>
          <p>
            {changed && changed.length > 0
              ? `Changed in v${scenario.version}: ${changed.join(", ")}.`
              : `Same definition as v${previous.version}.`}
          </p>
        </div>
      )}
      {scenario.approvals.length === 0 ? (
        <p><b>No decision yet on version {scenario.version}.</b></p>
      ) : (
        <ul className="plain" aria-label="Decisions">
          {scenario.approvals.map((a) => (
            <li key={a.approval_id} className="sunk small">
              {a.decision === "approve" ? <span className="pill pass">✓ approve</span> : <span className="pill fail">✕ reject</span>}{" "}
              {name(names, a.reviewer_id)}, {when(a.created_at)} UTC
              {a.decision === "approve" && (
                <span> · {a.expired ? "expired" : "until"} {a.expires_at ? utcDay(a.expires_at) : "(no expiry)"}</span>
              )}
              {a.comment && <span className="block">“{a.comment}”</span>}
            </li>
          ))}
        </ul>
      )}
      {REVIEWERS.has(role) && <Decide api={api} scenario={scenario} userId={userId} onChange={onChange} />}
    </article>
  );
}

function Decide({ api, scenario, userId, onChange }: {
  api: Api; scenario: ScenarioDetail; userId: string; onChange: () => void;
}) {
  const now = Date.now();
  const [comment, setComment] = useState("");
  const [day, setDay] = useState(utcDay(new Date(now + DEFAULT_APPROVAL_DAYS * DAY)));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const own = scenario.created_by === userId;

  async function decide(decision: "approve" | "reject") {
    setBusy(true);
    setError(null);
    const body: Record<string, string> = { decision };
    if (comment.trim()) body.comment = comment.trim();
    if (decision === "approve") body.expires_at = expiresAt(day);
    try {
      await api.post(`/v1/scenarios/${scenario.scenario_id}/approvals`, body);
      setComment("");
      onChange();
    } catch (caught) {
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  }

  if (own) {
    return (
      <div className="stack">
        <div className="row">
          <button type="button" className="btn sm primary" disabled>✓ Approve</button>
          <button type="button" className="btn sm danger" disabled>✕ Reject</button>
        </div>
        <p className="small">You wrote this scenario, so another reviewer must decide. The database refuses self-approval too.</p>
      </div>
    );
  }
  return (
    <form className="stack" onSubmit={(e: FormEvent) => e.preventDefault()} aria-label="Decide this version">
      <label className="field">
        Comment
        <textarea value={comment} maxLength={2000} onChange={(e) => setComment(e.target.value)} />
      </label>
      <label className="field">
        Approval lasts until (UTC, at most {MAX_APPROVAL_DAYS} days)
        <input type="date" value={day} required min={utcDay(new Date(now + DAY))}
          max={utcDay(new Date(now + MAX_APPROVAL_DAYS * DAY))} onChange={(e) => setDay(e.target.value)} />
      </label>
      {error && <div className="box alert" role="alert">{error}</div>}
      <div className="row">
        <button type="button" className="btn sm primary" disabled={busy || !day} onClick={() => void decide("approve")}>
          ✓ Approve
        </button>
        <button type="button" className="btn sm danger" disabled={busy} onClick={() => void decide("reject")}>
          ✕ Reject
        </button>
      </div>
      <p className="small">
        You did not write this version, so you may decide it. Approving lets it run until the
        day above; rejecting it later withdraws an approval.
      </p>
    </form>
  );
}
