import { useCallback, useEffect, useState } from "react";
import { ApiError, type Api, type Attainment, type Page, type Project, type Recovery as RecoveryOut,
  type Measurement, type ScenarioRef } from "../api";
import { shortId } from "./Runs";

// W08: recovery (FR-08.1, FR-08.2, FR-08.3). RTO and RPO attainment against each
// scenario's objectives, and what each run found after recovery. Every number shows its
// numerator, denominator, window and definition (NFR-USE-1).

const WINDOWS = [7, 30, 90] as const;

function message(caught: unknown): string {
  return caught instanceof ApiError ? caught.message : "The RAI API cannot be reached.";
}

function day(value: string): string {
  return new Date(value).toISOString().slice(0, 10);
}

/** "42 s" under a minute, else "2.9 min": the same unit the objectives are set in. */
export function duration(seconds: number | string | null): string {
  if (seconds === null) return "—";
  const s = Number(seconds);
  return s < 60 ? `${Math.round(s * 10) / 10} s` : `${(s / 60).toFixed(1)} min`;
}

function Card({ label, tag, value, detail, definition }: {
  label: string; tag: string; value: Attainment; detail: string; definition: string;
}) {
  return (
    <article className="card" aria-label={label}>
      <div className="row"><span className="lbl">{label}</span><span className="tag">{tag}</span></div>
      <div className="big">{value.with_objective ? `${value.met} / ${value.with_objective}` : "—"}</div>
      <p className="note">{value.with_objective ? detail : "Nothing to count in the window"}</p>
      <details className="small">
        <summary>Definition</summary>
        <p>{definition}</p>
      </details>
    </article>
  );
}

function Rto({ m }: { m: Measurement }) {
  if (m.restored_at === null) return <span className="pill fail">✕ not restored</span>;
  const value = duration(m.rto_seconds);
  if (m.rto_met === null) return <span>{value}</span>;
  const target = m.rto_objective_seconds === null ? "" : ` / ${duration(m.rto_objective_seconds)}`;
  return m.rto_met
    ? <span className="pill pass">✓ {value}{target}</span>
    : <span className="pill fail">✕ {value}{target}</span>;
}

function Rpo({ m }: { m: Measurement }) {
  if (!m.rpo_attempted) return <span>—</span>;
  const value = m.rpo_seconds === null ? "unreadable" : duration(m.rpo_seconds);
  return m.rpo_met === false ? <span className="pill fail">✕ {value}</span> : <span>{value}</span>;
}

function Agrees({ m }: { m: Measurement }) {
  const agrees = m.rpo_truth?.agrees;
  if (agrees === undefined || agrees === null) return <span>—</span>;
  return agrees ? <span>✓ yes</span> : <span className="pill fail">✕ no</span>;
}

function Consistency({ m }: { m: Measurement }) {
  const c = m.consistency;
  if (!c) return <span>—</span>;
  const counts = `${c.missing} / ${c.duplicated} / ${c.unexpected} / ${c.mismatched}`;
  return c.passed ? <span className="pill pass">✓ {counts}</span> : <span className="pill fail">✕ {counts}</span>;
}

