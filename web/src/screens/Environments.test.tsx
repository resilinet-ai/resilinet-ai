import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Api } from "../api";
import { ENVIRONMENTS, fakeFetch, json, TARGETS } from "../fake";
import { Environments, nameProblem, targetProblems } from "./Environments";

type Routes = Parameters<typeof fakeFetch>[0];

function api(overrides: Routes = {}) {
  const fake = fakeFetch({
    "POST /v1/auth/login": json({ access_token: "a", refresh_token: "r" }),
    "GET /v1/me": json({}),
    "GET /v1/environments": json(ENVIRONMENTS),
    "GET /v1/environments/e-1/targets": json(TARGETS),
    "GET /v1/environments/e-3/targets": json({ items: [], next_cursor: null }),
    ...overrides,
  });
  const client = new Api("", fake.fetcher);
  return { client, sent: fake.sent, ready: client.login("ada@example.test", "pw") };
}

function writes(sent: { method: string; path: string; body: unknown }[]) {
  return sent.filter((s) => s.method !== "GET" && !s.path.startsWith("/v1/auth"));
}

describe("Environments (FR-07.2, NFR-SAF-1)", () => {
  it("lists active environments first and shows the allow-list, read-only for a viewer", async () => {
    const { client, ready } = api();
    await ready;
    render(<Environments api={client} role="viewer" environmentId={null} />);
    expect(await screen.findByRole("heading", { name: "SANDBOX-01 · target allow-list" })).toBeInTheDocument();
    const list = screen.getByRole("article", { name: "Environments" });
    expect(within(list).getAllByRole("link").map((a) => a.querySelector("b")!.textContent))
      .toEqual(["SANDBOX-01", "STAGING-01", "SANDBOX-OLD"]);
    expect(within(list).getByRole("link", { name: /SANDBOX-01/ })).toHaveAttribute("aria-current", "true");
    expect(within(list).getByText("⊘ retired")).toBeInTheDocument();
    expect(within(list).getByText("staging · ceiling 30.0 min")).toBeInTheDocument();
    const rows = (await screen.findAllByRole("row")).slice(1) as HTMLTableRowElement[];
    expect(rows.map((r) => r.cells[0]!.textContent)).toEqual(["ledger-db", "ledger-db-proxy", "payments-model"]);
    expect(within(rows[0]!).getByText("sha256:abababababab…")).toBeInTheDocument();
    expect(within(rows[2]!).getByText("scores rows at http://payments-api:8000/v1/score")).toBeInTheDocument();
    expect(screen.queryAllByRole("button").map((b) => b.textContent)).toEqual(["Refresh"]);
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("lets an admin register an environment, and refuses a production name with the reason", async () => {
    const { client, sent, ready } = api({ "POST /v1/environments": json({}, 201) });
    await ready;
    const user = userEvent.setup();
    render(<Environments api={client} role="admin" environmentId={null} />);
    const form = await screen.findByRole("form", { name: "Register an environment" });
    const name = within(form).getByLabelText("Name");
    await user.type(name, "prod-mirror");
    expect(name).toHaveValue("PROD-MIRROR");
    expect(name).toHaveAttribute("aria-invalid", "true");
    expect(within(form).getByText(/Names may not contain PROD/)).toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Register environment" })).toBeDisabled();
    await user.clear(name);
    await user.type(name, "sandbox-02");
    await user.selectOptions(within(form).getByLabelText("Type"), "STAGING");
    await user.click(within(form).getByRole("button", { name: "Register environment" }));
    await waitFor(() => expect(writes(sent)).toHaveLength(1));
    expect(writes(sent)[0]).toMatchObject({ method: "POST", path: "/v1/environments",
      body: { name: "SANDBOX-02", type: "STAGING", max_fault_duration_seconds: 900 } });
  });

  it("adds a container target only with its label and image", async () => {
    const { client, sent, ready } = api({ "POST /v1/environments/e-1/targets": json({}, 201) });
    await ready;
    const user = userEvent.setup();
    render(<Environments api={client} role="admin" environmentId="e-1" />);
    const form = await screen.findByRole("form", { name: "Allow a target" });
    await user.type(within(form).getByLabelText("Target name"), "payments-api");
    await user.type(within(form).getByLabelText("Required label"), "rai.target=payments-api");
    expect(within(form).getByText("Still needed: its image ID (sha256: and 64 hex).")).toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Add target" })).toBeDisabled();
    await user.type(within(form).getByLabelText("Image ID"), `sha256:${"cd".repeat(32)}`);
    await user.click(within(form).getByRole("button", { name: "Add target" }));
    await waitFor(() => expect(writes(sent)).toHaveLength(1));
    expect(writes(sent)[0]!.body).toEqual({ kind: "container", name: "payments-api",
      required_label: "rai.target=payments-api", image_digest: `sha256:${"cd".repeat(32)}` });
  });

  it("removes a target after a second click", async () => {
    const { client, sent, ready } = api({ "DELETE /v1/environments/e-1/targets/t-2": { status: 204 } });
    await ready;
    const user = userEvent.setup();
    render(<Environments api={client} role="admin" environmentId="e-1" />);
    await user.click(await screen.findByRole("button", { name: "Remove ledger-db-proxy" }));
    expect(writes(sent)).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Yes, remove ledger-db-proxy" }));
    await waitFor(() => expect(writes(sent)).toMatchObject([
      { method: "DELETE", path: "/v1/environments/e-1/targets/t-2" }]));
  });

  it("changes the ceiling and retires with the revision it read (If-Match)", async () => {
    const { client, sent, ready } = api({ "PATCH /v1/environments/e-1": json(ENVIRONMENTS.items[2]) });
    await ready;
    const user = userEvent.setup();
    render(<Environments api={client} role="admin" environmentId="e-1" />);
    const form = await screen.findByRole("form", { name: "Change SANDBOX-01" });
    const ceiling = within(form).getByLabelText(/Fault ceiling/);
    await user.clear(ceiling);
    await user.type(ceiling, "600");
    await user.click(within(form).getByRole("button", { name: "Save ceiling" }));
    await user.click(within(form).getByRole("button", { name: "Retire environment" }));
    await user.click(within(form).getByRole("button", { name: "Yes, retire SANDBOX-01" }));
    await waitFor(() => expect(writes(sent)).toHaveLength(2));
    expect(writes(sent)).toMatchObject([
      { method: "PATCH", headers: { "If-Match": '"v2"' }, body: { max_fault_duration_seconds: 600 } },
      { method: "PATCH", headers: { "If-Match": '"v2"' }, body: { status: "retired" } },
    ]);
  });

  it("shows why a change was refused", async () => {
    const { client, ready } = api({
      "PATCH /v1/environments/e-1": json({ title: "The resource has changed; read it again", code: "request.stale" }, 412),
    });
    await ready;
    const user = userEvent.setup();
    render(<Environments api={client} role="admin" environmentId="e-1" />);
    const form = await screen.findByRole("form", { name: "Change SANDBOX-01" });
    await user.click(within(form).getByRole("button", { name: "Retire environment" }));
    await user.click(within(form).getByRole("button", { name: "Yes, retire SANDBOX-01" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("The resource has changed");
  });

  it("disables new targets on a retired environment, with the reason", async () => {
    const { client, ready } = api();
    await ready;
    render(<Environments api={client} role="admin" environmentId="e-3" />);
    expect(await screen.findByText(/No target is allowed here yet/)).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Allow a target" })).toBeNull();
    expect(screen.getByRole("button", { name: "Add target" })).toBeDisabled();
    expect(screen.getByText("A retired environment takes no new targets.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reactivate" })).toBeEnabled();
  });

  it("checks names and targets as the API does", () => {
    expect(nameProblem("SANDBOX-02")).toBeNull();
    expect(nameProblem("PRODUCTION")).toMatch(/PROD/);
    expect(nameProblem("sandbox")).toMatch(/Capital letters/);
    const base = { kind: "proxy" as const, name: "p", label: "", image: "", health: "", score: "" };
    expect(targetProblems(base)).toEqual(["the label it carries"]);
    expect(targetProblems({ ...base, kind: "db" })).toEqual([]);
    expect(targetProblems({ ...base, kind: "http", health: "ftp://x" }))
      .toEqual(["a health URL starting http:// or https://"]);
  });
});
