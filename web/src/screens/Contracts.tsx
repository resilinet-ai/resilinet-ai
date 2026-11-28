import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ApiError, type Api, type Asset, type AssetInput, type ColumnSpec, type ContractDetail,
  type ContractSpec, type ContractSummary, type Observation, type Page, type Project, type Run } from "../api";
import { short } from "./Evidence";
import { shortId } from "./Runs";

// W04: contracts and data (FR-02.1, FR-02.3, FR-03.1–03.4, FR-04.1, FR-05.1). A project's
// assets, each asset's contract versions, what the newest checked batch found against
// them, and what the asset is derived from. Every role reads; engineers publish a new
// contract version. Published versions never change (docs/api.md, "Data contracts").

const RUNS_SEARCHED = 5;
const PAGES_PER_RUN = 10;

function message(caught: unknown): string {
  return caught instanceof ApiError ? caught.message : "The RAI API cannot be reached.";
}

/** "first … last" for a long list, so a row stays readable. */
function values(list: (string | number | boolean)[]): string {
  const text = list.map(String);
  return text.length > 3 ? `${text[0]} … ${text[text.length - 1]}` : text.join(", ");
}

/** The checks a column's spec asks for, in the words of the contract. */
export function columnRules(name: string, spec: ColumnSpec, contract: ContractSpec): string[] {
  const out: string[] = [];
  if (spec.required || contract.primary_key.includes(name)) out.push("required");
  if (spec.pattern !== undefined) out.push(`pattern ${spec.pattern}`);
  if (spec.min !== undefined && spec.max !== undefined) out.push(`${spec.min} to ${spec.max}`);
  else if (spec.min !== undefined) out.push(`at least ${spec.min}`);
  else if (spec.max !== undefined) out.push(`at most ${spec.max}`);
  if (spec.max_lookup) out.push(`at most ${spec.max_lookup.ref} (by ${spec.max_lookup.key})`);
  if (spec.allowed) out.push(`allowed [${values(spec.allowed)}]`);
  if (spec.references) out.push(`references ${spec.references}`);
  if (spec.unique || (contract.primary_key.length === 1 && contract.primary_key[0] === name)) out.push("unique");
  if (contract.unique_across_batches.includes(name)) out.push("unique across batches");
  if (contract.freshness?.column === name) out.push(`freshness ≤ ${contract.freshness.max_lag}`);
  return out;
}

interface Batch {
  run: Run;
  results: Observation[];
}

/** The newest finished run of the project that checked this asset against a contract. */
async function lastBatch(api: Api, projectId: string, assetId: string): Promise<Batch | null> {
  const runs = await api.get<Page<Run>>(`/v1/assurance/runs?project_id=${projectId}&limit=50`);
  const finished = runs.items.filter((r) => r.finished_at !== null).slice(0, RUNS_SEARCHED);
  for (const run of finished) {
    const results: Observation[] = [];
    let cursor: string | null = null;
    for (let n = 0; n < PAGES_PER_RUN; n += 1) {
      const query: string = cursor ? `&cursor=${encodeURIComponent(cursor)}` : "";
      const page: Page<Observation> = await api.get<Page<Observation>>(
        `/v1/assurance/runs/${run.run_id}/observations?limit=200${query}`);
      results.push(...page.items.filter((o) => o.kind === "rule_result" && o.asset_id === assetId));
      cursor = page.next_cursor;
      if (!cursor) break;
    }
    if (results.length) return { run, results };
  }
  return null;
}

function Outcome({ o }: { o: Observation }) {
  const n = (o.failing_count ?? 0).toLocaleString("en-US");
  if (o.outcome === "pass") return <span className="pill pass">✓ {n} failing</span>;
  if (o.outcome === "fail") return <span className="pill fail">✕ {n} failing</span>;
  if (o.outcome === "error") return <span className="pill warn">▲ error</span>;
  return <span className="pill neutral">⊘ skipped</span>;
}

