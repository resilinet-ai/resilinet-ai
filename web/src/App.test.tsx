import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Api } from "./api";
import { App } from "./App";
import { fakeFetch, json, ME, PROJECT, RUN, SUMMARY } from "./fake";

const PAIR = { access_token: "a1", refresh_token: "r1", token_type: "bearer", expires_in: 900 };
const GATE_BLOCKED = {
  verdict: "blocked", blocking_count: 1, waived_count: 0, waived: [],
  blocking: [{ finding_id: "f-1", title: "Duplicate txn_id in the feed", waiver_expires_at: null }],
};

function api(overrides: Record<string, ReturnType<typeof json>> = {}) {
  return fakeFetch({
    "POST /v1/auth/login": json(PAIR),
    "GET /v1/me": json(ME),
    "GET /v1/projects": json({ items: [PROJECT], next_cursor: null }),
    "GET /v1/assurance/runs": json({ items: [RUN], next_cursor: null }),
    "GET /v1/projects/p-1/gate": json(GATE_BLOCKED),
    "GET /v1/assurance/summary": json(SUMMARY),
    ...overrides,
  });
}

async function signIn(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Email"), "ada@example.test");
  await user.type(screen.getByLabelText("Password"), "pw");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
}

beforeEach(() => {
  window.location.hash = "";
  window.localStorage.clear();
});

describe("single sign-on (FR-01.3)", () => {
  it("offers the provider only when the API has one", async () => {
    const off = api();
    const { unmount } = render(<App api={new Api("", off.fetcher)} />);
    await waitFor(() => expect(off.sent.some((s) => s.path === "/v1/auth/sso")).toBe(true));
    expect(screen.queryByRole("link", { name: /Sign in with/ })).toBeNull();
    unmount();

    const on = api({ "GET /v1/auth/sso": json({ enabled: true, name: "Example SSO" }) });
    render(<App api={new Api("", on.fetcher)} />);
    const link = await screen.findByRole("link", { name: "Sign in with Example SSO" });
    expect(link).toHaveAttribute("href", "/v1/auth/sso/start");
  });

  it("exchanges the one-time code once and clears it from the address bar", async () => {
    window.location.hash = "#sso=one-time-code_123";
    const { fetcher, sent } = api({ "POST /v1/auth/sso/exchange": json(PAIR) });
    render(<App api={new Api("", fetcher)} />);
    await screen.findByRole("heading", { name: "Assurance overview" });
    const exchanges = sent.filter((s) => s.path === "/v1/auth/sso/exchange");
    expect(exchanges.map((s) => s.body)).toEqual([{ code: "one-time-code_123" }]);
    expect(window.location.hash).not.toContain("sso=");
    expect(sent.find((s) => s.path === "/v1/me")?.headers.Authorization).toBe("Bearer a1");
  });

  it("says why a single sign-on did not succeed, without details", async () => {
    window.location.hash = "#sso_error=sso.no_account";
    const { fetcher } = api();
    render(<App api={new Api("", fetcher)} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("not linked to a RAI user");
    expect(window.location.hash).toBe("");
  });

  it("treats a used or expired code as a failed sign-in", async () => {
    window.location.hash = "#sso=already-used";
    const { fetcher } = api({
      "POST /v1/auth/sso/exchange": json({ title: "Sign in again", code: "sso.invalid_code" }, 401),
    });
    render(<App api={new Api("", fetcher)} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("already used or has expired");
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
  });

  it("ignores a fragment that is not an SSO answer", async () => {
    window.location.hash = "#sso=<script>";
    const { fetcher, sent } = api();
    render(<App api={new Api("", fetcher)} />);
    await waitFor(() => expect(sent.some((s) => s.path === "/v1/auth/sso")).toBe(true));
    expect(sent.some((s) => s.path === "/v1/auth/sso/exchange")).toBe(false);
  });
});

describe("App", () => {
  it("shows the synthetic-data banner before and after sign-in (FR-11.2)", async () => {
    const { fetcher } = api();
    const user = userEvent.setup();
    render(<App api={new Api("", fetcher)} />);
    const notice = screen.getByRole("note", { name: "Data notice" });
    expect(notice).toHaveTextContent("RAI RUNS ON SANDBOX AND STAGING DATA ONLY");
    expect(within(notice).queryByRole("button")).toBeNull(); // it cannot be dismissed
    await signIn(user);
    await screen.findByRole("heading", { name: "Assurance overview" }); // the landing screen
    expect(screen.getByRole("note", { name: "Data notice" })).toHaveTextContent("Never production");
  });

  it("names an authorised-snapshot project in the banner", async () => {
    const { fetcher } = api({
      "GET /v1/projects": json({ items: [{ ...PROJECT, data_classification: "authorized_snapshot" }] }),
    });
    const user = userEvent.setup();
    render(<App api={new Api("", fetcher)} />);
    await signIn(user);
    await waitFor(() =>
      expect(screen.getByRole("note", { name: "Data notice" })).toHaveTextContent(
        "Project ledger-resilience uses an authorised snapshot",
      ),
    );
  });

  it("shows the server's sign-in error", async () => {
    const { fetcher } = api({
      "POST /v1/auth/login": json({ title: "Email or password is wrong", code: "auth.invalid_credentials" }, 401),
    });
    const user = userEvent.setup();
    render(<App api={new Api("", fetcher)} />);
    await signIn(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("Email or password is wrong");
  });

  it("lists runs with the release gate, and opens a run with its failing rules", async () => {
    const { fetcher, sent } = api({
      [`GET /v1/assurance/runs/${RUN.run_id}`]: json({
        ...RUN,
        steps: [{ step_key: "item-1", plan_position: 1, state: "done", verdict: "fail", error: null }],
        observations: { fail: 1, pass: 35 },
      }),
      [`GET /v1/assurance/runs/${RUN.run_id}/observations`]: json({ items: [], next_cursor: null }),
    });
    const user = userEvent.setup();
    window.location.hash = "#/runs";
    render(<App api={new Api("", fetcher)} />);
    await signIn(user);
    expect(await screen.findByText("✕ Release gate blocked · 1 critical")).toBeInTheDocument();
    expect(screen.getByText("Duplicate txn_id in the feed")).toBeInTheDocument();
    await user.click(await screen.findByRole("link", { name: RUN.run_id.slice(-8) }));
    expect(await screen.findByRole("heading", { name: /^Run / })).toBeInTheDocument();
    expect(screen.getByText("1 fail · 35 pass")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download evidence" })).toBeEnabled();
    expect(sent.some((s) => s.path.startsWith("/v1/assurance/runs?project_id=p-1"))).toBe(true);
  });

  it("switches theme, Midnight by default (D10)", async () => {
    const user = userEvent.setup();
    render(<App api={new Api("", api().fetcher)} />);
    expect(document.documentElement).toHaveAttribute("data-theme", "midnight");
    await user.selectOptions(screen.getByRole("combobox", { name: "Theme" }), "contrast");
    expect(document.documentElement).toHaveAttribute("data-theme", "contrast");
    expect(window.localStorage.getItem("rai-theme")).toBe("contrast");
  });

  it("marks screens that are not built yet as planned", async () => {
    const user = userEvent.setup();
    render(<App api={new Api("", api().fetcher)} />);
    await signIn(user);
    await user.click(await screen.findByRole("link", { name: /Models & drift/ }));
    expect(await screen.findByRole("heading", { name: "Models & drift" })).toBeInTheDocument();
    expect(screen.getByText("FR-09.1, FR-09.2")).toBeInTheDocument();
  });
});
