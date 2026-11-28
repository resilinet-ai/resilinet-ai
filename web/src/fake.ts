// A fake RAI API for tests: routes "METHOD /path" to a handler, records every request.

export interface Sent {
  method: string;
  path: string;
  headers: Record<string, string>;
  body: unknown;
}

type Reply = { status?: number; body?: unknown; headers?: Record<string, string> };
type Handler = (sent: Sent) => Reply;

export function json(body: unknown, status = 200, headers: Record<string, string> = {}): Reply {
  return { status, body, headers };
}

export function fakeFetch(routes: Record<string, Handler | Reply>) {
  const sent: Sent[] = [];
  const fetcher = async (input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
    const url = new URL(String(input), "http://rai.test");
    const method = init.method ?? "GET";
    const request: Sent = {
      method,
      path: url.pathname + url.search,
      headers: (init.headers ?? {}) as Record<string, string>,
      body: typeof init.body === "string" ? JSON.parse(init.body) : undefined,
    };
    sent.push(request);
    const route = routes[`${method} ${url.pathname + url.search}`] ?? routes[`${method} ${url.pathname}`];
    const reply = typeof route === "function" ? route(request) : route ?? json({ title: "Not found", code: "x.not_found" }, 404);
    const headers = { "Content-Type": "application/json", ...(reply.headers ?? {}) };
    const payload = reply.body instanceof Blob ? reply.body
      : reply.body instanceof Uint8Array ? (reply.body.slice() as Uint8Array<ArrayBuffer>) : JSON.stringify(reply.body ?? null);
    return new Response(reply.status === 204 ? null : payload, { status: reply.status ?? 200, headers });
  };
  return { fetcher: fetcher as typeof fetch, sent };
}

export const ME = {
  user_id: "u-1",
  email: "ada@example.test",
  display_name: "Ada Lovelace",
  tenants: [{ tenant_id: "t-1", slug: "sandboxbank", name: "SandboxBank Demo", role: "engineer" }],
};

export const PROJECT = {
  project_id: "p-1",
  name: "ledger-resilience",
  description: null,
  data_classification: "synthetic",
  status: "active",
};

export const RUN = {
  run_id: "0193a5b0-0000-7000-8000-000000000001",
  project_id: "p-1",
  plan_id: "pl-1",
  state: "failed",
  verdict: "fail",
  reason: null,
  trigger_ref: "4f2a9c1e0b7d",
  queued_at: "2026-11-18T09:00:00Z",
  started_at: "2026-11-18T09:00:01Z",
  finished_at: "2026-11-18T09:00:09Z",
};

export const SUMMARY = {
  project_id: "p-1",
  since: "2026-10-20T09:00:00Z",
  until: "2026-11-19T09:00:00Z",
  window_definition: "Runs and steps that finished in [since, until), UTC.",
  validation: { numerator: 1153, denominator: 1200, definition: "Rule checks that passed, over rule checks judged." },
  issues: {
    total: 42,
    rules: [{ rule: "txn_id.unique", failing_checks: 26 }, { rule: "amount.range", failing_checks: 8 }],
    definition: "Failing rule checks by rule.",
  },
  runs: { finished: 12, by_state: { passed: 9, failed: 2, refused: 1 }, definition: "Runs that finished." },
  refused: { numerator: 1, denominator: 12, definition: "Runs a preflight stopped." },
  chaos: { numerator: 3, denominator: 4, definition: "Scenario steps that passed." },
  scenarios: [
    { scenario_id: "s-1", name: "etl-stale-feed", version: 2, run_id: RUN.run_id, step_key: "item-1",
      state: "done", verdict: "fail", finished_at: "2026-11-18T09:00:09Z", rto_met: false, rpo_met: true },
  ],
  rto: { met: 14, with_objective: 16, definition: "Onset to validated restoration." },
  rpo: { met: 15, with_objective: 16, definition: "From the last committed ledger record." },
  recovery_measurements: 16,
  findings: { open: { critical: 2, high: 1, medium: 0, low: 0 }, opened_in_window: 5, definition: "Open now." },
};

