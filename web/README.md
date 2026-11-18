# Web UI

The RAI web UI: React and TypeScript (ADR-004), built with Vite. Its design is the wireframe set in [`docs/design/rai-wireframes.md`](../docs/design/rai-wireframes.md), and its themes are the wireframes' four: Midnight by default, Clarity, Evergreen and High contrast (decision D10).

![Runs, Midnight theme](../docs/ui/runs.png)

## What it does so far

| Screen | Status | API |
| --- | --- | --- |
| Sign-in and tenant choice (W10) | Built | `POST /v1/auth/login`, `GET /v1/me` |
| Runs (W02) | Built: a project's runs, a run's steps and failing rules, the release gate, and the evidence download | `/v1/assurance/runs`, `/observations`, `/v1/projects/{id}/gate`, `/evidence` |
| Dashboard, Chaos scenarios, Contracts & data, Approvals & gates, Evidence, Environments, Recovery, Models & drift | In the navigation, marked *planned*, with their requirements | — |

- **Synthetic-data banner (FR-11.2, B-37).** On every screen, sign-in included, and it cannot be dismissed. When the selected project is an authorised snapshot, the banner names it.
- **Read-only.** This slice only reads; actions (approving, waiving, starting runs) come with their screens.

## Develop

```bash
cd web
npm ci                 # exact versions from package-lock.json
npm run dev            # http://127.0.0.1:5173, forwarding /v1 to `rai serve` on 127.0.0.1:8000
npm run check          # type-check, tests (Vitest + Testing Library) and a production build
```

`RAI_API_URL` points the dev server at another API. Run the API and a worker as in [`docs/api.md`](../docs/api.md).

## In production

`deploy/caddy/Dockerfile` builds the UI (running `npm run check` first) into the Caddy image, so the UI and the API share one HTTPS origin ([`docs/deployment.md`](../docs/deployment.md)). The CI job "Web UI" runs the same checks on every pull request.

## Security

- **Tokens in memory only.** The access and refresh tokens are never written to `localStorage`, `sessionStorage` or cookies; a reload signs you out. A 401 triggers one refresh, which rotates the refresh token, and one retry. Signing out revokes the refresh token on the server.
- **Content-Security-Policy.** Caddy serves the UI with `default-src 'self'` and `script-src 'self'`: no inline scripts, no third-party origins, no framing. The build emits no inline scripts or styles.
- **No HTML from data.** Everything the API returns is rendered as text by React; the UI never sets HTML.
- **Only a theme in storage.** `localStorage` holds the theme choice and nothing else, and the UI works without storage.

## Dependencies

Two runtime dependencies (React and React DOM) and a small development set, each pinned to an exact version. New versions are installed with `npm install --before=<a date 14 days ago>`, so nothing younger than two weeks is resolved, even transitively (decision DEP-COOLDOWN); Dependabot waits 14 days too.
