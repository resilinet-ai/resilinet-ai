import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Api, type Project } from "../api";
import { memberJson, storedMembers } from "../files";
import { BUNDLE_INFO, bundleZip, fakeFetch, json, PROJECT, PUBLIC_KEY, RUN, storedZip } from "../fake";
import { Evidence, short, size } from "./Evidence";

const project = PROJECT as Project;
const RUNNING = { ...RUN, run_id: "0193a5b0-0000-7000-8000-0000000000aa", state: "running", finished_at: null };
const PASSED = { ...RUN, run_id: "0193a5b0-0000-7000-8000-0000000000bb", state: "passed",
  finished_at: "2026-11-19T10:00:00Z" };
const FILE = `rai-evidence-${RUN.run_id}.zip`;
const MANIFEST_SHA = "3e7a90c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8d0";

type Routes = Parameters<typeof fakeFetch>[0];

function bundle(headers: Record<string, string> = { "X-RAI-Signature-Key-Id": PUBLIC_KEY.key_id }, zip = bundleZip()) {
  return { status: 200, body: zip, headers: { "Content-Type": "application/zip",
    "Content-Disposition": `attachment; filename="${FILE}"`, "X-RAI-Manifest-SHA256": MANIFEST_SHA, ...headers } };
}

function api(overrides: Routes = {}) {
  const fake = fakeFetch({
    "POST /v1/auth/login": json({ access_token: "a", refresh_token: "r" }),
    "GET /v1/me": json({}),
    "GET /v1/assurance/runs": json({ items: [RUNNING, PASSED, RUN], next_cursor: null }),
    "GET /v1/evidence/public-key": json(PUBLIC_KEY),
    [`GET /v1/assurance/runs/${RUN.run_id}/evidence`]: bundle(),
    ...overrides,
  });
  const client = new Api("", fake.fetcher);
  return { client, sent: fake.sent, ready: client.login("ada@example.test", "pw") };
}

let saved: string[] = [];

beforeEach(() => {
  saved = [];
  URL.createObjectURL = vi.fn(() => "blob:bundle");
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    saved.push(this.download);
  });
});

afterEach(() => vi.restoreAllMocks());