const MEASURED = {
  run_id: RUN.run_id, step_key: "item-1", scenario_id: "s-1", scenario_version: 3,
  reverted_at: "2026-11-18T09:05:00Z", rto_objective_seconds: 1800, rpo_attempted: true,
  rpo_objective_seconds: 0, lost_rows: 0,
};

export const RECOVERY = {
  project_id: "p-1",
  scenario_id: null,
  since: "2026-10-20T09:00:00Z",
  until: "2026-11-19T09:00:00Z",
  measurements: 3,
  not_restored: 1,
  rto: { met: 1, with_objective: 3, definition: "First fault onset to stable restoration." },
  rpo: { met: 2, with_objective: 3, definition: "0 s if no committed ledger row was lost." },
  consistency: { met: 1, with_objective: 2, definition: "Runs whose consistency checks found nothing (FR-08.3)." },
  items: [
    { ...MEASURED, measurement_id: "m-1", scenario_name: "ledger-resilience", onset_at: "2026-11-18T09:00:00Z",
      restored_at: "2026-11-18T09:24:12Z", rto_seconds: "1452.000", rto_met: true, rpo_seconds: "0.000",
      rpo_met: true,
      consistency: { missing: 0, duplicated: 0, unexpected: 3, mismatched: 0, passed: true },
      rpo_truth: { seconds: 0, lost_acknowledged: 0, agrees: true } },
    { ...MEASURED, measurement_id: "m-2", scenario_name: "etl-stale-feed", onset_at: "2026-11-17T09:00:00Z",
      restored_at: "2026-11-17T09:34:36Z", rto_seconds: "2076.000", rto_met: false, rpo_seconds: "372.000",
      rpo_met: false, lost_rows: 14,
      consistency: { missing: 14, duplicated: 0, unexpected: 0, mismatched: 0, passed: false },
      rpo_truth: { seconds: 372, lost_acknowledged: 0, agrees: false } },
    { ...MEASURED, measurement_id: "m-3", scenario_name: "api-latency", onset_at: "2026-11-16T09:00:00Z",
      restored_at: null, rto_seconds: null, rto_met: false, rpo_seconds: "0.000", rpo_met: true,
      consistency: null, rpo_truth: null },
  ],
  truncated: false,
};

const SCENARIO_V2 = {
  scenario_id: "s-0", project_id: "p-1", name: "ledger-resilience", version: 2, description: null,
  blast_radius: ["ledger-db"],
  faults: [{ type: "container_stop", target: "ledger-db", duration_seconds: 600, latency_ms: null,
    jitter_ms: 0, network: null }],
  recovery: { probes: ["payments-health"], ledger: "payments-ledger", rto_seconds: 1800, rpo_seconds: 300,
    window_seconds: 2700, stable_checks: 3 },
  status: "approved", created_by: "u-1", created_at: "2026-11-04T10:00:00Z",
};

export const SCENARIO = {
  ...SCENARIO_V2, scenario_id: "s-1", version: 3, status: "pending", created_at: "2026-11-11T15:02:00Z",
  blast_radius: ["ledger-db", "ledger-db-proxy"],
  faults: [
    ...SCENARIO_V2.faults,
    { type: "latency", target: "ledger-db-proxy", duration_seconds: 120, latency_ms: 800, jitter_ms: 50,
      network: null },
  ],
};

export const SCENARIOS = { items: [SCENARIO, SCENARIO_V2], next_cursor: null };

export const SCENARIO_DETAILS = {
  "s-1": { ...SCENARIO, approvals: [] },
  "s-0": { ...SCENARIO_V2, approvals: [{ approval_id: "a-1", scenario_id: "s-0", reviewer_id: "u-2",
    decision: "approve", comment: "radius limited to P1", created_at: "2026-11-04T12:00:00Z",
    expires_at: "2026-12-04T12:00:00Z", expired: false }] },
};

