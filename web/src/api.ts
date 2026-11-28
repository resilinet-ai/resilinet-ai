// The RAI API client (docs/api.md). Same origin: Caddy serves the UI and /v1 together.
//
// Tokens live in memory only, never in localStorage or cookies: a reload signs you out.
// The access token lasts 15 minutes; a 401 triggers one refresh (rotating the refresh
// token) and one retry.

export interface Problem {
  status: number;
  code: string;
  title: string;
  detail?: string;
  retryAfter?: number;
}

export class ApiError extends Error {
  readonly problem: Problem;

  constructor(problem: Problem) {
    super(problem.detail ? `${problem.title} (${problem.detail})` : problem.title);
    this.problem = problem;
  }
}

export interface Tenant {
  tenant_id: string;
  slug: string;
  name: string;
  role: string;
}

export interface Me {
  user_id: string;
  email: string;
  display_name: string;
  tenants: Tenant[];
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export interface Project {
  project_id: string;
  name: string;
  description: string | null;
  data_classification: "synthetic" | "authorized_snapshot";
  status: "active" | "archived";
}

export type RunState = "queued" | "running" | "passed" | "failed" | "refused" | "aborted" | "error";

export interface Run {
  run_id: string;
  project_id: string;
  plan_id: string;
  state: RunState;
  verdict: "pass" | "fail" | null;
  reason: string | null;
  trigger_ref: string | null;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface Step {
  step_key: string;
  plan_position: number;
  state: "done" | "error";
  verdict: "pass" | "fail" | null;
  error: string | null;
}

export interface RunDetail extends Run {
  steps: Step[];
  observations: Record<string, number>;
}

export interface Observation {
  observation_id: string;
  step_key: string;
  rule: string;
  outcome: "pass" | "fail" | "error" | "skipped";
  row_count: number | null;
  failing_count: number | null;
  expected: { severity?: string } & Record<string, unknown>;
  observed_value: { message?: string } & Record<string, unknown>;
}

export interface GateFinding {
  finding_id: string;
  title: string;
  owner_id?: string;
  source_run_id?: string;
  waiver_id?: string | null;
  waiver_expires_at: string | null;
}

export interface Gate {
  verdict: "pass" | "blocked";
  evaluated_at?: string;
  blocking_count: number;
  waived_count: number;
  blocking: GateFinding[];
  waived: GateFinding[];
  definition?: string;
}

interface Tokens {
  access: string;
  refresh: string;
}

export interface Download {
  blob: Blob;
  filename: string;
  headers: Headers;
}

async function toProblem(response: Response): Promise<ApiError> {
  let body: Record<string, unknown> = {};
  try {
    body = (await response.json()) as Record<string, unknown>;
  } catch {
    // not JSON: keep the status alone
  }
  const retry = Number(response.headers.get("Retry-After"));
  const problem: Problem = {
    status: response.status,
    code: typeof body.code === "string" ? body.code : `http.${response.status}`,
    title: typeof body.title === "string" ? body.title : `HTTP ${response.status}`,
  };
  if (typeof body.detail === "string") problem.detail = body.detail;
  if (Number.isFinite(retry) && retry > 0) problem.retryAfter = retry;
  return new ApiError(problem);
}

export interface Sso {
  enabled: boolean;
  name: string | null;
}

/** What the SSO callback left in the URL fragment: a one-time code, an error code, or
 * nothing. The fragment never reaches a server or a Referer. */
export function ssoReturn(hash: string): { code: string } | { error: string } | null {
  const match = /^#sso(_error)?=([A-Za-z0-9._-]{1,200})$/.exec(hash);
  if (!match) return null;
  return match[1] ? { error: match[2]! } : { code: match[2]! };
}

export class Api {
  private tokens: Tokens | null = null;
  tenantId: string | null = null;

  constructor(
    private readonly base = "",
    private readonly fetcher: typeof fetch = (...args) => fetch(...args),
  ) {}

  get signedIn(): boolean {
    return this.tokens !== null;
  }

  async login(email: string, password: string): Promise<Me> {
    const pair = await this.send<{ access_token: string; refresh_token: string }>(
      "POST", "/v1/auth/login", { email, password }, false,
    );
    this.tokens = { access: pair.access_token, refresh: pair.refresh_token };
    return this.get<Me>("/v1/me");
  }

  /** Whether this deployment offers single sign-on, and its button's label (FR-01.3). */
  async sso(): Promise<Sso> {
    try {
      return await this.send<Sso>("GET", "/v1/auth/sso", undefined, false);
    } catch {
      return { enabled: false, name: null }; // an older API, or none reachable: no button
    }
  }

