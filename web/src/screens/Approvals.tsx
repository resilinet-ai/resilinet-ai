import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ApiError, type Api, type Finding, type Gate, type Member, type Page, type Project,
  type ReleaseSummary, type Run, type Severity, type Waiver } from "../api";
import { shortId } from "./Runs";

// W05: approvals and gates (FR-10.1, FR-12.1, FR-12.2). The release gate, the project's
// findings with their owner and gate effect, one finding's fix, retest, waivers and
// dismissal, and the release decisions (B-42). Controls a role cannot use are hidden;
// one that is unavailable for a reason (deciding your own waiver, approving a release
// while the gate is blocked) is shown disabled with the reason. Decisions are
// append-only (NFR-AUD-1): nothing here edits or deletes one.

const WRITERS = new Set(["engineer", "reviewer", "admin"]);
const REVIEWERS = new Set(["reviewer", "admin"]);
const LABEL = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/;
const MAX_WAIVER_DAYS = 90;
const DEFAULT_WAIVER_DAYS = 14;
const DAY = 86_400_000;

const SEVERITY: Record<Severity, { icon: string; tone: string }> = {
  critical: { icon: "✕", tone: "fail" },
  high: { icon: "▲", tone: "warn" },
  medium: { icon: "●", tone: "neutral" },
  low: { icon: "○", tone: "neutral" },
};

function message(caught: unknown): string {
  return caught instanceof ApiError ? caught.message : "The RAI API cannot be reached.";
}

/** A date as YYYY-MM-DD, in UTC. */
export function utcDay(value: Date | string): string {
  return new Date(value).toISOString().slice(0, 10);
}

/** A waiver chosen by day expires at the start of that day, UTC: the latest choosable
 * day is then still within 90 days of now, as the API requires. */
export function expiresAt(day: string): string {
  return `${day}T00:00:00Z`;
}

function when(value: string | null): string {
  return value ? value.slice(0, 16).replace("T", " ") : "—";
}

export function SeverityPill({ severity }: { severity: Severity }) {
  const { icon, tone } = SEVERITY[severity];
  return <span className={`pill ${tone}`}>{icon} {severity}</span>;
}

type Names = Record<string, string>;

function name(names: Names, id: string | null): string {
  if (!id) return "—";
  return names[id] ?? shortId(id);
}

interface Props {
  api: Api;
  project: Project | null;
  userId: string;
  role: string;
  findingId: string | null;
}