// The Approvals & gates screen (W05). Ada (u-1) is the signed-in user.
export const MEMBERS = [
  { user_id: "u-1", email: "ada@example.test", display_name: "Ada Lovelace", role: "engineer" },
  { user_id: "u-2", email: "grace@example.test", display_name: "Grace Hopper", role: "reviewer" },
];

const FINDING = {
  project_id: "p-1", description: null, source_run_id: RUN.run_id, source_step_key: "item-1",
  fix_ref: null, retest_run_id: null, resolution: null, resolved_by: null, resolved_at: null,
  created_by: "u-1", created_at: "2026-11-18T10:00:00Z", updated_at: "2026-11-18T10:00:00Z",
};

export const FINDINGS = {
  items: [
    { ...FINDING, finding_id: "f-1", title: "Duplicate txn_id in the feed", severity: "critical",
      status: "open", source_rule: "txn_id.unique", owner_id: "u-1" },
    { ...FINDING, finding_id: "f-2", title: "RTO objective missed (etl-stale-feed)", severity: "critical",
      status: "open", source_rule: "rto.objective", owner_id: "u-1" },
    { ...FINDING, finding_id: "f-3", title: "Drift: channel PSI 0.12", severity: "high",
      status: "open", source_rule: "psi.channel", owner_id: "u-2" },
  ],
  next_cursor: null,
};

export const GATE_BLOCKED = {
  project_id: "p-1", verdict: "blocked", evaluated_at: "2026-11-27T09:00:00Z",
  blocking_count: 1, waived_count: 1,
  blocking: [{ finding_id: "f-1", title: "Duplicate txn_id in the feed", owner_id: "u-1",
    source_run_id: RUN.run_id, waiver_id: null, waiver_expires_at: null }],
  waived: [{ finding_id: "f-2", title: "RTO objective missed (etl-stale-feed)", owner_id: "u-1",
    source_run_id: RUN.run_id, waiver_id: "w-2", waiver_expires_at: "2026-12-15T00:00:00Z" }],
  definition: "blocked while any open critical finding of the project has no waiver in force",
};

export const WAIVER = {
  waiver_id: "w-1", finding_id: "f-1", reason: "Fix ships in v1.5", expires_at: "2026-12-15T00:00:00Z",
  requested_by: "u-1", created_at: "2026-11-20T09:00:00Z", status: "pending", expired: false,
  in_force: false, decisions: [],
};

export const RELEASES = [
  { release_label: "v1.3", status: "approved", decided_at: "2026-11-10T12:00:00Z", reviewer_id: "u-2" },
];

/** A ZIP file of stored (uncompressed) members, as the API writes an evidence bundle. The
 * CRCs are left at zero: the UI does not check them, rai verify checks the SHA-256s. */
export function storedZip(files: Record<string, string>): Uint8Array {
  const text = new TextEncoder();
  const chunks: Uint8Array[] = [];
  const central: Uint8Array[] = [];
  let offset = 0;
  const header = (length: number, fill: (v: DataView) => void) => {
    const bytes = new Uint8Array(length);
    fill(new DataView(bytes.buffer));
    return bytes;
  };
  for (const [path, content] of Object.entries(files)) {
    const name = text.encode(path);
    const data = text.encode(content);
    chunks.push(header(30, (v) => {
      v.setUint32(0, 0x04034b50, true);
      v.setUint32(18, data.length, true);
      v.setUint32(22, data.length, true);
      v.setUint16(26, name.length, true);
    }), name, data);
    central.push(header(46, (v) => {
      v.setUint32(0, 0x02014b50, true);
      v.setUint32(20, data.length, true);
      v.setUint32(24, data.length, true);
      v.setUint16(28, name.length, true);
      v.setUint32(42, offset, true);
    }), name);
    offset += 30 + name.length + data.length;
  }
  const directory = central.reduce((n, c) => n + c.length, 0);
  const end = header(22, (v) => {
    v.setUint32(0, 0x06054b50, true);
    v.setUint16(8, central.length / 2, true);
    v.setUint16(10, central.length / 2, true);
    v.setUint32(12, directory, true);
    v.setUint32(16, offset, true);
  });
  const all = [...chunks, ...central, end];
  const out = new Uint8Array(all.reduce((n, c) => n + c.length, 0));
  let at = 0;
  for (const c of all) {
    out.set(c, at);
    at += c.length;
  }
  return out;
}