  /** Finish a single sign-on: the one-time code from the callback, for a session. */
  async ssoExchange(code: string): Promise<Me> {
    const pair = await this.send<{ access_token: string; refresh_token: string }>(
      "POST", "/v1/auth/sso/exchange", { code }, false,
    );
    this.tokens = { access: pair.access_token, refresh: pair.refresh_token };
    return this.get<Me>("/v1/me");
  }

  async logout(): Promise<void> {
    const tokens = this.tokens;
    this.tokens = null;
    this.tenantId = null;
    if (tokens) {
      const headers = { Authorization: `Bearer ${tokens.access}`, "Content-Type": "application/json" };
      await this.fetcher(`${this.base}/v1/auth/logout`, {
        method: "POST", headers, body: JSON.stringify({ refresh_token: tokens.refresh }),
      }).catch(() => undefined); // signing out locally does not wait on the server
    }
  }

  get<T>(path: string): Promise<T> {
    return this.send<T>("GET", path);
  }

  post<T>(path: string, body: unknown): Promise<T> {
    return this.send<T>("POST", path, body);
  }

  patch<T>(path: string, body: unknown): Promise<T> {
    return this.send<T>("PATCH", path, body);
  }

  /** A file the API returns (for example an evidence bundle), with its file name. */
  async download(path: string): Promise<Download> {
    const response = await this.authorised("GET", path);
    const disposition = response.headers.get("Content-Disposition") ?? "";
    const filename = /filename="([^"]+)"/.exec(disposition)?.[1] ?? "download";
    return { blob: await response.blob(), filename, headers: response.headers };
  }

  private async send<T>(method: string, path: string, body?: unknown, auth = true): Promise<T> {
    const response = auth
      ? await this.authorised(method, path, body)
      : await this.raw(method, path, body, null);
    if (!response.ok) throw await toProblem(response);
    return (response.status === 204 ? undefined : await response.json()) as T;
  }

  private async authorised(method: string, path: string, body?: unknown): Promise<Response> {
    if (!this.tokens) throw new ApiError({ status: 401, code: "auth.signed_out", title: "Sign in first" });
    let response = await this.raw(method, path, body, this.tokens.access);
    if (response.status === 401 && (await this.refresh())) {
      response = await this.raw(method, path, body, this.tokens.access);
    }
    if (response.status === 401) this.tokens = null;
    if (!response.ok) throw await toProblem(response);
    return response;
  }

  private async refresh(): Promise<boolean> {
    if (!this.tokens) return false;
    const response = await this.raw("POST", "/v1/auth/refresh", { refresh_token: this.tokens.refresh }, null);
    if (!response.ok) {
      this.tokens = null;
      return false;
    }
    const pair = (await response.json()) as { access_token: string; refresh_token: string };
    this.tokens = { access: pair.access_token, refresh: pair.refresh_token };
    return true;
  }

  private raw(method: string, path: string, body: unknown, access: string | null): Promise<Response> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (access) headers.Authorization = `Bearer ${access}`;
    if (access && this.tenantId) headers["X-RAI-Tenant"] = this.tenantId;
    const init: RequestInit = { method, headers };
    if (body !== undefined) init.body = JSON.stringify(body);
    return this.fetcher(`${this.base}${path}`, init);
  }
}

// GET /v1/assurance/summary (FR-11.1, NFR-USE-1): every number with its denominator,
// its window and its definition.
export interface Ratio {
  numerator: number;
  denominator: number;
  definition: string;
}

export interface Attainment {
  met: number;
  with_objective: number;
  definition: string;
}

export interface ScenarioResult {
  scenario_id: string;
  name: string;
  version: number;
  run_id: string;
  step_key: string;
  state: "done" | "error";
  verdict: "pass" | "fail" | null;
  finished_at: string;
  rto_met: boolean | null;
  rpo_met: boolean | null;
}

export type Severity = "critical" | "high" | "medium" | "low";

export interface Summary {
  project_id: string;
  since: string;
  until: string;
  window_definition: string;
  validation: Ratio;
  issues: { total: number; rules: { rule: string; failing_checks: number }[]; definition: string };
  runs: { finished: number; by_state: Record<string, number>; definition: string };
  refused: Ratio;
  chaos: Ratio;
  scenarios: ScenarioResult[];
  rto: Attainment;
  rpo: Attainment;
  recovery_measurements: number;
  findings: { open: Record<Severity, number>; opened_in_window: number; definition: string };
}