describe("Evidence (FR-13.1, FR-13.2, NFR-AUD-2)", () => {
  it("offers only finished runs, starting from the one in the address", async () => {
    const { client, ready } = api();
    await ready;
    render(<Evidence api={client} project={project} runId={RUN.run_id} />);
    const select = await screen.findByLabelText("Run");
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "000000bb · passed · finished 2026-11-19 10:00 UTC",
      "00000001 · failed · finished 2026-11-18 09:00 UTC",
    ]);
    expect(select).toHaveValue(RUN.run_id);
    expect(screen.getByText("Nothing exported yet in this session.")).toBeInTheDocument();
  });

  it("saves the bundle and shows its manifest, anchor, signature and limitations", async () => {
    const { client, sent, ready } = api();
    await ready;
    const user = userEvent.setup();
    render(<Evidence api={client} project={project} runId={RUN.run_id} />);
    await user.click(await screen.findByRole("button", { name: "Export bundle" }));
    const last = screen.getByRole("article", { name: "Last export" });
    expect(await within(last).findByText(FILE)).toBeInTheDocument();
    expect(saved).toEqual([FILE]);
    expect(sent.filter((s) => s.path.endsWith("/evidence"))).toHaveLength(1);
    expect(within(last).getByText("sha256 3e7a90…f8d0 · 3 files")).toBeInTheDocument();
    expect(within(last).getByText("audit entry seq 1,204 · 8d2c41…0b7b")).toBeInTheDocument();
    expect(within(last).getByText(`Ed25519 · key ${PUBLIC_KEY.key_id}`)).toBeInTheDocument();
    expect(within(last).getByText(/^2 steps · 36 observations · 3 lineage edges/)).toBeInTheDocument();
    expect(within(last).getByText(/Results hold only for the pinned inputs/)).toBeInTheDocument();
    expect(within(last).getByLabelText("Verify command"))
      .toHaveTextContent(`rai verify ${FILE} --key ${PUBLIC_KEY.public_key}`);
    expect(within(last).getByText(BUNDLE_INFO.statement)).toBeInTheDocument();
    expect(within(last).queryByRole("note")).toBeNull(); // same key as the server's
  });

  it("says when the server does not sign, and verifies with --allow-unsigned", async () => {
    const { client, ready } = api({
      "GET /v1/evidence/public-key": json({ title: "Evidence is not signed", code: "evidence.unsigned" }, 404),
      [`GET /v1/assurance/runs/${RUN.run_id}/evidence`]: bundle({}, bundleZip({ ...BUNDLE_INFO, signature: null })),
    });
    await ready;
    const user = userEvent.setup();
    render(<Evidence api={client} project={project} runId={RUN.run_id} />);
    expect(await screen.findByText(/This server does not sign bundles/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Export bundle" }));
    const last = screen.getByRole("article", { name: "Last export" });
    expect(await within(last).findByText("▲ unsigned")).toBeInTheDocument();
    expect(within(last).getByLabelText("Verify command")).toHaveTextContent(`rai verify ${FILE} --allow-unsigned`);
  });

  it("warns when the bundle's key is not the server's key now", async () => {
    const { client, ready } = api({ "GET /v1/evidence/public-key": json({ ...PUBLIC_KEY, key_id: "aaaabbbbccccdddd" }) });
    await ready;
    const user = userEvent.setup();
    render(<Evidence api={client} project={project} runId={RUN.run_id} />);
    await user.click(await screen.findByRole("button", { name: "Export bundle" }));
    expect(await screen.findByRole("note")).toHaveTextContent("the server now signs with aaaabbbbccccdddd");
  });

  it("shows the API's refusal", async () => {
    const { client, ready } = api({
      [`GET /v1/assurance/runs/${RUN.run_id}/evidence`]: json({ title: "The run has no audit entries",
        code: "evidence.no_audit_trail" }, 409),
    });
    await ready;
    const user = userEvent.setup();
    render(<Evidence api={client} project={project} runId={RUN.run_id} />);
    await user.click(await screen.findByRole("button", { name: "Export bundle" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The run has no audit entries");
    expect(saved).toEqual([]);
  });

  it("explains that a run has evidence once it finishes", async () => {
    const { client, ready } = api({ "GET /v1/assurance/runs": json({ items: [RUNNING], next_cursor: null }) });
    await ready;
    render(<Evidence api={client} project={project} runId={null} />);
    expect(await screen.findByText(/No finished run in this project yet/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Export bundle" })).toBeNull();
  });

  it("starts from the newest finished run without one in the address", async () => {
    const { client, ready } = api();
    await ready;
    render(<Evidence api={client} project={project} runId={null} />);
    await waitFor(() => expect(screen.getByLabelText("Run")).toHaveValue(PASSED.run_id));
  });

  it("reads stored ZIP members, and refuses what is not a ZIP", () => {
    const members = storedMembers(storedZip({ "b/bundle.json": '{"a":1}', "b/x.txt": "hi" }).slice().buffer);
    expect([...members.keys()]).toEqual(["b/bundle.json", "b/x.txt"]);
    expect(memberJson(members, "bundle.json")).toEqual({ a: 1 });
    expect(memberJson(members, "missing.json")).toBeNull();
    expect(() => storedMembers(new Uint8Array(40).buffer)).toThrow("not a ZIP file");
    expect(short("8d2c41a0e5f97b")).toBe("8d2c41…f97b");
    expect([size(900), size(412 * 1024), size(3 * 1024 * 1024)]).toEqual(["900 B", "412 KB", "3.0 MB"]);
  });
});