export const PUBLIC_KEY = {
  algorithm: "ed25519", key_id: "1111222233334444", public_key: "FAKE-PUBLIC-KEY-FOR-TESTS",
};

export const BUNDLE_INFO = {
  format: "rai-evidence/1",
  scope: { kind: "run", run_id: RUN.run_id, project_id: "p-1", tenant_id: "t-1" },
  anchor: { seq: 1204, record_hash: "8d2c41a0e5f97b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b7b",
    action: "run.finished", occurred_at: "2026-11-18T09:00:09.000000Z" },
  counts: { steps: 2, observations: 36, lineage_edges: 3, recovery_measurements: 0, scenario_approvals: 0,
    audit_entries: 4 },
  excluded: ["synthetic data (its SHA-256 digests are in lineage.json)", "secret values (pins hold secret references only)"],
  statement: "Tamper-evident, not tamper-proof (NFR-AUD-2): the manifest and its signature show whether a member changed after export.",
  signature: { algorithm: "ed25519", key_id: PUBLIC_KEY.key_id, member: "signature.json" },
  limitations: [
    "The run executed in environment SANDBOX-01 (sandbox). RAI never runs against production.",
    "Results hold only for the pinned inputs at the time of the run.",
  ],
};

/** A finished run's evidence bundle, as GET /v1/assurance/runs/{id}/evidence returns it. */
export function bundleZip(info: object = BUNDLE_INFO): Uint8Array {
  const dir = `rai-evidence-${RUN.run_id}`;
  const manifest = { format: "rai-evidence/1", algorithm: "sha256",
    files: ["bundle.json", "run.json", "steps.json"].map((path) => ({ path, sha256: "0", bytes: 1 })) };
  return storedZip({
    [`${dir}/bundle.json`]: JSON.stringify(info),
    [`${dir}/run.json`]: JSON.stringify(RUN),
    [`${dir}/steps.json`]: "[]",
    [`${dir}/manifest.json`]: JSON.stringify(manifest),
    [`${dir}/signature.json`]: JSON.stringify({ key_id: PUBLIC_KEY.key_id }),
  });
}

const ENV_BASE = { production_flag: false, created_by: "u-9", created_at: "2026-11-01T09:00:00Z",
  updated_at: "2026-11-01T09:00:00Z" };
export const ENVIRONMENTS = {
  items: [
    { ...ENV_BASE, environment_id: "e-2", name: "STAGING-01", type: "STAGING", max_fault_duration_seconds: 1800,
      status: "active", revision: 1 },
    { ...ENV_BASE, environment_id: "e-3", name: "SANDBOX-OLD", type: "SANDBOX", max_fault_duration_seconds: 900,
      status: "retired", revision: 4 },
    { ...ENV_BASE, environment_id: "e-1", name: "SANDBOX-01", type: "SANDBOX", max_fault_duration_seconds: 900,
      status: "active", revision: 2 },
  ],
  next_cursor: null,
};

const TARGET_BASE = { environment_id: "e-1", required_label: null, health_url: null, score_url: null,
  image_digest: null, created_by: "u-9", created_at: "2026-11-01T09:00:00Z" };
export const TARGETS = {
  items: [
    { ...TARGET_BASE, target_id: "t-2", kind: "proxy", name: "ledger-db-proxy",
      required_label: "rai.target=ledger-db-proxy" },
    { ...TARGET_BASE, target_id: "t-1", kind: "container", name: "ledger-db", required_label: "rai.target=ledger-db",
      image_digest: `sha256:${"ab".repeat(32)}` },
    { ...TARGET_BASE, target_id: "t-3", kind: "http", name: "payments-model",
      health_url: "http://payments-api:8000/health", score_url: "http://payments-api:8000/v1/score" },
  ],
  next_cursor: null,
};