export function Approvals({ api, project, userId, role, findingId }: Props) {
  const [gate, setGate] = useState<Gate | null>(null);
  const [findings, setFindings] = useState<Finding[] | null>(null);
  const [more, setMore] = useState(false);
  const [status, setStatus] = useState<"open" | "">("open");
  const [names, setNames] = useState<Names>({});
  const [releases, setReleases] = useState<ReleaseSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0); // bumped after every change, so all panels reload

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
    const base = `/v1/projects/${project.project_id}`;
    const query = new URLSearchParams({ limit: "200" });
    if (status) query.set("status", status);
    try {
      const [g, page, r] = await Promise.all([
        api.get<Gate>(`${base}/gate`),
        api.get<Page<Finding>>(`${base}/findings?${query.toString()}`),
        api.get<ReleaseSummary[]>(`${base}/releases`),
      ]);
      setGate(g);
      setFindings(page.items);
      setMore(page.next_cursor !== null);
      setReleases(r);
    } catch (caught) {
      setError(message(caught));
    }
  }, [api, project, status]);

  useEffect(() => {
    setFindings(null);
    void load();
  }, [load, version]);

  if (!project) {
    return (
      <section className="screen">
        <h1>Approvals &amp; gates</h1>
        <p className="note">This tenant has no projects yet. Create one through the API (docs/api.md).</p>
      </section>
    );
  }

  const changed = () => setVersion((v) => v + 1);
  const waivedUntil = new Map((gate?.waived ?? []).map((f) => [f.finding_id, f.waiver_expires_at]));
  const blocking = new Set((gate?.blocking ?? []).map((f) => f.finding_id));
  return (
    <section className="screen" aria-labelledby="approvals-h">
      <div className="head">
        <div>
          <h1 id="approvals-h">Approvals &amp; gates</h1>
          <p className="sub">{project.name}: release gate <GateVerdict gate={gate} /></p>
        </div>
        <div className="row">
          <label className="inline-field">
            <span className="sr-only">Status</span>
            <select className="btn sm" value={status} onChange={(e) => setStatus(e.target.value as "open" | "")}>
              <option value="open">Open findings</option>
              <option value="">All findings</option>
            </select>
          </label>
          <button type="button" className="btn sm" onClick={() => void load()}>Refresh</button>
        </div>
      </div>
      {error && <div className="box alert" role="alert">{error}</div>}
      {gate?.definition && (
        <details className="small">
          <summary>How the gate is decided</summary>
          <p>{gate.definition}. Evaluated {when(gate.evaluated_at ?? null)} UTC.</p>
        </details>
      )}
      <div className={findingId ? "grid split" : "grid"}>
        <article className="card" aria-labelledby="findings-h">
          <div className="row">
            <h2 id="findings-h">Findings</h2>
            {findings && (
              <span className="small">
                {findings.length} {status === "open" ? "open" : "in all"}{more ? " (the first 200)" : ""}
              </span>
            )}
          </div>
          {findings === null ? (
            !error && <p className="note" role="status">Loading…</p>
          ) : findings.length === 0 ? (
            <p className="note">{status === "open" ? "No open findings." : "No findings yet."}</p>
          ) : (
            <div className="scroll">
              <table className="tbl">
                <thead>
                  <tr><th>Severity</th><th>Finding</th><th>Owner</th><th>Status</th><th>Gate</th></tr>
                </thead>
                <tbody>
                  {findings.map((f) => (
                    <tr key={f.finding_id} className={f.finding_id === findingId ? "sel" : undefined}>
                      <td><SeverityPill severity={f.severity} /></td>
                      <td><a href={`#/approvals/${f.finding_id}`}>{f.title}</a></td>
                      <td>{name(names, f.owner_id)}</td>
                      <td>{f.status}</td>
                      <td>
                        <GateEffect blocks={blocking.has(f.finding_id)} waivedUntil={waivedUntil.get(f.finding_id)} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="small">
            Only an open critical finding blocks the release gate, until a named reviewer
            approves a waiver for it. A finding closes only on a passing retest.
          </p>
        </article>
        {findingId && (
          <FindingPanel key={`${findingId}-${version}`} api={api} findingId={findingId} names={names}
            userId={userId} role={role} onChange={changed} />
        )}
      </div>
      <Releases api={api} project={project} gate={gate} releases={releases} names={names}
        role={role} onChange={changed} />
    </section>
  );
}

function GateVerdict({ gate }: { gate: Gate | null }) {
  if (!gate) return <span className="pill neutral">…</span>;
  if (gate.verdict === "pass") {
    return (
      <span className="pill pass">
        ✓ Pass{gate.waived_count ? ` · ${gate.waived_count} waived` : ""}
      </span>
    );
  }
  const n = gate.blocking_count;
  return <span className="pill fail">✕ Blocked by {n} critical finding{n === 1 ? "" : "s"}</span>;
}

function GateEffect({ blocks, waivedUntil }: { blocks: boolean; waivedUntil: string | null | undefined }) {
  if (blocks) return <span className="pill fail">✕ blocks</span>;
  if (waivedUntil) return <span>◷ waived to {utcDay(waivedUntil)}</span>;
  return <span>— no</span>;
}

interface PanelProps {
  api: Api;
  findingId: string;
  names: Names;
  userId: string;
  role: string;
  onChange: () => void;
}

function FindingPanel({ api, findingId, names, userId, role, onChange }: PanelProps) {
  const [finding, setFinding] = useState<Finding | null>(null);
  const [waivers, setWaivers] = useState<Waiver[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    Promise.all([
      api.get<Finding>(`/v1/findings/${findingId}`),
      api.get<Waiver[]>(`/v1/findings/${findingId}/waivers`),
    ]).then(([f, w]) => {
      if (!live) return;
      setFinding(f);
      setWaivers(w);
    }).catch((caught) => live && setError(message(caught)));
    return () => {
      live = false;
    };
  }, [api, findingId]);

  if (error) return <div className="card alert" role="alert">{error}</div>;
  if (!finding) return <div className="card"><p className="note" role="status">Loading finding…</p></div>;
  const open = finding.status === "open";
  const writer = WRITERS.has(role);
  const current = waivers.find((w) => !w.expired && w.status !== "rejected");
  return (
    <article className="card" aria-labelledby="finding-h">
      <div className="row">
        <h2 id="finding-h">{finding.title}</h2>
        <span className="tag">FR-10.1</span>
      </div>
      <dl className="kv">
        <dt>Severity</dt><dd><SeverityPill severity={finding.severity} /></dd>
        <dt>Status</dt><dd>{finding.status}</dd>
        <dt>Source</dt>
        <dd>
          run <a className="mono" href={`#/runs/${finding.source_run_id}`} title={finding.source_run_id}>
            {shortId(finding.source_run_id)}</a> · {finding.source_step_key} ·{" "}
          <span className="mono">{finding.source_rule}</span>
        </dd>
        <dt>Owner</dt><dd>{name(names, finding.owner_id)}</dd>
        <dt>Opened</dt><dd>{when(finding.created_at)} UTC by {name(names, finding.created_by)}</dd>
        <dt>Fix</dt><dd>{finding.fix_ref ?? "none yet"}</dd>
        <dt>Retest</dt>
        <dd>
          {finding.retest_run_id ? (
            <span className="pill pass">✓ passed in run {shortId(finding.retest_run_id)}</span>
          ) : finding.status === "open" ? (
            <span className="pill fail">✕ not run yet</span>
          ) : "—"}
        </dd>
        {finding.status === "dismissed" && (
          <><dt>Dismissed</dt><dd>{finding.resolution} ({name(names, finding.resolved_by)})</dd></>
        )}
      </dl>
      {finding.description && <p className="note">{finding.description}</p>}
      {open && writer && <FixForm api={api} finding={finding} onChange={onChange} />}
      {open && writer && <RetestForm api={api} finding={finding} onChange={onChange} />}
      {finding.severity === "critical" && (
        <Waivers api={api} finding={finding} waivers={waivers} names={names} userId={userId}
          role={role} onChange={onChange} />
      )}
      {finding.severity === "critical" && open && writer && !current && (
        <WaiverForm api={api} finding={finding} onChange={onChange} />
      )}
      {open && REVIEWERS.has(role) && (
        <DismissForm api={api} finding={finding} userId={userId} onChange={onChange} />
      )}
    </article>
  );
}

/** Run an action, show its error, and report success. */
export function useAction(onChange: () => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      onChange();
    } catch (caught) {
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  }
  return { busy, error, run };
}

export function Failure({ error }: { error: string | null }) {
  return error ? <div className="box alert" role="alert">{error}</div> : null;
}

function FixForm({ api, finding, onChange }: { api: Api; finding: Finding; onChange: () => void }) {
  const [fix, setFix] = useState(finding.fix_ref ?? "");
  const { busy, error, run } = useAction(onChange);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void run(() => api.patch(`/v1/findings/${finding.finding_id}`, { fix_ref: fix.trim() }));
  };
  return (
    <form className="stack" onSubmit={submit}>
      <label className="field">
        Fix reference (pull request, commit or ticket)
        <input value={fix} maxLength={500} onChange={(e) => setFix(e.target.value)} />
      </label>
      <Failure error={error} />
      <div><button type="submit" className="btn sm" disabled={busy || !fix.trim()}>Save fix</button></div>
    </form>
  );
}

function RetestForm({ api, finding, onChange }: { api: Api; finding: Finding; onChange: () => void }) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [runId, setRunId] = useState("");
  const { busy, error, run } = useAction(onChange);

  useEffect(() => {
    let live = true;
    api.get<Page<Run>>(`/v1/assurance/runs?project_id=${finding.project_id}&limit=50`)
      .then((page) => {
        if (!live) return;
        // a retest is a finished run that started after the finding was opened
        const later = page.items.filter((r) => (r.state === "passed" || r.state === "failed")
          && new Date(r.queued_at) > new Date(finding.created_at));
        setRuns(later);
        setRunId(later[0]?.run_id ?? "");
      })
      .catch(() => live && setRuns([]));
    return () => {
      live = false;
    };
  }, [api, finding]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    void run(() => api.post(`/v1/findings/${finding.finding_id}/retest`, { run_id: runId }));
  };
  return (
    <form className="stack" onSubmit={submit}>
      <label className="field">
        Retest with run
        <select value={runId} onChange={(e) => setRunId(e.target.value)} disabled={runs.length === 0}>
          {runs.length === 0 && <option value="">No finished run since the finding was opened</option>}
          {runs.map((r) => (
            <option key={r.run_id} value={r.run_id}>{shortId(r.run_id)} · {r.state} · {when(r.queued_at)}</option>
          ))}
        </select>
      </label>
      <Failure error={error} />
      <div><button type="submit" className="btn sm" disabled={busy || !runId}>Record retest</button></div>
      <p className="small">Closes the finding only if that run passed the same rule for the same test.</p>
    </form>
  );
}

