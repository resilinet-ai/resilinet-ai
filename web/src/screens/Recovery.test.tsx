import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Api, type Project } from "../api";
import { fakeFetch, json, PROJECT, RECOVERY, SCENARIOS } from "../fake";
import { duration, Recovery } from "./Recovery";

const project = PROJECT as Project;

function api(recovery: unknown = RECOVERY) {
  const fake = fakeFetch({
    "POST /v1/auth/login": json({ access_token: "a", refresh_token: "r" }),
    "GET /v1/me": json({}),
    "GET /v1/assurance/recovery": json(recovery),
    "GET /v1/projects/p-1/scenarios": json(SCENARIOS),
  });
  const client = new Api("", fake.fetcher);
  return { client, sent: fake.sent, ready: client.login("ada@example.test", "pw") };
}

function card(name: string) {
  return screen.getByRole("article", { name });
}

function row(scenario: string) {
  return within(screen.getByRole("table")).getByText(scenario).closest("tr") as HTMLElement;
}

describe("Recovery (FR-08.1, FR-08.2, FR-08.3, NFR-USE-1)", () => {
  it("shows each attainment with its denominator and definition", async () => {
    const { client, ready } = api();
    await ready;
    render(<Recovery api={client} project={project} />);
    await screen.findByText("1 / 3");
    expect(within(card("RTO objective met")).getByText("1 / 3")).toBeInTheDocument();
    expect(within(card("RTO objective met")).getByText(/1 not restored/)).toBeInTheDocument();
    expect(within(card("RPO objective met")).getByText("2 / 3")).toBeInTheDocument();
    expect(within(card("Consistent after recovery")).getByText("1 / 2")).toBeInTheDocument();
    for (const name of ["RTO objective met", "RPO objective met", "Consistent after recovery"]) {
      expect(within(card(name)).getByText("Definition")).toBeInTheDocument();
    }
    expect(screen.getByText(/2026-10-20 – 2026-11-19, UTC/)).toBeInTheDocument();
    expect(screen.getByText("Showing 3 of 3")).toBeInTheDocument();
  });

  it("marks each run's misses with a symbol as well as colour", async () => {
    const { client, ready } = api();
    await ready;
    render(<Recovery api={client} project={project} />);
    await screen.findByRole("table");
    const met = row("ledger-resilience@3");
    expect(within(met).getByText("✓ 24.2 min / 30.0 min")).toBeInTheDocument();
    expect(within(met).getByText("✓ 0 / 0 / 3 / 0")).toBeInTheDocument(); // allowed extras pass
    expect(within(met).getByText("✓ yes")).toBeInTheDocument();
    const missed = row("etl-stale-feed@3");
    expect(within(missed).getByText("✕ 34.6 min / 30.0 min")).toBeInTheDocument();
    expect(within(missed).getByText("✕ 6.2 min")).toBeInTheDocument(); // RPO objective missed
    expect(within(missed).getByText("✕ no")).toBeInTheDocument(); // the seeding log disagrees
    expect(within(missed).getByText("✕ 14 / 0 / 0 / 0")).toBeInTheDocument();
    const lost = row("api-latency@3");
    expect(within(lost).getByText("✕ not restored")).toBeInTheDocument();
    expect(within(lost).getAllByText("—")).toHaveLength(3); // no truth, no agreement, no checks
    expect(within(met).getByRole("link")).toHaveAttribute("href", `#/runs/${RECOVERY.items[0]!.run_id}`);
  });

  it("filters by scenario and window", async () => {
    const { client, sent, ready } = api();
    await ready;
    const user = userEvent.setup();
    render(<Recovery api={client} project={project} />);
    await screen.findByText("1 / 3");
    await user.selectOptions(screen.getByLabelText("Scenario"), "s-1");
    await waitFor(() => expect(sent.at(-1)!.path).toContain("scenario_id=s-1"));
    expect(screen.getByText(/ledger-resilience@3 · window: last 30 days/)).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Window"), "7");
    await waitFor(() => expect(screen.getByText(/last 7 days/)).toBeInTheDocument());
  });

  it("says when there is nothing to count", async () => {
    const empty = { ...RECOVERY, measurements: 0, not_restored: 0, items: [],
      rto: { ...RECOVERY.rto, met: 0, with_objective: 0 }, rpo: { ...RECOVERY.rpo, met: 0, with_objective: 0 },
      consistency: { ...RECOVERY.consistency, met: 0, with_objective: 0 } };
    const { client, ready } = api(empty);
    await ready;
    render(<Recovery api={client} project={project} />);
    await screen.findByText("No recovery was measured in the window.");
    expect(within(card("RTO objective met")).getByText("—")).toBeInTheDocument();
    expect(within(card("Consistent after recovery")).getByText("Nothing to count in the window")).toBeInTheDocument();
  });

  it("formats durations in the unit objectives use", () => {
    expect(duration(null)).toBe("—");
    expect(duration("42.04")).toBe("42 s");
    expect(duration(174)).toBe("2.9 min");
  });
});
