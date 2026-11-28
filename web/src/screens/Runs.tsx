import { useCallback, useEffect, useState } from "react";
import { ApiError, type Api, type Gate, type Observation, type Page, type Project, type Run, type RunDetail } from "../api";
import { saveBlob } from "../files";

const FINISHED = new Set(["passed", "failed", "refused", "aborted", "error"]);

function when(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

/** A short run id: the random tail, since a UUIDv7 starts with its timestamp. */
export function shortId(id: string): string {
  return id.slice(-8);
}

function message(caught: unknown): string {
  return caught instanceof ApiError ? caught.message : "The RAI API cannot be reached.";
}

export function StatePill({ state }: { state: Run["state"] }) {
  const tone = state === "passed" ? "pass" : state === "failed" ? "fail" : FINISHED.has(state) ? "warn" : "neutral";
  return <span className={`pill ${tone}`}>{state}</span>;
}

function GatePill({ gate }: { gate: Gate | null }) {
  if (!gate) return <span className="pill neutral">gate …</span>;
  return gate.verdict === "pass" ? (
    <span className="pill pass">
      ✓ Release gate pass{gate.waived_count ? ` · ${gate.waived_count} waived` : ""}
    </span>
  ) : (
    <span className="pill fail">✕ Release gate blocked · {gate.blocking_count} critical</span>
  );
}

// W02: the project's runs, newest first, and the selected run's detail.
export function Runs({ api, project, runId }: { api: Api; project: Project | null; runId: string | null }) {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [gate, setGate] = useState<Gate | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!project) return;
    setError(null);
    try {
      const page = await api.get<Page<Run>>(`/v1/assurance/runs?project_id=${project.project_id}&limit=50`);
      setRuns(page.items);
      setGate(await api.get<Gate>(`/v1/projects/${project.project_id}/gate`));
    } catch (caught) {
      setError(message(caught));
    }
  }, [api, project]);

  useEffect(() => {
    setRuns(null);
    setGate(null);
    void load();
  }, [load]);

  if (!project) {
    return (
      <section className="screen">
        <h1>Runs</h1>
        <p className="note">This tenant has no projects yet. Create one through the API (docs/api.md).</p>
      </section>
    );
  }

  return (
    <section className="screen" aria-labelledby="runs-h">
      <div className="head">
        <div>
          <h1 id="runs-h">Runs</h1>
          <p className="sub">{project.name}: newest first</p>
        </div>
        <div className="row">
          <GatePill gate={gate} />
          <button type="button" className="btn sm" onClick={() => void load()}>Refresh</button>
        </div>
      </div>
      {error && <div className="box alert" role="alert">{error}</div>}
      {gate && gate.blocking.length > 0 && (
        <div className="card alert">
          <h2>Blocking the release</h2>
          <ul className="plain">
            {gate.blocking.map((f) => (
              <li key={f.finding_id}>
                <span className="pill fail">critical</span> <a href={`#/approvals/${f.finding_id}`}>{f.title}</a>
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className={runId ? "grid split" : "grid"}>
        <div className="card">
          {runs === null ? (
            <p className="note" role="status">Loading…</p>
          ) : runs.length === 0 ? (
            <p className="note">No runs yet. Start one with <span className="mono">rai run</span> or the API.</p>
          ) : (
            <div className="scroll">
              <table className="tbl">
                <thead>
                  <tr><th>Run</th><th>State</th><th>Trigger</th><th>Queued</th><th>Finished</th></tr>
                </thead>
                <tbody>
                  {runs.map((r) => (
                    <tr key={r.run_id} className={r.run_id === runId ? "sel" : undefined}>
                      <td><a className="mono" href={`#/runs/${r.run_id}`} title={r.run_id}>{shortId(r.run_id)}</a></td>
                      <td><StatePill state={r.state} /></td>
                      <td className="mono">{r.trigger_ref?.slice(0, 12) ?? "—"}</td>
                      <td>{when(r.queued_at)}</td>
                      <td>{when(r.finished_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
        {runId && <RunPanel key={runId} api={api} runId={runId} />}
      </div>
    </section>
  );
}

function RunPanel({ api, runId }: { api: Api; runId: string }) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [failing, setFailing] = useState<Observation[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const detail = await api.get<RunDetail>(`/v1/assurance/runs/${runId}`);
        const bad = await Promise.all(["fail", "error"].map((o) =>
          api.get<Page<Observation>>(`/v1/assurance/runs/${runId}/observations?outcome=${o}&limit=50`)));
        if (live) {
          setRun(detail);
          setFailing(bad.flatMap((p) => p.items));
        }
      } catch (caught) {
        if (live) setError(message(caught));
      }
    })();
    return () => {
      live = false;
    };
  }, [api, runId]);

  async function saveEvidence() {
    setSaving(true);
    setError(null);
    try {
      const { blob, filename } = await api.download(`/v1/assurance/runs/${runId}/evidence`);
      saveBlob(blob, filename);
    } catch (caught) {
      setError(message(caught));
    } finally {
      setSaving(false);
    }
  }

  if (error) return <div className="card alert" role="alert">{error}</div>;
  if (!run) return <div className="card"><p className="note" role="status">Loading run…</p></div>;
  const finished = FINISHED.has(run.state);
  return (
    <article className="card" aria-labelledby="run-h">
      <div className="row">
        <h2 id="run-h" title={run.run_id}>Run <span className="mono">{shortId(run.run_id)}</span></h2>
        <StatePill state={run.state} />
      </div>
      {run.reason && <p className="note">{run.reason}</p>}
      <dl className="kv">
        <dt>Started</dt><dd>{when(run.started_at)}</dd>
        <dt>Finished</dt><dd>{when(run.finished_at)}</dd>
        <dt>Observations</dt>
        <dd>{Object.entries(run.observations).map(([k, v]) => `${v} ${k}`).join(" · ") || "none"}</dd>
      </dl>
      <div className="steps" aria-label="Steps">
        {run.steps.map((s) => (
          <span key={s.step_key} className={s.verdict === "fail" || s.state === "error" ? "s now" : "s"}>
            {s.step_key} {s.verdict ?? s.state}
          </span>
        ))}
      </div>
      {failing.length > 0 && (
        <div className="scroll">
          <table className="tbl wrap">
            <thead><tr><th>Failing rule</th><th>Rows</th></tr></thead>
            <tbody>
              {failing.map((o) => (
                <tr key={o.observation_id}>
                  <td>
                    <span className={`pill ${o.outcome === "error" ? "warn" : "fail"}`}>{o.outcome}</span>{" "}
                    <span className="mono">{o.rule}</span>
                    {o.expected.severity && <span className="tag">{o.expected.severity}</span>}
                    <span className="small block">
                      {o.step_key}
                      {o.observed_value.message ? ` · ${o.observed_value.message}` : ""}
                    </span>
                  </td>
                  <td className="num">
                    {o.failing_count != null ? `${o.failing_count.toLocaleString()} / ${(o.row_count ?? 0).toLocaleString()}` : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="row">
        <span className="small">Evidence bundle: manifest and signature, checked with <span className="mono">rai verify</span></span>
        <button type="button" className="btn sm" disabled={!finished || saving} onClick={() => void saveEvidence()}>
          {saving ? "Exporting…" : "Download evidence"}
        </button>
      </div>
    </article>
  );
}
