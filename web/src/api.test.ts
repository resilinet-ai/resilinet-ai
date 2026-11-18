import { Api, ApiError } from "./api";
import { fakeFetch, json, ME } from "./fake";

const PAIR = { access_token: "a1", refresh_token: "r1", token_type: "bearer", expires_in: 900 };

describe("Api", () => {
  it("signs in, then sends the token and the tenant on every call", async () => {
    const { fetcher, sent } = fakeFetch({
      "POST /v1/auth/login": json(PAIR),
      "GET /v1/me": json(ME),
      "GET /v1/projects": json({ items: [], next_cursor: null }),
    });
    const api = new Api("", fetcher);
    expect(await api.login("ada@example.test", "pw")).toEqual(ME);
    api.tenantId = "t-1";
    await api.get("/v1/projects");
    expect(sent[0]?.headers.Authorization).toBeUndefined();
    expect(sent[0]?.body).toEqual({ email: "ada@example.test", password: "pw" });
    expect(sent[2]?.headers).toMatchObject({ Authorization: "Bearer a1", "X-RAI-Tenant": "t-1" });
  });

  it("refreshes once on 401, rotating the refresh token, and retries", async () => {
    let calls = 0;
    const { fetcher, sent } = fakeFetch({
      "POST /v1/auth/login": json(PAIR),
      "GET /v1/me": json(ME),
      "POST /v1/auth/refresh": json({ ...PAIR, access_token: "a2", refresh_token: "r2" }),
      "GET /v1/projects": () => (++calls === 1 ? json({ title: "Expired" }, 401) : json({ items: [] })),
    });
    const api = new Api("", fetcher);
    await api.login("ada@example.test", "pw");
    await api.get("/v1/projects");
    const refresh = sent.find((s) => s.path === "/v1/auth/refresh");
    expect(refresh?.body).toEqual({ refresh_token: "r1" });
    expect(sent.at(-1)?.headers.Authorization).toBe("Bearer a2");
    expect(api.signedIn).toBe(true);
  });

  it("signs out when the refresh token is refused", async () => {
    const { fetcher } = fakeFetch({
      "POST /v1/auth/login": json(PAIR),
      "GET /v1/me": json(ME),
      "POST /v1/auth/refresh": json({ title: "Sign in again", code: "auth.invalid_refresh_token" }, 401),
      "GET /v1/projects": json({ title: "Expired" }, 401),
    });
    const api = new Api("", fetcher);
    await api.login("ada@example.test", "pw");
    await expect(api.get("/v1/projects")).rejects.toBeInstanceOf(ApiError);
    expect(api.signedIn).toBe(false);
  });

  it("turns problem details into ApiError, with Retry-After", async () => {
    const { fetcher } = fakeFetch({
      "POST /v1/auth/login": json(
        { title: "Too many failed sign-ins; try again later", code: "auth.too_many_attempts" },
        429,
        { "Retry-After": "42" },
      ),
    });
    const error = await new Api("", fetcher).login("a@b.test", "x").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).problem).toEqual({
      status: 429,
      code: "auth.too_many_attempts",
      title: "Too many failed sign-ins; try again later",
      retryAfter: 42,
    });
  });

  it("sign-out revokes the refresh token and forgets the session", async () => {
    const { fetcher, sent } = fakeFetch({
      "POST /v1/auth/login": json(PAIR),
      "GET /v1/me": json(ME),
      "POST /v1/auth/logout": { status: 204 },
    });
    const api = new Api("", fetcher);
    await api.login("ada@example.test", "pw");
    await api.logout();
    expect(sent.at(-1)?.body).toEqual({ refresh_token: "r1" });
    expect(api.signedIn).toBe(false);
  });
});