export function Recovery({ api, project }: { api: Api; project: Project | null }) {
  const [days, setDays] = useState<number>(30);
  const [scenario, setScenario] = useState<string>("");
  const [scenarios, setScenarios] = useState<ScenarioRef[]>([]);
  const [data, setData] = useState<RecoveryOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!project) return;
    let live = true;
    api.get<Page<ScenarioRef>>(`/v1/projects/${project.project_id}/scenarios?limit=200`)
      .then((page) => live && setScenarios(page.items))
      .catch(() => live && setScenarios([]));
    return () => {
      live = false;
    };
  }, [api, project]);

  const load = useCallback(async () => {
    if (!project) return;
    setError(null);
    const until = new Date();
    const since = new Date(until.getTime() - days * 86_400_000);
    const query = new URLSearchParams({ project_id: project.project_id, since: since.toISOString(),
      until: until.toISOString(), limit: "50" });
    if (scenario) query.set("scenario_id", scenario);
    try {
      setData(await api.get<RecoveryOut>(`/v1/assurance/recovery?${query.toString()}`));
    } catch (caught) {
      setError(message(caught));
    }
  }, [api, project, days, scenario]);

  useEffect(() => {
    setData(null);
    void load();
  }, [load]);

  if (!project) {
    return (
      <section className="screen">
        <h1>Recovery</h1>
        <p className="note">This tenant has no projects yet. Create one through the API (docs/api.md).</p>
      </section>
    );
  }

  const chosen = scenarios.find((s) => s.scenario_id === scenario);
  return (
    <section className="screen" aria-labelledby="recovery-h">
      <div className="head">
        <div>
          <h1 id="recovery-h">Recovery</h1>
          <p className="sub">
            {chosen ? `${chosen.name}@${chosen.version}` : "All scenarios"} · window: last {days} days
            {data && ` (${day(data.since)} – ${day(data.until)}, UTC)`} · by fault onset
          </p>
        </div>
        <div className="row">
          <label className="inline-field">
            <span className="sr-only">Scenario</span>
            <select className="btn sm" value={scenario} onChange={(e) => setScenario(e.target.value)}>
              <option value="">All scenarios</option>
              {scenarios.map((s) => (
                <option key={s.scenario_id} value={s.scenario_id}>{s.name}@{s.version}</option>
              ))}
            </select>
          </label>
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
      {!data ? (
        !error && <p className="note" role="status">Loading…</p>
      ) : (
        <Panels data={data} />
      )}
    </section>
  );
}

function Panels({ data }: { data: RecoveryOut }) {
  return (
    <>
      <div className="grid g3">
        <Card label="RTO objective met" tag="FR-08.1" value={data.rto}
          detail={`measured recoveries met their scenario's RTO · ${data.not_restored} not restored`}
          definition={data.rto.definition} />
        <Card label="RPO objective met" tag="FR-08.2" value={data.rpo}
          detail="measured recoveries met their scenario's RPO" definition={data.rpo.definition} />
        <Card label="Consistent after recovery" tag="FR-08.3" value={data.consistency}
          detail="runs whose consistency checks found nothing missing, doubled, unexpected or mismatched"
          definition={data.consistency.definition} />
      </div>

      <article className="card" aria-labelledby="recovery-runs-h">
        <div className="row">
          <h2 id="recovery-runs-h">Runs</h2>
          <span className="small">Showing {data.items.length} of {data.measurements}</span>
        </div>
        {data.items.length === 0 ? (
          <p className="note">No recovery was measured in the window.</p>
        ) : (
          <div className="scroll">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Run</th><th>Scenario</th><th>Onset (UTC)</th><th>RTO</th><th>RPO (ledger)</th>
                  <th>RPO (seeding log)</th><th>Agree</th><th>Consistency</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((m) => (
                  <tr key={m.measurement_id}>
                    <td><a className="mono" href={`#/runs/${m.run_id}`} title={m.run_id}>{shortId(m.run_id)}</a></td>
                    <td>{m.scenario_name}@{m.scenario_version}</td>
                    <td className="mono">{m.onset_at.slice(0, 19).replace("T", " ")}</td>
                    <td><Rto m={m} /></td>
                    <td><Rpo m={m} /></td>
                    <td>{m.rpo_truth ? duration(m.rpo_truth.seconds) : "—"}</td>
                    <td><Agrees m={m} /></td>
                    <td><Consistency m={m} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="small">
          Consistency = missing / duplicated / unexpected / mismatched keys, summed over the run's
          post-recovery checks; extra rows a check allows are counted but do not fail it. RPO
          (seeding log) is the ground truth from SandboxBank's hash-chained log of acknowledged
          writes, kept outside the system under test (B-30); Agree compares whether each says
          anything was lost. "—": the run had no such check.
        </p>
      </article>
    </>
  );
}
