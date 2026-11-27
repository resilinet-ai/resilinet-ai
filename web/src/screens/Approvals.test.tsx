import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Api, type Project } from "../api";
import { fakeFetch, FINDINGS, GATE_BLOCKED, json, MEMBERS, PROJECT, RELEASES, RUN, WAIVER } from "../fake";
import { Approvals, expiresAt, utcDay } from "./Approvals";

const project = PROJECT as Project;
const F1 = FINDINGS.items[0]!;
const GATE_PASS = { ...GATE_BLOCKED, verdict: "pass", blocking_count: 0, blocking: [] };
const LATER_RUN = { ...RUN, run_id: "0193a5b0-0000-7000-8000-00000000beef", state: "passed",
  queued_at: "2026-11-19T09:00:00Z" };

type Routes = Parameters<typeof fakeFetch>[0];

function api(overrides: Routes = {}) {
  const fake = fakeFetch({
    "POST /v1/auth/login": json({ access_token: "a", refresh_token: "r" }),
    "GET /v1/me": json({}),
    "GET /v1/members": json(MEMBERS),
    "GET /v1/projects/p-1/gate": json(GATE_BLOCKED),
    "GET /v1/projects/p-1/findings": json(FINDINGS),
    "GET /v1/projects/p-1/releases": json(RELEASES),
    "GET /v1/findings/f-1": json(F1),
    "GET /v1/findings/f-1/waivers": json([]),
    "GET /v1/assurance/runs": json({ items: [LATER_RUN, RUN], next_cursor: null }),
    ...overrides,
  });
  const client = new Api("", fake.fetcher);
  return { client, sent: fake.sent, ready: client.login("ada@example.test", "pw") };
}

function findingsRow(title: string) {
  const table = within(screen.getByRole("article", { name: "Findings" })).getByRole("table");
  return within(table).getByText(title).closest("tr") as HTMLElement;
}

function posts(sent: { method: string; path: string; body: unknown }[]) {
  return sent.filter((s) => s.method !== "GET" && !s.path.startsWith("/v1/auth"));
}