function Waivers({ api, finding, waivers, names, userId, role, onChange }: {
  api: Api; finding: Finding; waivers: Waiver[]; names: Names; userId: string; role: string;
  onChange: () => void;
}) {
  if (waivers.length === 0) return null;
  return (
    <section className="stack" aria-labelledby="waivers-h">
      <div className="row"><h3 id="waivers-h">Waivers</h3><span className="tag">FR-12.2</span></div>
      <ul className="plain">
        {waivers.map((w) => (
          <li key={w.waiver_id} className="sunk">
            <div className="row">
              <WaiverStatus waiver={w} />
              <span className="small">to {utcDay(w.expires_at)} · asked by {name(names, w.requested_by)}</span>
            </div>
            <p className="note">{w.reason}</p>
            {w.decisions.length > 0 && (
              <ul className="plain small">
                {w.decisions.map((d) => (
                  <li key={d.decision_id}>
                    {d.decision === "approve" ? "✓ approved" : "✕ rejected"} by {name(names, d.reviewer_id)},{" "}
                    {when(d.created_at)} UTC{d.comment ? `: ${d.comment}` : ""}
                  </li>
                ))}
              </ul>
            )}
            {REVIEWERS.has(role) && !w.expired && finding.status === "open" && (
              <DecideWaiver api={api} waiver={w} finding={finding} userId={userId} onChange={onChange} />
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

function WaiverStatus({ waiver }: { waiver: Waiver }) {
  if (waiver.in_force) return <span className="pill pass">✓ in force</span>;
  if (waiver.expired) return <span className="pill neutral">◷ expired</span>;
  if (waiver.status === "rejected") return <span className="pill fail">✕ rejected</span>;
  if (waiver.status === "pending") return <span className="pill warn">◷ pending</span>;
  return <span className="pill neutral">{waiver.status}</span>;
}

function DecideWaiver({ api, waiver, finding, userId, onChange }: {
  api: Api; waiver: Waiver; finding: Finding; userId: string; onChange: () => void;
}) {
  const [comment, setComment] = useState("");
  const { busy, error, run } = useAction(onChange);
  const own = userId === waiver.requested_by ? "you asked for this waiver"
    : userId === finding.owner_id ? "you own this finding" : null;
  const decide = (decision: "approve" | "reject") =>
    void run(() => api.post(`/v1/waivers/${waiver.waiver_id}/decisions`,
      comment.trim() ? { decision, comment: comment.trim() } : { decision }));
  return (
    <div className="stack">
      {own ? (
        <p className="small">You cannot decide this waiver: {own}. Another named reviewer must.</p>
      ) : (
        <label className="field">
          Comment (optional)
          <input value={comment} maxLength={2000} onChange={(e) => setComment(e.target.value)} />
        </label>
      )}
      <Failure error={error} />
      <div className="row">
        <button type="button" className="btn sm primary" disabled={busy || own !== null || waiver.status === "approved"}
          onClick={() => decide("approve")}>Approve waiver</button>
        <button type="button" className="btn sm danger" disabled={busy || own !== null || waiver.status === "rejected"}
          onClick={() => decide("reject")}>{waiver.status === "approved" ? "Withdraw approval" : "Reject waiver"}</button>
      </div>
    </div>
  );
}

function WaiverForm({ api, finding, onChange }: { api: Api; finding: Finding; onChange: () => void }) {
  const today = Date.now();
  const [reason, setReason] = useState("");
  const [day, setDay] = useState(utcDay(new Date(today + DEFAULT_WAIVER_DAYS * DAY)));
  const { busy, error, run } = useAction(onChange);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void run(() => api.post(`/v1/findings/${finding.finding_id}/waivers`,
      { reason: reason.trim(), expires_at: expiresAt(day) }));
  };
  return (
    <form className="stack" onSubmit={submit} aria-labelledby="waiver-h">
      <div className="row"><h3 id="waiver-h">Request a waiver</h3><span className="tag">FR-12.2</span></div>
      <label className="field">
        Reason (required)
        <textarea value={reason} maxLength={2000} required onChange={(e) => setReason(e.target.value)} />
      </label>
      <label className="field">
        Expires on (UTC, at most {MAX_WAIVER_DAYS} days)
        <input type="date" value={day} required min={utcDay(new Date(today + DAY))}
          max={utcDay(new Date(today + MAX_WAIVER_DAYS * DAY))} onChange={(e) => setDay(e.target.value)} />
      </label>
      <Failure error={error} />
      <div><button type="submit" className="btn sm primary" disabled={busy || !reason.trim() || !day}>Request waiver</button></div>
      <p className="small">
        Until a reviewer other than you and the finding's owner approves it, the gate stays
        blocked. When the waiver expires, the finding blocks again (FR-12.1, FR-12.2).
      </p>
    </form>
  );
}

function DismissForm({ api, finding, userId, onChange }: {
  api: Api; finding: Finding; userId: string; onChange: () => void;
}) {
  const [reason, setReason] = useState("");
  const { busy, error, run } = useAction(onChange);
  if (finding.created_by === userId) {
    return (
      <div className="stack">
        <button type="button" className="btn sm danger" disabled>Dismiss</button>
        <p className="small">You opened this finding, so another reviewer must dismiss it.</p>
      </div>
    );
  }
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void run(() => api.post(`/v1/findings/${finding.finding_id}/dismiss`, { reason: reason.trim() }));
  };
  return (
    <form className="stack" onSubmit={submit}>
      <label className="field">
        Dismiss as not a defect: reason
        <input value={reason} maxLength={2000} onChange={(e) => setReason(e.target.value)} />
      </label>
      <Failure error={error} />
      <div><button type="submit" className="btn sm danger" disabled={busy || !reason.trim()}>Dismiss</button></div>
    </form>
  );
}

function Releases({ api, project, gate, releases, names, role, onChange }: {
  api: Api; project: Project; gate: Gate | null; releases: ReleaseSummary[] | null; names: Names;
  role: string; onChange: () => void;
}) {
  const [label, setLabel] = useState("");
  const [justification, setJustification] = useState("");
  const { busy, error, run } = useAction(() => {
    setJustification("");
    onChange();
  });
  const validLabel = LABEL.test(label);
  const ready = validLabel && justification.trim() !== "" && !busy;
  const blocked = gate?.verdict !== "pass";
  const decide = (decision: "approve" | "reject") =>
    void run(() => api.post(
      `/v1/projects/${project.project_id}/releases/${encodeURIComponent(label)}/decisions`,
      { decision, justification: justification.trim() },
    ));
  return (
    <article className="card" aria-labelledby="releases-h">
      <div className="row">
        <h2 id="releases-h">Release decisions</h2>
        <span className="tag">FR-12.1, FR-12.2</span>
      </div>
      {releases === null ? null : releases.length === 0 ? (
        <p className="note">No release has been decided yet.</p>
      ) : (
        <div className="scroll">
          <table className="tbl">
            <thead><tr><th>Release</th><th>Latest decision</th><th>Reviewer</th><th>Decided (UTC)</th></tr></thead>
            <tbody>
              {releases.map((r) => (
                <tr key={r.release_label}>
                  <td className="mono">{r.release_label}</td>
                  <td>
                    {r.status === "approved"
                      ? <span className="pill pass">✓ approved</span>
                      : <span className="pill fail">✕ rejected</span>}
                  </td>
                  <td>{name(names, r.reviewer_id)}</td>
                  <td className="mono">{when(r.decided_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {REVIEWERS.has(role) && (
        <form className="stack" onSubmit={(e) => e.preventDefault()} aria-label="Decide a release">
          <div className="grid g2">
            <label className="field">
              Release
              <input value={label} placeholder="v1.4" maxLength={64} onChange={(e) => setLabel(e.target.value)}
                aria-invalid={label !== "" && !validLabel} />
            </label>
            <label className="field">
              Justification (required)
              <input value={justification} maxLength={2000} onChange={(e) => setJustification(e.target.value)} />
            </label>
          </div>
          {label !== "" && !validLabel && (
            <p className="small">A release is letters, digits and . _ + -, starting with a letter or digit.</p>
          )}
          <Failure error={error} />
          <div className="row">
            <button type="button" className="btn sm primary" disabled={!ready || blocked}
              onClick={() => decide("approve")}>Approve release</button>
            <button type="button" className="btn sm danger" disabled={!ready}
              onClick={() => decide("reject")}>Reject release</button>
          </div>
          {blocked && (
            <p className="small">
              Approve is unavailable while the release gate is blocked: fix or waive each
              blocking critical finding first.
            </p>
          )}
          <p className="small">
            Each decision records the gate as it stands at that moment, in the audit chain.
            The latest decision counts; nothing is edited or deleted.
          </p>
        </form>
      )}
    </article>
  );
}
