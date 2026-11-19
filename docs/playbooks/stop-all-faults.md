# Stop every fault now

**Goal:** revert every fault the chaos agent has open, at once, whatever state RAI is in.

**Who:** anyone with shell access to the host running the chaos agent. No RAI account, API or database is needed, by design.

**Use it when** a fault is doing more harm than intended, a run looks stuck, someone needs the sandbox back, or you are not sure. Reverting is always safe: it only ever makes the sandbox healthier.

## Steps

1. **See what is open** (optional, but quick):

    ```bash
    curl -s 127.0.0.1:18090/faults
    # {"faults": [{"request": {"type": "connection_drop", "target": "ledger-db-proxy", ...}, ...}]}
    docker compose exec rai-chaos-agent rai-chaos-agent status       # the same, from the state file
    ```

2. **Pull the kill switch:**

    ```bash
    docker compose exec rai-chaos-agent rai-chaos-agent kill --all
    # reverted 1 fault(s)
    ```

    `kill --all` reads the agent's state file directly; it does not need the agent's HTTP server. If the agent is up, `curl -s -X POST 127.0.0.1:18090/kill` does the same from the host.

## Check it worked

```bash
curl -s 127.0.0.1:18090/faults        # {"faults": []}
curl -s 127.0.0.1:18080/readyz        # {"status": "ready"} within a few seconds
```

The payments API can answer `500` for a few seconds after a database fault ends, while its connection pool replaces broken connections; it recovers by itself.

## What happens to the run

The run is not stopped. The worker waits out the fault's declared duration, then records `fault.reverted` with `reverted_by: "kill"` and the time you pulled the switch, and the step finishes. Evidence and observations show exactly when the fault really ended. Read a killed scenario's result with that in mind: its recovery started when you killed it, not when the scenario said.

## If the agent itself is down

The **watchdog** (`rai-chaos-watchdog`, its own container) reverts any fault past its request time plus duration plus a grace (30 s by default), whatever the agent does, every 5 seconds. To act sooner, start the agent again: on start it reverts every fault left open (`reverted_by: "startup"`):

```bash
docker compose --profile chaos up -d rai-chaos-agent
```

Both are drilled on every pull request: CI kills the agent mid-fault and checks the watchdog, and runs `kill --all` with no RAI API or database in the stack ([chaos agent](../chaos-agent.md#getting-faults-out-the-watchdog-and-the-kill-switch-b-25)).
