import { useCallback, useEffect, useState } from "react";
import { ApiError, type Api, type Gate, type Page, type Project, type Run, type ScenarioResult, type Severity,
  type Summary } from "../api";
import { shortId, StatePill } from "./Runs";

// W01: the executive dashboard (FR-11.1). Every number shows its numerator and
// denominator, its window and its definition (NFR-USE-1); there is no composite score.

const WINDOWS = [7, 30, 90] as const;
const SEVERITIES: Severity[] = ["critical", "high", "medium", "low"];

function message(caught: unknown): string {
  return caught instanceof ApiError ? caught.message : "The RAI API cannot be reached.";
}

/** The UTC date, ISO style: the same in every locale, and the window is stated in UTC. */
function day(value: string): string {
  return new Date(value).toISOString().slice(0, 10);
}

/** "96.1%", or null when there is nothing to divide by. */
export function percent(numerator: number, denominator: number): string | null {
  return denominator === 0 ? null : `${((100 * numerator) / denominator).toFixed(1)}%`;
}

function Definition({ text }: { text: string }) {
  return (
    <details className="small">
      <summary>Definition</summary>
      <p>{text}</p>
    </details>
  );
}

interface MetricProps {
  label: string;
  tag: string;
  value: string | null;
  detail: string;
  definition: string;
  bad?: boolean;
}

function Metric({ label, tag, value, detail, definition, bad }: MetricProps) {
  return (
    <article className={bad ? "card alert" : "card"} aria-label={label}>
      <div className="row"><span className="lbl">{label}</span><span className="tag">{tag}</span></div>
      <div className={bad ? "big bad" : "big"}>{value ?? "—"}</div>
      <p className="note">{detail}</p>
      <Definition text={definition} />
    </article>
  );
}

function ScenarioPill({ s }: { s: ScenarioResult }) {
  if (s.state === "error") return <span className="pill warn">! Error</span>;
  if (s.verdict === "pass") return <span className="pill pass">✓ Passed</span>;
  const missed = [s.rto_met === false && "RTO", s.rpo_met === false && "RPO"].filter(Boolean).join(" and ");
  return <span className="pill fail">✕ {missed ? `${missed} missed` : "Failed"}</span>;
}

export function Dashboard({ api, project }: { api: Api; project: Project | null }) {
  const [days, setDays] = useState<number>(30);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [gate, setGate] = useState<Gate | null>(null);
  const [recent, setRecent] = useState<Run[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!project) return;
    setError(null);
    const id = project.project_id;
    const until = new Date();
    const since = new Date(until.getTime() - days * 86_400_000);
    const window = `since=${encodeURIComponent(since.toISOString())}&until=${encodeURIComponent(until.toISOString())}`;
    try {
      const [s, g, r] = await Promise.all([
        api.get<Summary>(`/v1/assurance/summary?project_id=${id}&${window}`),
        api.get<Gate>(`/v1/projects/${id}/gate`),
        api.get<Page<Run>>(`/v1/assurance/runs?project_id=${id}&limit=5`),
      ]);
      setSummary(s);
      setGate(g);
      setRecent(r.items);
    } catch (caught) {
      setError(message(caught));
    }
  }, [api, project, days]);

  useEffect(() => {
    setSummary(null);
    void load();
  }, [load]);

  if (!project) {
    return (
      <section className="screen">
        <h1>Assurance overview</h1>
        <p className="note">This tenant has no projects yet. Create one through the API (docs/api.md).</p>
      </section>
    );
  }

  return (
    <section className="screen" aria-labelledby="dash-h">
      <div className="head">
        <div>
          <h1 id="dash-h">Assurance overview</h1>
          <p className="sub">
            {project.name} · window: last {days} days
            {summary && ` (${day(summary.since)} – ${day(summary.until)}, UTC)`} · every metric shows n and its definition
          </p>
        </div>
        <div className="row">
          <label className="inline-field">
            <span className="sr-only">Window</span>
            <select className="btn sm" value={days} onChange={(e) => setDays(Number(e.target.value))}>
              {WINDOWS.map((d) => <option key={d} value={d}>Last {d} days</option>)}
            </select>
          </label>
          <button type="button" className="btn sm" onClick={() => void load()}>Refresh</button>
        </div>
      </div>
      {error && <div className="box alert" role="alert">{error}</div>}
      {!summary ? (
        !error && <p className="note" role="status">Loading…</p>
      ) : (
        <Panels summary={summary} gate={gate} recent={recent ?? []} />
      )}
    </section>
  );
}

