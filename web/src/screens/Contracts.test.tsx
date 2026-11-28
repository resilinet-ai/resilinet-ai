import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Api, type ContractSpec, type Project } from "../api";
import { ASSETS, BATCH_OBSERVATIONS, CONTRACT_V2, CONTRACTS, fakeFetch, json, PROJECT, RUN } from "../fake";
import { columnRules, Contracts } from "./Contracts";

const project = PROJECT as Project;
const RUNNING = { ...RUN, run_id: "0193a5b0-0000-7000-8000-0000000000aa", state: "running", finished_at: null };
const OLDER = { ...RUN, run_id: "0193a5b0-0000-7000-8000-0000000000bb", state: "passed" };

type Routes = Parameters<typeof fakeFetch>[0];

function api(overrides: Routes = {}) {
  const fake = fakeFetch({
    "POST /v1/auth/login": json({ access_token: "a", refresh_token: "r" }),
    "GET /v1/me": json({}),
    "GET /v1/projects/p-1/assets": json(ASSETS),
    "GET /v1/assets/a-1/contracts": json(CONTRACTS),
    "GET /v1/assets/a-1/contracts/2": json(CONTRACT_V2),
    "GET /v1/assets/a-1/contracts/1": json({ ...CONTRACT_V2, ...CONTRACTS.items[0],
      spec: { ...CONTRACT_V2.spec, version: 1 } }),
    "GET /v1/assets/a-1/inputs": json({ items: [], next_cursor: null }),
    "GET /v1/assets/a-2/contracts": json({ items: [], next_cursor: null }),
    "GET /v1/assets/a-2/inputs": json({ items: [{ input_id: "i-1", input_asset_id: "a-1",
      input_name: "transactions_raw", input_kind: "source", transform_ref: "etl-job@4f2a9c1" }], next_cursor: null }),
    "GET /v1/assurance/runs": json({ items: [RUNNING, RUN, OLDER], next_cursor: null }),
    [`GET /v1/assurance/runs/${RUN.run_id}/observations`]: json(BATCH_OBSERVATIONS),
    [`GET /v1/assurance/runs/${OLDER.run_id}/observations`]: json({ items: [], next_cursor: null }),
    ...overrides,
  });
  const client = new Api("", fake.fetcher);
  return { client, sent: fake.sent, ready: client.login("ada@example.test", "pw") };
}

