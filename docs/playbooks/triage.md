# Triage a failing run

**Goal:** turn a failing run into a tracked finding, then either fix it and prove the fix, or accept the risk for a limited time with a second person's name on it (FR-10.1, FR-12.1, FR-12.2).

**Who:** an engineer opens the finding and asks for a waiver; a **reviewer or admin who is neither the requester nor the finding's owner** approves it.

**You need:** a failed run, for example the quick start's `resent` run.

## Steps

1. **Find what failed.** In the UI, Runs shows the failing rules and how many rows broke each. Or:

    ```bash
    curl -s "$API/v1/assurance/runs/<run id>/observations?outcome=fail" -H "$AUTH"
    # [{"step_key": "item-2", "rule": "unique_across_batches:txn_id", "failing_count": 2, ...}]
    ```

2. **Open a finding** on that observation, with a severity. Only `critical` findings block the release gate.

    ```bash
    curl -s -X POST "$API/v1/projects/<project id>/findings" -H "$AUTH" -H 'content-type: application/json' \
      -d '{"run_id": "<run id>", "step_key": "item-2", "rule": "unique_across_batches:txn_id",
           "title": "Duplicate txn_id in the feed", "severity": "critical"}'
    ```

    The owner defaults to you; set `owner_id` to give it to someone else. From now on `rai run` and the CI gate exit `1` for this project, even for runs that pass, until the finding is closed or waived.

3. **Then one of three:**

    - **Fix it.** Correct the data or the pipeline, run the plan again, and retest the finding with the new run. Only a run started after the finding, which passes the same rule for the same test, closes it:

        ```bash
        curl -s -X POST "$API/v1/findings/<finding id>/retest" -H "$AUTH" -H 'content-type: application/json' \
          -d '{"run_id": "<new run id>"}'
        # 200 {"status": "closed", ...}    or 409 findings.retest_not_passing
        ```

    - **Ship knowingly with a waiver.** Ask for one with a reason and an expiry at most 90 days ahead:

        ```bash
        curl -s -X POST "$API/v1/findings/<finding id>/waivers" -H "$AUTH" -H 'content-type: application/json' \
          -d '{"reason": "fix ships next week; duplicates are filtered downstream",
               "expires_at": "2026-12-01T00:00:00Z"}'
        ```

        A reviewer then decides, signed in as themselves (`$REVIEWER_AUTH`):

        ```bash
        curl -s -X POST "$API/v1/waivers/<waiver id>/decisions" -H "$REVIEWER_AUTH" \
          -H 'content-type: application/json' -d '{"decision": "approve"}'
        # 201 {"status": "approved", "in_force": true, ...}
        ```

    - **It is not a defect.** A reviewer who did not open it dismisses it with a reason (`POST /v1/findings/<id>/dismiss`).

## Check it worked

```bash
curl -s "$API/v1/projects/<project id>/gate" -H "$AUTH"
# {"verdict": "pass", "blocking_count": 0, "waived_count": 1, ...}
```

`rai run` then exits `0` for a passing run and lists the finding as `WAIVED ... until <expiry>`. The Dashboard's "Open critical findings" card shows the finding and "Release gate pass · 1 waived".

## If it goes wrong

| Answer | Meaning |
| --- | --- |
| `422 findings.source_not_failing` | The observation passed; there is nothing to raise. |
| `409 findings.duplicate` | That observation already has a finding; the `Location` header points to it. |
| `409 findings.retest_too_early` | The retest run was queued before the finding was opened. Start a new run. |
| `403` on a waiver decision | You asked for the waiver, you own the finding, or your role cannot decide. Ask a reviewer who is neither. |
| `409 waivers.exists` | A waiver is already pending or in force for this finding. |
| `422 waivers.expiry_out_of_range` | The expiry is in the past or more than 90 days ahead, or has no time zone. |

**When a waiver expires**, the gate blocks again by itself. Nobody has to remember. Findings, waivers and decisions can never be edited or deleted; each is an audit entry. More: [findings](../api.md#findings), [waivers and the release gate](../api.md#waivers-and-the-release-gate).