describe("Approvals & gates (FR-10.1, FR-12.1, FR-12.2)", () => {
  it("shows the gate, and each finding's severity, owner and effect on the gate", async () => {
    const { client, ready } = api();
    await ready;
    render(<Approvals api={client} project={project} userId="u-1" role="engineer" findingId={null} />);
    await screen.findByText("Duplicate txn_id in the feed");
    expect(screen.getByText("✕ Blocked by 1 critical finding")).toBeInTheDocument();
    const blocks = findingsRow("Duplicate txn_id in the feed");
    expect(within(blocks).getByText("✕ critical")).toBeInTheDocument();
    expect(within(blocks).getByText("Ada Lovelace")).toBeInTheDocument();
    expect(within(blocks).getByText("✕ blocks")).toBeInTheDocument();
    expect(within(findingsRow("RTO objective missed (etl-stale-feed)")).getByText("◷ waived to 2026-12-15"))
      .toBeInTheDocument();
    const high = findingsRow("Drift: channel PSI 0.12");
    expect(within(high).getByText("▲ high")).toBeInTheDocument();
    expect(within(high).getByText("Grace Hopper")).toBeInTheDocument();
    expect(within(high).getByText("— no")).toBeInTheDocument();
    expect(screen.getByText("3 open")).toBeInTheDocument();
    expect(within(blocks).getByRole("link")).toHaveAttribute("href", "#/approvals/f-1");
  });

  it("lets an engineer ask for a waiver, but not decide a release", async () => {
    const { client, sent, ready } = api({
      "POST /v1/findings/f-1/waivers": json(WAIVER, 201),
    });
    await ready;
    const user = userEvent.setup();
    render(<Approvals api={client} project={project} userId="u-1" role="engineer" findingId="f-1" />);
    const panel = await screen.findByRole("article", { name: "Duplicate txn_id in the feed" });
    expect(within(panel).getByText("✕ not run yet")).toBeInTheDocument();
    expect(within(panel).getByText("txn_id.unique")).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "Dismiss" })).toBeNull(); // reviewers only
    await user.type(within(panel).getByLabelText("Reason (required)"), "Fix ships in v1.5");
    const day = (within(panel).getByLabelText(/Expires on/) as HTMLInputElement).value;
    await user.click(within(panel).getByRole("button", { name: "Request waiver" }));
    await waitFor(() => expect(posts(sent)).toHaveLength(1));
    expect(posts(sent)[0]).toMatchObject({
      method: "POST", path: "/v1/findings/f-1/waivers",
      body: { reason: "Fix ships in v1.5", expires_at: `${day}T00:00:00Z` },
    });
    expect(screen.queryByRole("button", { name: "Approve release" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject release" })).toBeNull();
  });

  it("records a retest with a finished run that started after the finding", async () => {
    const { client, sent, ready } = api({
      "POST /v1/findings/f-1/retest": json({ ...F1, status: "closed", retest_run_id: LATER_RUN.run_id }),
    });
    await ready;
    const user = userEvent.setup();
    render(<Approvals api={client} project={project} userId="u-1" role="engineer" findingId="f-1" />);
    const panel = await screen.findByRole("article", { name: "Duplicate txn_id in the feed" });
    const select = within(panel).getByLabelText("Retest with run");
    await waitFor(() => expect(within(select).getAllByRole("option")).toHaveLength(1)); // RUN is too early
    await user.click(within(panel).getByRole("button", { name: "Record retest" }));
    await waitFor(() => expect(posts(sent).at(-1)).toMatchObject({
      path: "/v1/findings/f-1/retest", body: { run_id: LATER_RUN.run_id },
    }));
  });

  it("shows why the API refused, and keeps the form", async () => {
    const { client, ready } = api({
      "POST /v1/findings/f-1/waivers": json({ title: "The finding already has a waiver that is pending",
        code: "waivers.exists" }, 409),
    });
    await ready;
    const user = userEvent.setup();
    render(<Approvals api={client} project={project} userId="u-1" role="engineer" findingId="f-1" />);
    const panel = await screen.findByRole("article", { name: "Duplicate txn_id in the feed" });
    await user.type(within(panel).getByLabelText("Reason (required)"), "again");
    await user.click(within(panel).getByRole("button", { name: "Request waiver" }));
    expect(await within(panel).findByRole("alert")).toHaveTextContent("already has a waiver that is pending");
    expect(within(panel).getByLabelText("Reason (required)")).toHaveValue("again");
  });

  it("lets another reviewer decide a waiver, and disables it for its requester", async () => {
    const routes = {
      "GET /v1/findings/f-1/waivers": json([WAIVER]),
      "POST /v1/waivers/w-1/decisions": json({ ...WAIVER, status: "approved", in_force: true }, 201),
    };
    const other = api(routes);
    await other.ready;
    const user = userEvent.setup();
    const { unmount } = render(
      <Approvals api={other.client} project={project} userId="u-2" role="reviewer" findingId="f-1" />);
    let waivers = await screen.findByRole("region", { name: "Waivers" });
    expect(within(waivers).getByText("◷ pending")).toBeInTheDocument();
    expect(within(waivers).getByText(/asked by Ada Lovelace/)).toBeInTheDocument();
    await user.type(within(waivers).getByLabelText("Comment (optional)"), "ok until v1.5");
    await user.click(within(waivers).getByRole("button", { name: "Approve waiver" }));
    await waitFor(() => expect(posts(other.sent).at(-1)).toMatchObject({
      path: "/v1/waivers/w-1/decisions", body: { decision: "approve", comment: "ok until v1.5" },
    }));
    unmount();

    const own = api(routes);
    await own.ready;
    render(<Approvals api={own.client} project={project} userId="u-1" role="reviewer" findingId="f-1" />);
    waivers = await screen.findByRole("region", { name: "Waivers" });
    expect(within(waivers).getByRole("button", { name: "Approve waiver" })).toBeDisabled();
    expect(within(waivers).getByText(/you asked for this waiver/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeDisabled(); // Ada opened it
  });

  it("disables release approval while the gate is blocked, with the reason", async () => {
    const { client, sent, ready } = api({
      "POST /v1/projects/p-1/releases/v1.4/decisions": json({}, 201),
    });
    await ready;
    const user = userEvent.setup();
    render(<Approvals api={client} project={project} userId="u-2" role="reviewer" findingId={null} />);
    const releases = await screen.findByRole("article", { name: "Release decisions" });
    await within(releases).findByText("v1.3");
    expect(within(releases).getByText("✓ approved")).toBeInTheDocument();
    expect(within(releases).getByText("Grace Hopper")).toBeInTheDocument();
    await user.type(within(releases).getByLabelText("Release"), "v1.4");
    await user.type(within(releases).getByLabelText("Justification (required)"), "Known issue in f-1");
    expect(within(releases).getByRole("button", { name: "Approve release" })).toBeDisabled();
    expect(within(releases).getByText(/Approve is unavailable while the release gate is blocked/))
      .toBeInTheDocument();
    await user.click(within(releases).getByRole("button", { name: "Reject release" }));
    await waitFor(() => expect(posts(sent).at(-1)).toMatchObject({
      path: "/v1/projects/p-1/releases/v1.4/decisions",
      body: { decision: "reject", justification: "Known issue in f-1" },
    }));
  });

  it("approves a release when the gate passes", async () => {
    const { client, sent, ready } = api({
      "GET /v1/projects/p-1/gate": json(GATE_PASS),
      "POST /v1/projects/p-1/releases/v1.4/decisions": json({}, 201),
    });
    await ready;
    const user = userEvent.setup();
    render(<Approvals api={client} project={project} userId="u-2" role="admin" findingId={null} />);
    await screen.findByText("✓ Pass · 1 waived");
    const releases = screen.getByRole("article", { name: "Release decisions" });
    await user.type(within(releases).getByLabelText("Release"), "v1.4");
    await user.type(within(releases).getByLabelText("Justification (required)"), "Gate passes");
    await user.click(within(releases).getByRole("button", { name: "Approve release" }));
    await waitFor(() => expect(posts(sent).at(-1)).toMatchObject({ body: { decision: "approve" } }));
  });

  it("refuses a release label the API would refuse", async () => {
    const { client, ready } = api();
    await ready;
    const user = userEvent.setup();
    render(<Approvals api={client} project={project} userId="u-2" role="reviewer" findingId={null} />);
    const releases = await screen.findByRole("article", { name: "Release decisions" });
    await user.type(within(releases).getByLabelText("Release"), "v 1.4");
    await user.type(within(releases).getByLabelText("Justification (required)"), "x");
    expect(within(releases).getByRole("button", { name: "Reject release" })).toBeDisabled();
    expect(within(releases).getByLabelText("Release")).toHaveAttribute("aria-invalid", "true");
  });

  it("is read-only for a viewer", async () => {
    const { client, ready } = api({ "GET /v1/findings/f-1/waivers": json([WAIVER]) });
    await ready;
    render(<Approvals api={client} project={project} userId="u-3" role="viewer" findingId="f-1" />);
    await screen.findByRole("region", { name: "Waivers" });
    expect(screen.queryAllByRole("button").map((b) => b.textContent)).toEqual(["Refresh"]);
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("chooses waiver expiry by UTC day", () => {
    expect(utcDay("2026-11-27T23:30:00Z")).toBe("2026-11-27");
    expect(expiresAt("2026-12-15")).toBe("2026-12-15T00:00:00Z");
  });
});
