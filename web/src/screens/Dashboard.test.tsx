import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Api, type Project } from "../api";
import { fakeFetch, json, PROJECT, RUN, SUMMARY } from "../fake";
import { Dashboard, percent } from "./Dashboard";

const GATE = { verdict: "blocked", blocking_count: 1, waived_count: 0, blocking: [], waived: [] };
const project = PROJECT as Project;

function signedIn(routes: Parameters<typeof fakeFetch>[0]) {
  const fake = fakeFetch({ "POST /v1/auth/login": json({ access_token: "a", refresh_token: "r" }),
    "GET /v1/me": json({}), ...routes });
  const client = new Api("", fake.fetcher);
  return { client, sent: fake.sent, ready: client.login("ada@example.test", "pw") };
}

function api(summary: unknown = SUMMARY) {
  return signedIn({
    "GET /v1/assurance/summary": json(summary),
    "GET /v1/projects/p-1/gate": json(GATE),
    "GET /v1/assurance/runs": json({ items: [RUN], next_cursor: null }),
  });
}

function card(name: string) {
  return screen.getByRole("article", { name });
}

describe("Dashboard (FR-11.1, NFR-USE-1)", () => {
  it("shows every number with its denominator, and a definition", async () => {
    const { client, ready } = api();
    await ready;
    render(<Dashboard api={client} project={project} />);
    await screen.findByText("96.1%");
    expect(screen.getByText("1,153 of 1,200 rule checks passed")).toBeInTheDocument();
    expect(within(card("RTO attainment")).getByText("14 / 16")).toBeInTheDocument();
    expect(within(card("RPO attainment")).getByText("15 / 16")).toBeInTheDocument();
    expect(within(card("Open critical findings")).getByText("✕ 1 blocks the release gate")).toBeInTheDocument();
    expect(screen.getByText(/n = 42 failing rule checks/)).toBeInTheDocument();
    expect(screen.getByText("etl-stale-feed@2")).toBeInTheDocument();
    expect(screen.getByText("✕ RTO missed")).toBeInTheDocument();
    expect(screen.getByText(/3 of 4 scenario steps passed · 1 of 12 finished runs refused/)).toBeInTheDocument();
    expect(screen.getByText("n = 12 runs finished in the window")).toBeInTheDocument();
    // Every metric card carries its definition (NFR-USE-1).
    for (const name of ["Validation pass rate", "RTO attainment", "RPO attainment", "Open critical findings"]) {
      expect(within(card(name)).getByText("Definition")).toBeInTheDocument();
    }
    expect(screen.getByText(/2026-10-20 – 2026-11-19, UTC/)).toBeInTheDocument();
  });

  it("shows no number where there is nothing to divide by", async () => {
    const empty = {
      ...SUMMARY,
      validation: { ...SUMMARY.validation, numerator: 0, denominator: 0 },
      rto: { ...SUMMARY.rto, met: 0, with_objective: 0 },
      issues: { ...SUMMARY.issues, total: 0, rules: [] },
      scenarios: [],
    };
    const { client, ready } = api(empty);
    await ready;
    render(<Dashboard api={client} project={project} />);
    expect(await screen.findByText("No rule checks in the window")).toBeInTheDocument();
    expect(within(card("Validation pass rate")).getByText("—")).toBeInTheDocument();
    expect(within(card("RTO attainment")).getByText("—")).toBeInTheDocument();
    expect(screen.getByText("No scenario ran in the window.")).toBeInTheDocument();
    expect(percent(0, 0)).toBeNull();
    expect(percent(1, 3)).toBe("33.3%");
  });

  it("asks for the chosen window", async () => {
    const { client, sent, ready } = api();
    await ready;
    const user = userEvent.setup();
    render(<Dashboard api={client} project={project} />);
    await screen.findByText("96.1%");
    await user.selectOptions(screen.getByRole("combobox", { name: "Window" }), "7");
    await waitFor(() => {
      const last = sent.filter((s) => s.path.startsWith("/v1/assurance/summary")).at(-1)!;
      const params = new URL(last.path, "http://rai.test").searchParams;
      expect(Date.parse(params.get("until")!) - Date.parse(params.get("since")!)).toBe(7 * 86_400_000);
    });
  });

  it("shows the API's error", async () => {
    const { client, ready } = signedIn({
      "GET /v1/assurance/summary": json({ title: "Project not found", code: "projects.not_found" }, 404),
    });
    await ready;
    render(<Dashboard api={client} project={project} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Project not found");
  });
});
