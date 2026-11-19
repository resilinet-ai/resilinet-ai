// WCAG 2.2 AA checks with axe-core on every built screen (NFR-USE-2).
//
// jsdom has no layout or computed colours, so axe's colour-contrast rule cannot run
// here; tests/unit/test_web_contrast.py checks every theme's colour pairs instead.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { Api } from "./api";
import { App } from "./App";
import { fakeFetch, json, ME, PROJECT, RUN, SUMMARY } from "./fake";
import { THEMES } from "./theme";

const PAIR = { access_token: "a1", refresh_token: "r1" };
const GATE = {
  verdict: "blocked", blocking_count: 1, waived_count: 0, waived: [],
  blocking: [{ finding_id: "f-1", title: "Duplicate txn_id in the feed", waiver_expires_at: null }],
};

function api() {
  return fakeFetch({
    "POST /v1/auth/login": json(PAIR),
    "GET /v1/me": json(ME),
    "GET /v1/projects": json({ items: [PROJECT], next_cursor: null }),
    "GET /v1/assurance/runs": json({ items: [RUN], next_cursor: null }),
    "GET /v1/projects/p-1/gate": json(GATE),
    "GET /v1/assurance/summary": json(SUMMARY),
    [`GET /v1/assurance/runs/${RUN.run_id}`]: json({
      ...RUN,
      steps: [{ step_key: "item-1", plan_position: 1, state: "done", verdict: "fail", error: null }],
      observations: { fail: 1, pass: 35 },
    }),
    [`GET /v1/assurance/runs/${RUN.run_id}/observations`]: json({
      items: [{ observation_id: "o-1", step_key: "item-1", rule: "txn_id.unique", outcome: "fail",
        row_count: 100000, failing_count: 100, expected: { severity: "critical" },
        observed_value: { message: "100 duplicate keys" } }],
      next_cursor: null,
    }),
  }).fetcher;
}

async function violations(): Promise<string[]> {
  const result = await axe.run(document.body, {
    runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"] },
    rules: { "color-contrast": { enabled: false } },
  });
  return result.violations.map(
    (v) => `${v.id}: ${v.help} (${v.nodes.map((n) => n.target.join(" ")).join(", ")})`,
  );
}

async function signIn() {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Email"), "ada@example.test");
  await user.type(screen.getByLabelText("Password"), "pw");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  return user;
}

beforeEach(() => {
  window.location.hash = "";
  window.localStorage.clear();
});

describe("WCAG 2.2 AA (axe-core)", () => {
  it("catches a violation, so a clean result means something", async () => {
    render(<button type="button" />);
    expect((await violations()).some((v) => v.startsWith("button-name"))).toBe(true);
  });

  it("gives every screen its own page title (2.4.2)", async () => {
    render(<App api={new Api("", api())} />);
    expect(document.title).toBe("Sign in · Resilinet-AI");
    const user = await signIn();
    await screen.findByText("96.1%");
    expect(document.title).toBe("Dashboard · Resilinet-AI");
    await user.click(screen.getByRole("link", { name: /^Runs/ }));
    await screen.findByRole("heading", { name: "Runs" });
    expect(document.title).toBe("Runs · Resilinet-AI");
  });

  it("sign-in", async () => {
    render(<App api={new Api("", api())} />);
    expect(await violations()).toEqual([]);
  });

  it("dashboard", async () => {
    render(<App api={new Api("", api())} />);
    await signIn();
    await screen.findByText("96.1%");
    expect(await violations()).toEqual([]);
  });

  it("runs, with a run open", async () => {
    window.location.hash = `#/runs/${RUN.run_id}`;
    render(<App api={new Api("", api())} />);
    await signIn();
    await screen.findAllByText("100 / 100,000"); // the fake answers the fail and error queries alike
    expect(await violations()).toEqual([]);
  });

  it("a planned screen", async () => {
    window.location.hash = "#/recovery";
    render(<App api={new Api("", api())} />);
    await signIn();
    await screen.findByRole("heading", { name: "Recovery" });
    expect(await violations()).toEqual([]);
  });

  it.each(THEMES.map((t) => t.id))("theme %s keeps the page valid", async (theme) => {
    window.localStorage.setItem("rai-theme", theme);
    render(<App api={new Api("", api())} />);
    expect(document.documentElement).toHaveAttribute("data-theme", theme);
    expect(await violations()).toEqual([]);
  });
});