export function Contracts({ api, project, role, assetId }: {
  api: Api; project: Project | null; role: string; assetId: string | null;
}) {
  const engineer = role === "engineer";
  const [assets, setAssets] = useState<Asset[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!project) return;
    setError(null);
    setAssets(null);
    try {
      const page = await api.get<Page<Asset>>(`/v1/projects/${project.project_id}/assets?limit=200`);
      setAssets([...page.items].sort((a, b) => a.name.localeCompare(b.name)));
    } catch (caught) {
      setError(message(caught));
    }
  }, [api, project]);

  useEffect(() => {
    void load();
  }, [load]);

  const selected = assets?.find((a) => a.asset_id === assetId) ?? assets?.[0] ?? null;
  return (
    <section className="screen" aria-labelledby="contracts-h">
      <div className="head">
        <div>
          <h1 id="contracts-h">Contracts &amp; data</h1>
          <p className="sub">Assets, their contracts and what the newest batch found. {engineer
            ? "You can publish a new contract version as an engineer." : "Engineers publish contract versions."}</p>
        </div>
        <button type="button" className="btn sm" onClick={() => void load()} disabled={!project}>Refresh</button>
      </div>
      {error && <div className="box alert" role="alert">{error}</div>}
      {!project ? (
        <p className="note">Choose a project to see its assets.</p>
      ) : assets === null ? (
        !error && <p className="note" role="status">Loading…</p>
      ) : (
        <div className="grid g21">
          {selected ? (
            <AssetPanel key={selected.asset_id} api={api} project={project} asset={selected} engineer={engineer} />
          ) : (
            <article className="card">
              <p className="note">This project has no asset yet. Engineers register assets through the API
                (<code>POST /v1/projects/{"{id}"}/assets</code>) or <code>rai</code>.</p>
            </article>
          )}
          <div className="vstack">
            <article className="card vstack" aria-labelledby="assets-h">
              <div className="row"><h2 id="assets-h">Assets</h2><span className="tag">FR-03.1</span></div>
              {assets.length > 0 && (
                <ul className="envs">
                  {assets.map((a) => (
                    <li key={a.asset_id}>
                      <a href={`#/contracts/${a.asset_id}`} aria-current={a === selected ? "true" : undefined}>
                        <span>
                          <b>{a.name}</b>
                          <span className="small block anywhere">{a.kind.replace("_", " ")}{a.locator && ` · ${a.locator}`}</span>
                        </span>
                      </a>
                    </li>
                  ))}
                </ul>
              )}
            </article>
            <article className="card vstack" aria-labelledby="connectors-h">
              <div className="row"><h2 id="connectors-h">Connectors</h2><span className="tag">FR-02.1 · FR-02.3</span></div>
              <p className="small">
                The worker reads each asset through a connector named by the asset's locator: PostgreSQL,
                Redshift, or CSV and Parquet files. Connectors, their paths and addresses come from the
                operator's configuration, never from tenants, so this screen does not list them.
              </p>
              <p className="small">
                Credentials are secret references (<code>env:</code>, <code>file:</code>), never stored values,
                and database sessions are read-only.
              </p>
            </article>
            <article className="card vstack" aria-labelledby="privacy-h">
              <div className="row"><h2 id="privacy-h">Privacy sentinel</h2><span className="tag">FR-05.1</span></div>
              <p className="small">
                Synthetic canary values are planted in sensitive fields. A <code>privacy_canary</code> test case
                searches an asset for them, and its result appears above as <code>privacy.canary</code>;{" "}
                <code>rai canary scan</code> searches logs and evidence bundles. Uses no real personal data.
              </p>
            </article>
          </div>
        </div>
      )}
    </section>
  );
}