function Panels({ summary: s, gate, recent }: { summary: Summary; gate: Gate | null; recent: Run[] }) {
  const critical = s.findings.open.critical;
  const maxRule = Math.max(1, ...s.issues.rules.map((r) => r.failing_checks));
  return (
    <>
      <div className="grid g4">
        <Metric label="Validation pass rate" tag="FR-03" value={percent(s.validation.numerator, s.validation.denominator)}
          detail={s.validation.denominator === 0 ? "No rule checks in the window"
            : `${s.validation.numerator.toLocaleString()} of ${s.validation.denominator.toLocaleString()} rule checks passed`}
          definition={s.validation.definition} />
        <Metric label="RTO attainment" tag="FR-08.1"
          value={s.rto.with_objective ? `${s.rto.met} / ${s.rto.with_objective}` : null}
          detail={s.rto.with_objective ? "recovery measurements met their scenario's RTO objective"
            : "No recovery measured with an RTO objective in the window"}
          definition={s.rto.definition} />
        <Metric label="RPO attainment" tag="FR-08.2"
          value={s.rpo.with_objective ? `${s.rpo.met} / ${s.rpo.with_objective}` : null}
          detail={s.rpo.with_objective ? "recovery measurements met their scenario's RPO objective"
            : "No recovery measured with an RPO objective in the window"}
          definition={s.rpo.definition} />
        <Metric label="Open critical findings" tag="FR-10.1" value={String(critical)} bad={critical > 0}
          detail={gate === null ? "Release gate: not available"
            : gate.verdict === "blocked" ? `✕ ${gate.blocking_count} ${gate.blocking_count === 1 ? "blocks" : "block"} the release gate`
            : `✓ Release gate pass${gate.waived_count ? ` · ${gate.waived_count} waived` : ""}`}
          definition={s.findings.definition} />
      </div>

      <div className="grid g2">
        <article className="card" aria-labelledby="issues-h">
          <div className="row"><h2 id="issues-h">Data-quality issues by rule</h2><span className="tag">FR-03 · FR-04.1</span></div>
          {s.issues.rules.length === 0 ? (
            <p className="note">No failing rule checks in the window.</p>
          ) : (
            <ul className="plain" aria-label="Failing checks by rule">
              {s.issues.rules.map((r) => (
                <li key={r.rule} className="hbar">
                  <span className="mono" title={r.rule}>{r.rule}</span>
                  <meter className="meter" min={0} max={maxRule} value={r.failing_checks} aria-hidden="true" />
                  <span className="n">{r.failing_checks}</span>
                </li>
              ))}
            </ul>
          )}
          <p className="small">
            n = {s.issues.total.toLocaleString()} failing rule {s.issues.total === 1 ? "check" : "checks"}
            {" "}· counts are failing rule checks, not rows
          </p>
          <Definition text={s.issues.definition} />
        </article>

        <article className="card" aria-labelledby="chaos-h">
          <div className="row"><h2 id="chaos-h">Controlled chaos testing</h2><span className="tag">FR-07 · NFR-SAF-1</span></div>
          {s.scenarios.length === 0 ? (
            <p className="note">No scenario ran in the window.</p>
          ) : (
            <div className="scroll">
              <table className="tbl">
                <thead><tr><th>Scenario</th><th>Last run</th><th>Result</th></tr></thead>
                <tbody>
                  {s.scenarios.map((sc) => (
                    <tr key={sc.scenario_id}>
                      <td>{sc.name}@{sc.version}</td>
                      <td><a className="mono" href={`#/runs/${sc.run_id}`} title={sc.run_id}>{shortId(sc.run_id)}</a></td>
                      <td><ScenarioPill s={sc} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="small">
            {s.chaos.numerator} of {s.chaos.denominator} scenario steps passed · {s.refused.numerator} of{" "}
            {s.refused.denominator} finished runs refused in preflight
          </p>
          <Definition text={`${s.chaos.definition} ${s.refused.definition}`} />
        </article>
      </div>

      <div className="grid g3">
        <article className="card" aria-labelledby="runs-out-h">
          <div className="row"><h2 id="runs-out-h">Runs by outcome</h2><span className="tag">FR-06.1</span></div>
          {s.runs.finished === 0 ? (
            <p className="note">No run finished in the window.</p>
          ) : (
            <dl className="kv">
              {Object.entries(s.runs.by_state).sort(([a], [b]) => a.localeCompare(b)).map(([state, n]) => (
                <div key={state}><dt>{state}</dt><dd>{n}</dd></div>
              ))}
            </dl>
          )}
          <p className="small">n = {s.runs.finished} runs finished in the window</p>
          <Definition text={s.runs.definition} />
        </article>

        <article className="card" aria-labelledby="findings-h">
          <div className="row"><h2 id="findings-h">Open findings</h2><span className="tag">FR-10.1 · FR-12.2</span></div>
          <dl className="kv">
            {SEVERITIES.map((sev) => (
              <div key={sev}><dt>{sev}</dt><dd>{s.findings.open[sev]}</dd></div>
            ))}
          </dl>
          <p className="small">Open now · {s.findings.opened_in_window} opened in the window</p>
          <Definition text={s.findings.definition} />
          <a className="small" href="#/approvals">Review findings and the release gate</a>
        </article>

        <article className="card" aria-labelledby="later-h">
          <div className="row"><h2 id="later-h">Not on this dashboard yet</h2><span className="pill warn">◷ Planned</span></div>
          <ul className="plain small">
            <li>Seeded defects detected and false alarms on clean batches (Protocol section 6): reported by <span className="mono">rai-bench</span>, not the API.</li>
            <li>Recovery time per run against its target: the Recovery screen.</li>
            <li>Drift: drift checks run (FR-09.2) and show on the run's page; a dashboard panel is not built yet.</li>
          </ul>
        </article>
      </div>

      <article className="card" aria-labelledby="recent-h">
        <div className="row"><h2 id="recent-h">Recent runs</h2><a href="#/runs">All runs</a></div>
        {recent.length === 0 ? (
          <p className="note">No runs yet.</p>
        ) : (
          <div className="scroll">
            <table className="tbl">
              <thead><tr><th>Run</th><th>State</th><th>Trigger</th><th>Queued</th></tr></thead>
              <tbody>
                {recent.map((r) => (
                  <tr key={r.run_id}>
                    <td><a className="mono" href={`#/runs/${r.run_id}`} title={r.run_id}>{shortId(r.run_id)}</a></td>
                    <td><StatePill state={r.state} /></td>
                    <td className="mono">{r.trigger_ref?.slice(0, 12) ?? "—"}</td>
                    <td>{new Date(r.queued_at).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="small">Refused = a preflight stopped the run before any test or fault ran (NFR-SAF-1).</p>
      </article>
    </>
  );
}