describe("Contracts & data (FR-03.1–03.4, FR-04.1)", () => {
  it("shows the asset's latest contract and its columns' checks, read-only for a viewer", async () => {
    const { client, ready } = api();
    await ready;
    render(<Contracts api={client} project={project} role="viewer" assetId="a-1" />);
    expect(await screen.findByRole("heading", { name: "transactions_raw · contract v2" })).toBeInTheDocument();
    const assets = screen.getByRole("article", { name: "Assets" });
    expect(within(assets).getAllByRole("link").map((a) => a.querySelector("b")!.textContent))
      .toEqual(["transactions_clean", "transactions_raw"]);
    expect(within(assets).getByRole("link", { name: /transactions_raw/ })).toHaveAttribute("aria-current", "true");
    expect(screen.getByLabelText("Version")).toHaveValue("2");
    expect(screen.getByText(/sha256 5b1f0c…9a0b/)).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Columns of transactions_raw@2" });
    const rows = within(table).getAllByRole("row").slice(1) as HTMLTableRowElement[];
    expect(rows.map((r) => r.cells[0]!.textContent)).toEqual(
      ["txn_id", "account_id", "channel", "amount", "created_at", "is_fraud"]);
    expect(rows[0]!.cells[2]!.textContent).toBe("required · unique · unique across batches");
    expect(rows[2]!.cells[2]!.textContent).toBe("required · allowed [card_present … p2p]");
    expect(rows[5]!.cells[3]!.textContent).toBe("warn");
    expect(screen.queryByText("New contract version")).toBeNull();
  });

  it("shows what the newest finished run found for this asset only, with the schema drift", async () => {
    const { client, ready } = api();
    await ready;
    render(<Contracts api={client} project={project} role="viewer" assetId="a-1" />);
    const batch = await screen.findByRole("table", { name: "Rule results of the last batch" });
    const rows = within(batch).getAllByRole("row").slice(1) as HTMLTableRowElement[];
    expect(rows.map((r) => r.cells[0]!.querySelector("code")!.textContent))
      .toEqual(["schema", "account_id.required", "unique_across_batches:txn_id", "is_fraud.required"]);
    expect(within(rows[2]!).getByText("✕ 5 failing")).toBeInTheDocument();
    expect(within(rows[2]!).getByText("5 rows fail")).toBeInTheDocument();
    expect(within(rows[3]!).getByText("warn")).toBeInTheDocument();
    expect(screen.getByText(/100,000 rows · against transactions_raw@2 · 4 rules/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "00000001" })).toHaveAttribute("href", `#/runs/${RUN.run_id}`);
    const drift = screen.getByRole("group", { name: "Schema drift" });
    expect(drift).toHaveTextContent("Schema drift · breaking");
    expect(drift).toHaveTextContent("column 'merchant_id' renamed to 'merchant' (breaking)");
  });

  it("says when no recent run checked the asset, and shows what it is derived from", async () => {
    const { client, ready } = api();
    await ready;
    render(<Contracts api={client} project={project} role="viewer" assetId="a-2" />);
    expect(await screen.findByText(/No contract is published for this asset/)).toBeInTheDocument();
    expect(await screen.findByText(/No finished run among the project's newest 5 checked this asset/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "transactions_raw" })).toHaveAttribute("href", "#/contracts/a-1");
    expect(screen.getByText("etl-job@4f2a9c1")).toBeInTheDocument();
  });

  it("reads an earlier version on request", async () => {
    const { client, sent, ready } = api();
    await ready;
    const user = userEvent.setup();
    render(<Contracts api={client} project={project} role="viewer" assetId="a-1" />);
    await user.selectOptions(await screen.findByLabelText("Version"), "1");
    expect(await screen.findByRole("heading", { name: "transactions_raw · contract v1" })).toBeInTheDocument();
    expect(sent.some((s) => s.path === "/v1/assets/a-1/contracts/1")).toBe(true);
  });

  it("lets an engineer publish the next version, starting from the latest with its number raised", async () => {
    const published = { ...CONTRACT_V2, contract_id: "c-3", version: 3, ref: "transactions_raw@3" };
    const { client, sent, ready } = api({ "POST /v1/assets/a-1/contracts": json(published, 201) });
    await ready;
    const user = userEvent.setup();
    render(<Contracts api={client} project={project} role="engineer" assetId="a-1" />);
    await screen.findByRole("heading", { name: "transactions_raw · contract v2" });
    await user.click(screen.getByText("New contract version"));
    const form = screen.getByRole("form", { name: "Publish a contract version" });
    const source = within(form).getByLabelText("Contract (YAML or JSON)");
    expect((source as HTMLTextAreaElement).value).toMatch(/^contract: transactions_raw\nversion: 3\n/);
    expect(within(form).getByText(/Set version: to 3 or later and keep the name transactions_raw/)).toBeInTheDocument();
    await user.click(within(form).getByRole("button", { name: "Publish version" }));
    await waitFor(() => expect(sent.filter((s) => s.method === "POST" && s.path.includes("contracts"))).toHaveLength(1));
    const post = sent.find((s) => s.method === "POST" && s.path.includes("contracts"))!;
    expect((post.body as { source: string }).source).toContain("version: 3");
  });

  it("lists every problem when the API refuses a contract", async () => {
    const { client, ready } = api({
      "POST /v1/assets/a-1/contracts": json({ title: "The contract is not valid", code: "contracts.invalid",
        errors: [{ location: "body.source", message: "columns.amount: min must be a number" },
          { location: "body.source", message: "freshness.max_lag: not a duration" }] }, 422),
    });
    await ready;
    const user = userEvent.setup();
    render(<Contracts api={client} project={project} role="engineer" assetId="a-1" />);
    await screen.findByRole("heading", { name: "transactions_raw · contract v2" });
    await user.click(screen.getByText("New contract version"));
    await user.click(screen.getByRole("button", { name: "Publish version" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The contract is not valid");
    expect(within(alert).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "columns.amount: min must be a number", "freshness.max_lag: not a duration"]);
  });

  it("names the checks a column asks for, as the contract does", () => {
    const spec = CONTRACT_V2.spec as ContractSpec;
    expect(columnRules("amount", spec.columns.amount!, spec))
      .toEqual(["required", "0.01 to 25000", "at most accounts.txn_limit (by account_id)"]);
    expect(columnRules("created_at", spec.columns.created_at!, spec)).toEqual(["required", "freshness ≤ 1h"]);
    expect(columnRules("x", { type: "string", min: 1 }, spec)).toEqual(["at least 1"]);
  });
});
