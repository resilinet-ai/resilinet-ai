# Logs, traces and health

How RAI's processes report what they are doing (NFR-OPS-1): structured logs, trace context and health endpoints. Standard library only; nothing to install, no collector required.

## Logs

Every RAI process writes **one JSON object per line on stderr**: `rai serve` (service `rai-api`), `rai worker` (`rai-worker`), the chaos agent (`rai-chaos-agent`) and its watchdog (`rai-chaos-watchdog`). SandboxBank's ETL already did (one `"event": "cycle"` line per cycle).

```json
{"time": "2026-11-19T11:15:57.300Z", "level": "info", "service": "rai-api", "logger": "rai.trace",
 "message": "http.request ok in 6.3 ms", "event": "span", "span": "http.request",
 "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", "span_id": "5975e565dc782fe6",
 "parent_span_id": "00f067aa0ba902b7", "method": "GET", "route": "/readyz", "status_code": 200,
 "status": "ok", "duration_ms": 6.334}
```

| Field | Always | Meaning |
| --- | --- | --- |
| `time`, `level`, `logger`, `service`, `message` | yes | UTC to the millisecond; `level` in lower case |
| `trace_id`, `span_id` | inside a span | The trace the line belongs to |
| `error`, `stack` | on an exception | `{"type", "message"}` and the traceback |
| anything else | per event | Ids and counts, e.g. `run_id`, `tenant_id`, `attempt`, `route`, `status_code` |

- `RAI_LOG_FORMAT=text` gives readable lines for a terminal, with the first 8 characters of the trace id; `RAI_LOG_LEVEL` is `DEBUG`, `INFO` (default), `WARNING` or `ERROR`. A bad value stops the process with exit code 2.
- **What is never logged:** request or response bodies, headers, tokens, passwords, secret values, query strings and e-mail addresses. Log ids and counts. A failed readiness check logs the error's type only. An unexpected exception (a run the worker must retry, say) is logged with its message and stack, so a database driver's error may name the host it tried; never a password.
- Uvicorn's access log is off: the request spans below replace it, with the route template (`/v1/assurance/runs/{run_id}`) instead of the raw path.

## Traces

RAI follows **W3C Trace Context**. A *span* is one unit of work; when it ends it writes one line with `"event": "span"`, its `span`, `trace_id`, `span_id`, `parent_span_id`, `duration_ms` and `status` (`ok` or `error`). Any log pipeline can rebuild a trace from those lines.

| Span | Where | Attributes |
| --- | --- | --- |
| `http.request` | Every API request | `method`, `route`, `status_code`; `status` is `error` for a 5xx |
| `worker.run` | Every job the worker executes | `run_id`, `tenant_id`, `attempt`, `worker`, `outcome` (`finished`, `released` or `lease_lost`) |

- **Propagation.** The API continues the caller's `traceparent` header when it is valid (version `00`, non-zero ids) and starts a new trace otherwise. Every response carries `traceparent` naming the request's span, so a client or CI log can quote it.
- **Logs inside a span** carry its `trace_id` and `span_id`, so every line a request or a run writes can be found from one id.
- **Not yet:** a run started through the API is a new trace on the worker; the two join on `run_id`, not on the trace id. There is no exporter: spans are log lines. Sending them to an OpenTelemetry collector would be an exporter added later, without changing the ids.

## Health

| Process | Liveness | Readiness |
| --- | --- | --- |
| `rai serve` | `GET /healthz`: 200 while the process answers; never touches the database. Routed through Caddy | `GET /readyz`: 200 when the database answers as `rai_api`, else 503. Internal only: Caddy does not route it. The API image's Docker healthcheck uses it, and Caddy starts only once it passes |
| `rai worker --health-port 8081` | `GET /healthz`: 200 while the worker has shown a sign of life (a poll, a claimed job or a lease renewal) within one lease (5 min); a worker silent that long has lost its job to another anyway | `GET /readyz`: alive and the database answers |
| Chaos agent | `GET /healthz` on its port | — |
| Chaos watchdog | `rai-chaos-agent watchdog-health`: swept recently (its Docker healthcheck) | — |

The worker's health port binds `127.0.0.1` unless `--health-host` says otherwise, and is off without `--health-port`. It answers only these two paths and does not log the probes.

Tests: `tests/unit/test_telemetry.py`, `tests/integration/test_ops.py`.