function AssetPanel({ api, project, asset, engineer }: {
  api: Api; project: Project; asset: Asset; engineer: boolean;
}) {
  const [versions, setVersions] = useState<ContractSummary[] | null>(null);
  const [version, setVersion] = useState<number | null>(null);
  const [contract, setContract] = useState<ContractDetail | null>(null);
  const [inputs, setInputs] = useState<AssetInput[] | null>(null);
  const [batch, setBatch] = useState<Batch | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);

  const loadVersions = useCallback(async (show?: number) => {
    setError(null);
    try {
      const page = await api.get<Page<ContractSummary>>(`/v1/assets/${asset.asset_id}/contracts?limit=200`);
      setVersions(page.items);
      setVersion(show ?? page.items[page.items.length - 1]?.version ?? null);
    } catch (caught) {
      setError(message(caught));
    }
  }, [api, asset.asset_id]);

  useEffect(() => {
    void loadVersions();
    api.get<Page<AssetInput>>(`/v1/assets/${asset.asset_id}/inputs?limit=200`)
      .then((page) => setInputs(page.items), () => setInputs([]));
    lastBatch(api, project.project_id, asset.asset_id)
      .then(setBatch, (caught: unknown) => {
        setBatch(null);
        setError(message(caught));
      });
  }, [api, project.project_id, asset.asset_id, loadVersions]);

  useEffect(() => {
    if (version === null) return;
    let live = true;
    setContract(null);
    api.get<ContractDetail>(`/v1/assets/${asset.asset_id}/contracts/${version}`)
      .then((c) => live && setContract(c), (caught: unknown) => live && setError(message(caught)));
    return () => {
      live = false;
    };
  }, [api, asset.asset_id, version]);

  const latest = versions?.[versions.length - 1] ?? null;
  return (
    <div className="vstack">
      <article className="card vstack" aria-labelledby="contract-h">
        <div className="row">
          <h2 id="contract-h">{asset.name} · {contract ? `contract v${contract.version}` : latest ? "contract" : "no contract yet"}</h2>
          <span className="tag">FR-03.1 · FR-03.4</span>
        </div>
        {error && <div className="box alert" role="alert">{error}</div>}
        {versions === null ? (
          !error && <p className="note" role="status">Loading the contract…</p>
        ) : versions.length === 0 ? (
          <p className="note">No contract is published for this asset, so its batches are not checked.</p>
        ) : (
          <>
            {versions.length > 1 && (
              <label className="field">
                Version
                <select value={version ?? ""} onChange={(e) => setVersion(Number(e.target.value))}>
                  {[...versions].reverse().map((v) => (
                    <option key={v.version} value={v.version}>
                      v{v.version} · {v.created_at.slice(0, 10)}{v === latest ? " · latest" : ""}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {contract && <ContractView contract={contract} />}
          </>
        )}
        {engineer && versions !== null && (
          <Publish key={contract?.contract_id ?? "none"} api={api} asset={asset} latest={latest}
            source={contract && contract.version === latest?.version ? contract.source : null}
            onPublished={(v) => void loadVersions(v)} />
        )}
      </article>
      <LastBatch batch={batch} />
      <article className="card vstack" aria-labelledby="inputs-h">
        <div className="row"><h2 id="inputs-h">Derived from</h2><span className="tag">FR-04.1</span></div>
        {inputs === null ? (
          <p className="note" role="status">Loading…</p>
        ) : inputs.length === 0 ? (
          <p className="note">No input is declared: {asset.name} is a source, or its lineage is not declared yet.</p>
        ) : (
          <ul className="small">
            {inputs.map((i) => (
              <li key={i.input_id}>
                <a href={`#/contracts/${i.input_asset_id}`}>{i.input_name}</a> ({i.input_kind.replace("_", " ")})
                {i.transform_ref && <> through <code className="anywhere">{i.transform_ref}</code></>}
              </li>
            ))}
          </ul>
        )}
        <p className="small">One hop. Each run pins every hop upstream with the SHA-256 of what it read.</p>
      </article>
    </div>
  );
}

function ContractView({ contract }: { contract: ContractDetail }) {
  const spec = contract.spec;
  const policy = spec.schema_change_policy;
  return (
    <>
      <dl className="kv">
        <dt>Reference</dt><dd><code>{contract.ref}</code> · sha256 {short(contract.spec_sha256)}</dd>
        {spec.owner && <><dt>Owner</dt><dd>{spec.owner}</dd></>}
        {spec.description && <><dt>About</dt><dd>{spec.description}</dd></>}
        <dt>Defaults</dt><dd>severity {spec.severity_default} · threshold {String(spec.threshold_default)}</dd>
        <dt>Schema changes</dt>
        <dd>
          added column {policy.added_column.replace("_", "-")} · removed or renamed{" "}
          {policy.removed_or_renamed_column.replace("_", "-")} · type change {policy.type_change.replace("_", "-")}
        </dd>
      </dl>
      <div className="scroll">
        <table className="tbl wrap">
          <caption className="sr-only">Columns of {contract.ref}</caption>
          <thead><tr><th scope="col">Column</th><th scope="col">Type</th><th scope="col">Checks</th>
            <th scope="col">Severity</th><th scope="col">Threshold</th></tr></thead>
          <tbody>
            {Object.entries(spec.columns).map(([name, col]) => (
              <tr key={name}>
                <td><code>{name}</code></td>
                <td>{col.type}</td>
                <td className="anywhere">{columnRules(name, col, spec).join(" · ") || "type only"}</td>
                <td>{col.severity ?? spec.severity_default}</td>
                <td>{String(col.threshold ?? spec.threshold_default)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function LastBatch({ batch }: { batch: Batch | null | undefined }) {
  const schema = batch?.results.find((o) => o.rule === "schema");
  const changes = (schema?.observed_value.changes ?? []) as string[];
  const rows = batch?.results.find((o) => o.row_count !== null)?.row_count ?? null;
  const ref = batch?.results.find((o) => typeof o.expected.contract === "string")?.expected.contract as string | undefined;
  return (
    <article className="card vstack" aria-labelledby="batch-h">
      <div className="row"><h2 id="batch-h">Last batch</h2><span className="tag">FR-03.2 · FR-03.3</span></div>
      {batch === undefined ? (
        <p className="note" role="status">Looking for the newest checked batch…</p>
      ) : batch === null ? (
        <p className="note">No finished run among the project's newest {RUNS_SEARCHED} checked this asset.</p>
      ) : (
        <>
          <p className="small">
            Run <a href={`#/runs/${batch.run.run_id}`}>{shortId(batch.run.run_id)}</a>
            {batch.run.finished_at && ` · finished ${batch.run.finished_at.slice(0, 16).replace("T", " ")} UTC`}
            {rows !== null && ` · ${rows.toLocaleString("en-US")} rows`}
            {ref && ` · against ${ref}`} · {batch.results.length} rules
          </p>
          <div className="scroll">
            <table className="tbl wrap">
              <caption className="sr-only">Rule results of the last batch</caption>
              <thead><tr><th scope="col">Rule</th><th scope="col">Severity</th><th scope="col">Threshold</th>
                <th scope="col">Result</th></tr></thead>
              <tbody>
                {batch.results.map((o) => (
                  <tr key={o.observation_id}>
                    <td>
                      <code className="anywhere">{o.rule}</code>
                      {o.outcome !== "pass" && o.observed_value.message && (
                        <span className="small block">{o.observed_value.message}</span>
                      )}
                    </td>
                    <td>{o.expected.severity ?? "—"}</td>
                    <td>{String(o.expected.threshold ?? "—")}</td>
                    <td><Outcome o={o} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {changes.length > 0 && (
            <div className={`box ${schema?.outcome === "fail" ? "alert" : ""}`} role="group" aria-label="Schema drift">
              <b>Schema drift{schema?.outcome === "fail" ? " · breaking" : ""}</b> <span className="tag">FR-03.2</span>
              <ul className="small">{changes.map((c) => <li key={c}>{c}</li>)}</ul>
              <p className="small">A breaking change fails the batch; a non-breaking one is reported only.</p>
            </div>
          )}
        </>
      )}
    </article>
  );
}

function Publish({ api, asset, latest, source, onPublished }: {
  api: Api; asset: Asset; latest: ContractSummary | null; source: string | null; onPublished: (v: number) => void;
}) {
  const next = (latest?.version ?? 0) + 1;
  const [text, setText] = useState(() =>
    source ? source.replace(/^(version:\s*)\d+/m, `$1${next}`) : "");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<{ message: string; errors: string[] } | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      const out = await api.post<ContractDetail>(`/v1/assets/${asset.asset_id}/contracts`, { source: text });
      onPublished(out.version);
    } catch (caught) {
      const errors = caught instanceof ApiError ? (caught.problem.errors ?? []).map((x) => x.message) : [];
      setProblem({ message: message(caught), errors });
    } finally {
      setBusy(false);
    }
  };
  return (
    <details className="stack publish">
      <summary>New contract version</summary>
      <form className="vstack" onSubmit={(e) => void submit(e)} aria-label="Publish a contract version">
        <label className="field">
          Contract (YAML or JSON)
          <textarea className="mono" rows={14} value={text} spellCheck={false}
            onChange={(e) => setText(e.target.value)} />
        </label>
        <p className="small">
          {latest
            ? `Set version: to ${next} or later and keep the name ${latest.name}. Published versions never change.`
            : "The first version names the contract; later versions keep that name."}{" "}
          The API checks it as <code>rai contract check</code> does.
        </p>
        {problem && (
          <div className="box alert" role="alert">
            {problem.message}
            {problem.errors.length > 0 && <ul className="small">{problem.errors.map((m) => <li key={m}>{m}</li>)}</ul>}
          </div>
        )}
        <div>
          <button type="submit" className="btn primary sm" disabled={busy || !text.trim()}>Publish version</button>
        </div>
      </form>
    </details>
  );
}