// GET /v1/assurance/recovery (FR-08.1, FR-08.2, FR-08.3): the Recovery screen (W08).
export interface Measurement {
  measurement_id: string;
  run_id: string;
  step_key: string;
  scenario_id: string;
  scenario_name: string;
  scenario_version: number;
  onset_at: string;
  reverted_at: string;
  restored_at: string | null;
  rto_seconds: string | number | null; // a decimal, sent as a string
  rto_objective_seconds: number | null;
  rto_met: boolean | null;
  rpo_attempted: boolean;
  rpo_seconds: string | number | null;
  rpo_objective_seconds: number | null;
  rpo_met: boolean | null;
  lost_rows: number | null;
  consistency: { missing: number; duplicated: number; unexpected: number; mismatched: number;
    passed: boolean } | null;
  rpo_truth: { seconds: number | null; lost_acknowledged: number; agrees: boolean | null } | null;
}

export interface Recovery {
  project_id: string;
  scenario_id: string | null;
  since: string;
  until: string;
  measurements: number;
  not_restored: number;
  rto: Attainment;
  rpo: Attainment;
  consistency: Attainment;
  items: Measurement[];
  truncated: boolean;
}

export interface ScenarioRef {
  scenario_id: string;
  name: string;
  version: number;
}

// Findings, waivers and release decisions (FR-10.1, FR-12.1, FR-12.2): the Approvals &
// gates screen (W05).
export interface Member {
  user_id: string;
  email: string;
  display_name: string;
  role: string;
}

export interface Finding {
  finding_id: string;
  project_id: string;
  title: string;
  description: string | null;
  severity: Severity;
  status: "open" | "closed" | "dismissed";
  source_run_id: string;
  source_step_key: string;
  source_rule: string;
  owner_id: string;
  fix_ref: string | null;
  retest_run_id: string | null;
  resolution: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface WaiverDecision {
  decision_id: string;
  reviewer_id: string;
  decision: "approve" | "reject";
  comment: string | null;
  created_at: string;
}

export interface Waiver {
  waiver_id: string;
  finding_id: string;
  reason: string;
  expires_at: string;
  requested_by: string;
  created_at: string;
  status: "pending" | "approved" | "rejected";
  expired: boolean;
  in_force: boolean;
  decisions: WaiverDecision[];
}

export interface ReleaseSummary {
  release_label: string;
  status: "approved" | "rejected";
  decided_at: string;
  reviewer_id: string;
}

// Chaos scenarios and their review (FR-07.1, FR-07.2): the Chaos scenarios screen (W03).
export interface ScenarioFault {
  type: "latency" | "timeout" | "connection_drop" | "network_partition" | "container_stop"
    | "container_restart" | "stale_upstream";
  target: string;
  duration_seconds: number;
  latency_ms?: number | null;
  jitter_ms?: number;
  network?: string | null;
}

export interface RecoveryPlan {
  probes: string[];
  ledger: string | null;
  rto_seconds: number | null;
  rpo_seconds: number | null;
  window_seconds: number;
  stable_checks: number;
}

export interface Scenario extends ScenarioRef {
  project_id: string;
  description: string | null;
  blast_radius: string[];
  faults: ScenarioFault[];
  recovery: RecoveryPlan | null;
  status: "pending" | "approved" | "rejected" | "expired";
  created_by: string;
  created_at: string;
}

export interface ScenarioApproval {
  approval_id: string;
  scenario_id: string;
  reviewer_id: string;
  decision: "approve" | "reject";
  comment: string | null;
  created_at: string;
  expires_at: string | null;
  expired: boolean;
}

export interface ScenarioDetail extends Scenario {
  approvals: ScenarioApproval[];
}

// Evidence bundles (FR-13.1, FR-13.2, NFR-AUD-2): the Evidence screen (W06).
export interface PublicKey {
  algorithm: string;
  key_id: string;
  public_key: string;
}

/** What a bundle's bundle.json says about itself (docs/api.md, "Evidence"). */
export interface BundleInfo {
  format: string;
  scope: { kind: string; run_id: string; project_id: string; tenant_id: string };
  anchor: { seq: number; record_hash: string; action: string; occurred_at: string };
  counts: Record<string, number>;
  excluded: string[];
  statement: string;
  signature: { algorithm: string; key_id: string; member: string } | null;
  limitations: string[];
}
