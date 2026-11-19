# Gate a pipeline in CI

**Goal:** a pull request cannot merge while its data fails a critical contract rule, or while the project has an open critical finding without a waiver (FR-06.1, FR-12.2).

**Who:** an engineer of the project (admins can too).

**You need:** a project with a plan whose test cases check the data the pull request changes (the quick start's `clean` and `resent` plans will do), and a RAI API that your CI runners can reach over HTTPS.

## Steps

1. **Create a project API key.** CI signs in with a key, never a person's account. It can start and read runs of this project only, and it must expire (1 to 90 days):

    ```bash
    curl -s -X POST "$API/v1/api-keys" -H "$AUTH" -H 'content-type: application/json' \
      -d '{"project_id": "<project id>", "name": "github-actions", "expires_in_days": 30}'
    # 201 {"key": "rai_...", ...}     the key is shown in this response only
    ```

2. **Store it as a CI secret**, for example `RAI_API_KEY` in the repository's Actions secrets. Never put it on a command line or in a file.

3. **Try it from a terminal first:**

    ```bash
    export RAI_API_URL=$API RAI_API_KEY='rai_...'
    uv run rai run --plan resent; echo "exit code $?"
    # FAILED run ...: item-1 pass, item-2 fail
    #   FAIL  item-2 unique_across_batches:txn_id (critical) ...
    # exit code 1
    ```

    `0`: the run and the release gate passed. `1`: the run failed or the gate is blocked. `2`: no verdict (refused, error, timeout, unknown plan, bad key, API unreachable). A gate that cannot be read is `2`, never a pass.

4. **Add the action to the workflow**, pinned by commit SHA:

    ```yaml
    - uses: resilinet-ai/resilinet-ai@<commit SHA>
      with:
        api-url: https://rai.example.test
        api-key: ${{ secrets.RAI_API_KEY }}
        plan: ci-contracts
    ```

5. **Make the job a required status check** for the protected branch (repository **Settings > Branches**). Until it is required, a failing gate is only a red mark, not a block.

## Check it worked

Open a pull request whose data fails the plan: the check fails, its job summary names the failing rule, and the merge button is disabled. Fix the data and the check turns green. The run is on the Dashboard and in Runs, with the commit SHA as its trigger.

## If it goes wrong

| Symptom | Cause and fix |
| --- | --- |
| Exit `2`, "unknown plan" | The plan name is not in the key's project. A key only sees its own project. |
| Exit `2`, `401` | The key expired, was revoked, or its creator lost access. Create a new key. |
| Exit `2`, refused | The worker could not run the plan as pinned (environment not registered, connector missing on the worker). The run's `reason` lists every problem. |
| Exit `1` with "GATE blocked" although the run passed | An open critical finding has no waiver in force: see [triage](triage.md). Passing data does not hide a known defect. |
| `429` | More than 120 requests a minute with one key. The answer says when to retry. |

Plain `http://` is refused except to localhost, so a key never crosses a network in clear. More: [API keys and the CI gate](../api.md#api-keys-and-the-ci-gate).
