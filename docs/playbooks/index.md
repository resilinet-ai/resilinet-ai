# Playbooks

Step-by-step procedures for the jobs people do with RAI. Each says who does it, what you need first, the steps, how to tell it worked, and what to do when it doesn't. They build on the [quick start](../quickstart.md)'s local platform; the [API reference](../api.md) has every endpoint and error code.

| Playbook | Who | When |
| --- | --- | --- |
| [Gate a pipeline in CI](ci-gate.md) | engineer | A contract should block a pull request that would let bad data through |
| [Triage a failing run](triage.md) | engineer, then a reviewer | A run failed, or the release gate is blocked |
| [Run a chaos scenario](chaos-scenario.md) | engineer, a reviewer, an admin | You want to know whether the system recovers within its RTO and RPO |
| [Stop every fault now](stop-all-faults.md) | anyone with access to the agent's host | A fault is hurting more than intended, or anything looks wrong during a scenario |
| [Hand over and verify evidence](evidence.md) | anyone; the verifier needs no RAI account | Someone outside the team needs proof of what a run observed |

**Conventions.** Commands are for a POSIX shell (Git Bash on Windows). `$API` is the API's address (`http://127.0.0.1:8000` locally) and `$AUTH` is `authorization: Bearer <access token>` from `POST /v1/auth/login`, for example:

```bash
export API=http://127.0.0.1:8000
ACCESS=$(curl -s "$API/v1/auth/login" -H 'content-type: application/json' \
  -d '{"email": "quickstart-engineer@example.test", "password": "'"$RAI_NEW_PASSWORD"'"}' \
  | python -c "import json, sys; print(json.load(sys.stdin)['access_token'])")
export AUTH="authorization: Bearer $ACCESS"
```

Where a step needs a different person, the playbook says `$ADMIN_AUTH` or `$REVIEWER_AUTH`: the same, signed in as `admin@example.test` or `quickstart-reviewer@example.test`. An access token lasts 15 minutes; sign in again when a command answers `401`.
