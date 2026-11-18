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
