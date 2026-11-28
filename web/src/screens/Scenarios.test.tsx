import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Api, type Project, type Scenario } from "../api";
import { fakeFetch, json, MEMBERS, PROJECT, SCENARIO, SCENARIO_DETAILS, SCENARIOS } from "../fake";
import { changes, Scenarios } from "./Scenarios";

const project = PROJECT as Project;

type Routes = Parameters<typeof fakeFetch>[0];

function api(overrides: Routes = {}) {
  const fake = fakeFetch({
    "POST /v1/auth/login": json({ access_token: "a", refresh_token: "r" }),
    "GET /v1/me": json({}),
    "GET /v1/members": json(MEMBERS),
    "GET /v1/projects/p-1/scenarios": json(SCENARIOS),
    "GET /v1/scenarios/s-1": json(SCENARIO_DETAILS["s-1"]),
    "GET /v1/scenarios/s-0": json(SCENARIO_DETAILS["s-0"]),
    ...overrides,
  });
  const client = new Api("", fake.fetcher);
  return { client, sent: fake.sent, ready: client.login("ada@example.test", "pw") };
}

function decisions(sent: { method: string; path: string; body: unknown }[]) {
  return sent.filter((s) => s.method === "POST" && s.path.endsWith("/approvals"));
}

describe("Chaos scenarios (FR-07.1, FR-07.2, NFR-SAF-1)", () => {
  it("lists every version with its review status, faults and radius", async () => {
    const { client, ready } = api();
    await ready;
    render(<Scenarios api={client} project={project} userId="u-2" role="reviewer" scenarioId={null} />);
    const v3 = (await screen.findByRole("link", { name: "ledger-resilience@3" })).closest("tr") as HTMLElement;
    expect(within(v3).getByText("◷ pending review")).toBeInTheDocument();
    expect(within(v3).getByText("ledger-db, ledger-db-proxy")).toBeInTheDocument();
    expect(within(v3).getByText("Ada Lovelace")).toBeInTheDocument();
    const v2 = screen.getByRole("link", { name: "ledger-resilience@2" }).closest("tr") as HTMLElement;
    expect(within(v2).getByText("✓ approved")).toBeInTheDocument();
    expect((screen.getAllByRole("row").slice(1) as HTMLTableRowElement[]).map((r) => r.cells[0]!.textContent))
      .toEqual(["ledger-resilience@3", "ledger-resilience@2"]); // newest version first
    expect(v3.querySelector("a")).toHaveAttribute("href", "#/scenarios/s-1");
  });

  it("shows a version's radius, faults in order and recovery plan", async () => {
    const { client, ready } = api();
    await ready;
    render(<Scenarios api={client} project={project} userId="u-2" role="reviewer" scenarioId="s-1" />);
    expect(await screen.findByRole("heading", { name: "ledger-resilience · version 3" })).toBeInTheDocument();
    const targets = screen.getByRole("list", { name: "Targets" });
    expect(within(targets).getAllByRole("listitem").map((li) => li.textContent))
      .toEqual(["⊘ ledger-db", "⊘ ledger-db-proxy"]);
    const rows = within(screen.getByRole("article", { name: "Blast radius" })).getAllByRole("row")
      .slice(1) as HTMLTableRowElement[];
    expect(rows.map((r) => [...r.cells].map((c) => c.textContent))).toEqual([
      ["1", "container_stop", "ledger-db", "10.0 min", "—"],
      ["2", "latency", "ledger-db-proxy", "2.0 min", "800 ms ± 50 ms"],
    ]);
    expect(screen.getByText("RTO ≤ 30.0 min · RPO ≤ 5.0 min")).toBeInTheDocument();
    expect(screen.getByText("3 consecutive healthy rounds")).toBeInTheDocument();
  });

  it("compares with the previous version and its decision", async () => {
    const { client, ready } = api();
    await ready;
    render(<Scenarios api={client} project={project} userId="u-2" role="reviewer" scenarioId="s-1" />);
    const review = await screen.findByRole("article", { name: "Review" });
    expect(await within(review).findByText(/v2: ✓ approved by Grace Hopper, 2026-11-04/)).toBeInTheDocument();
    expect(within(review).getByText("Changed in v3: blast radius, faults.")).toBeInTheDocument();
    expect(within(review).getByText("No decision yet on version 3.")).toBeInTheDocument();
  });

  it("lets a reviewer who did not write it approve until a day, or reject", async () => {
    const { client, sent, ready } = api({ "POST /v1/scenarios/s-1/approvals": json({}, 201) });
    await ready;
    const user = userEvent.setup();
    render(<Scenarios api={client} project={project} userId="u-2" role="reviewer" scenarioId="s-1" />);
    const review = await screen.findByRole("article", { name: "Review" });
    await user.type(within(review).getByLabelText("Comment"), "Second fault widens nothing");
    const day = (within(review).getByLabelText(/Approval lasts until/) as HTMLInputElement).value;
    await user.click(within(review).getByRole("button", { name: "✓ Approve" }));
    await waitFor(() => expect(decisions(sent)).toHaveLength(1));
    expect(decisions(sent)[0]).toMatchObject({
      body: { decision: "approve", comment: "Second fault widens nothing", expires_at: `${day}T00:00:00Z` },
    });
    await user.click(within(review).getByRole("button", { name: "✕ Reject" }));
    await waitFor(() => expect(decisions(sent)).toHaveLength(2));
    expect(decisions(sent)[1]).toMatchObject({ body: { decision: "reject" } });
    expect(decisions(sent)[1]!.body).not.toHaveProperty("expires_at");
  });

  it("disables the decision for the author, with the reason", async () => {
    const { client, ready } = api();
    await ready;
    render(<Scenarios api={client} project={project} userId="u-1" role="reviewer" scenarioId="s-1" />);
    const review = await screen.findByRole("article", { name: "Review" });
    expect(within(review).getByRole("button", { name: "✓ Approve" })).toBeDisabled();
    expect(within(review).getByRole("button", { name: "✕ Reject" })).toBeDisabled();
    expect(within(review).getByText(/You wrote this scenario, so another reviewer must decide/)).toBeInTheDocument();
  });

  it("shows the API's refusal", async () => {
    const { client, ready } = api({
      "POST /v1/scenarios/s-1/approvals": json({ title: "expires_at must be in the future and at most 90 days ahead",
        code: "scenarios.expiry_out_of_range" }, 422),
    });
    await ready;
    const user = userEvent.setup();
    render(<Scenarios api={client} project={project} userId="u-2" role="admin" scenarioId="s-1" />);
    const review = await screen.findByRole("article", { name: "Review" });
    await user.click(within(review).getByRole("button", { name: "✓ Approve" }));
    expect(await within(review).findByRole("alert")).toHaveTextContent("at most 90 days ahead");
  });

  it("hides the decision from engineers and viewers", async () => {
    for (const role of ["engineer", "viewer"]) {
      const { client, ready } = api();
      await ready;
      const { unmount } = render(
        <Scenarios api={client} project={project} userId="u-3" role={role} scenarioId="s-0" />);
      const review = await screen.findByRole("article", { name: "Review" });
      expect(within(review).getByText("“radius limited to P1”")).toBeInTheDocument();
      expect(within(review).queryByRole("button")).toBeNull();
      unmount();
    }
  });

  it("names what changed between versions", () => {
    const v3 = SCENARIO as Scenario;
    expect(changes(v3, { ...v3, blast_radius: [...v3.blast_radius].reverse() })).toEqual([]); // order is not a change
    expect(changes(v3, { ...v3, description: "new" })).toEqual(["description"]);
  });
});
