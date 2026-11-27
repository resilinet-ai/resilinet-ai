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
    const payload = reply.body instanceof Blob ? reply.body : JSON.stringify(reply.body ?? null);
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

export const SCENARIOS = {
  items: [{ scenario_id: "s-1", name: "ledger-resilience", version: 3 }],
  next_cursor: null,
};
