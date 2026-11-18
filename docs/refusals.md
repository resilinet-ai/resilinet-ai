# Refusal catalogue (B-27: NFR-SAF-1)

Every way RAI refuses to inject a fault, which layer refuses, and the test that proves it. A fault has to pass every layer in order, and each layer checks on its own, so a later layer still holds if an earlier one is bypassed or wrong. The threats are in [threat-model.md](threat-model.md).

`tests/unit/test_refusal_catalogue.py` fails if a test cited here no longer exists, or if a threat ID cited here is not in the threat model. When you add a refusal, add a row.

## 1. API and database: what can be declared

| Refusal | Threat | Proof |
| --- | --- | --- |
| A production environment cannot be registered: the type enum has no PRODUCTION, `production_flag` must be false, and `PROD` is refused in names | E-02 | `tests/integration/test_environments.py::test_production_is_refused_at_the_schema_level` |
| An environment's name and type never change, so a sandbox cannot later become something else | E-02 | `tests/integration/test_environments.py::test_an_environment_never_changes_what_it_is` |
| Only admins add allow-list targets, each with its kind and required label | S-05 | `tests/integration/test_environments.py::test_the_target_allow_list` |
| A scenario fault whose target is outside the scenario's declared blast radius is refused when the scenario is published (422) | D-02 | `tests/integration/test_scenarios.py::test_scenarios_are_versioned_and_checked` |
| Nobody reviews their own scenario (API 403 and a database trigger); engineers cannot review; decisions are append-only | E-03, R-02 | `tests/integration/test_scenarios.py::test_review_is_append_only_and_never_by_the_author` |
| A run with an unapproved scenario (409), or without an environment (422), does not start | T-05 | `tests/integration/test_scenarios.py::test_a_run_starts_only_with_an_approved_scenario_and_an_environment` |

## 2. Worker preflight: checked against the registry before anything is signed

| Refusal | Threat | Proof |
| --- | --- | --- |
| A target that is not allow-listed, a target of the wrong kind, a duration above the environment's ceiling, a target without a required label | S-05, D-02 | `tests/integration/test_chaos_worker.py::test_the_worker_preflight_refuses_before_anything_is_signed` |
| A retired environment takes no faults | D-02 | `tests/integration/test_chaos_worker.py::test_a_retired_environment_takes_no_faults` |
| A scenario whose approval was withdrawn after the run was queued; a worker with no signing key | T-05 | `tests/integration/test_scenarios.py::test_the_worker_refuses_what_it_cannot_run_safely` |
| Recovery probes and the ledger must be allow-listed targets with a `health_url`; a scenario never names an address | S-05 | `tests/integration/test_scenario_recovery.py::test_probes_must_be_allow_listed` |

## 3. Chaos agent: the signature

| Refusal | Threat | Proof |
| --- | --- | --- |
| Unsigned requests, and requests signed by a key the agent does not trust (403) | S-03 | `tests/unit/test_chaos_agent.py::test_only_requests_signed_by_a_trusted_worker_are_applied`, `tests/integration/test_chaos_worker.py::test_the_agent_refuses_a_worker_it_does_not_trust` |
| Tampered, expired, future-dated or malformed envelopes | S-03 | `tests/unit/test_chaos_signing.py::test_the_agent_refuses_what_it_cannot_attribute`, `tests/unit/test_chaos_signing.py::test_the_payload_is_checked_after_the_signature` |
| An agent with no trusted keys or no environment refuses everything | S-03 | `tests/unit/test_chaos_signing.py::test_an_agent_without_keys_or_environment_refuses_everything` |
| A signed request is used once: the same run and step is a replay (409), also after a restart | S-03 | `tests/unit/test_chaos_agent.py::test_a_signed_request_is_used_once`, `tests/unit/test_chaos_signing.py::test_the_replay_guard_survives_a_restart` |

## 4. Chaos agent: its own limits (the second preflight)

| Refusal | Threat | Proof |
| --- | --- | --- |
| A request for another environment, a duration above the agent's own ceiling, more open faults than the agent allows | D-02 | `tests/unit/test_chaos_signing.py::test_the_agent_applies_its_own_limits_whatever_the_request_says` |
| A target outside the blast radius the request declares; malformed requests | D-02 | `tests/unit/test_chaos_agent.py::test_requests_are_checked` |
| A run keeps the radius of its open faults | D-02 | `tests/unit/test_chaos_signing.py::test_a_run_keeps_the_blast_radius_it_declared` |
| **Scenario-level radius (B-27):** every fault of a scenario step must name the same scenario version and the same radius as the faults already recorded for it, open *or reverted* | D-02 | `tests/unit/test_chaos_signing.py::test_a_scenario_keeps_its_radius_after_its_first_fault_is_reverted`, `tests/unit/test_chaos_signing.py::test_each_scenario_step_of_a_run_has_its_own_radius` |
| A scenario fault must name its run and a step key like `item-1.fault-2` | D-02 | `tests/unit/test_chaos_signing.py::test_a_scenario_fault_names_its_run_and_step` |

## 5. Chaos agent: the sandbox

| Refusal | Threat | Proof |
| --- | --- | --- |
| A container without `rai.sandbox=true` or the registered label, a name matching more than one container, a missing proxy, a network the container is not on | E-05, S-05 | `tests/unit/test_chaos_agent.py::test_targets_are_checked_against_the_sandbox` |
| Containers outside the agent's own Compose project; container faults when it cannot tell its project | E-01 | `tests/unit/test_chaos_agent.py::test_container_faults_stay_inside_the_agents_project` |
| One fault per target at a time | D-02 | `tests/unit/test_chaos_agent.py::test_one_fault_per_target` |
| Only the Docker calls the agent needs, through a socket proxy that allows only the agent and the watchdog | E-01 | `tests/unit/test_chaos_agent.py::test_the_docker_client_sends_only_the_allowed_calls`, `scripts/chaos_e2e.py::socket_proxy` |
| Refusals against the real stack: unsigned and wrongly signed requests, unlabelled targets | S-03, E-05 | `scripts/chaos_e2e.py::refused`, `scripts/chaos_e2e.py::signatures` |

## 6. Containment: when a fault outlives its welcome

These are not refusals, but they bound what a fault that got through can do.

| Control | Threat | Proof |
| --- | --- | --- |
| The agent reverts faults past their duration | D-01 | `tests/unit/test_chaos_agent.py::test_expired_faults_are_reverted` |
| An independent watchdog reverts overdue faults when the agent is dead or hung | D-01 | `tests/unit/test_chaos_watchdog.py::test_the_watchdog_reverts_only_overdue_faults`, `scripts/chaos_e2e.py::watchdog_after_agent_crash` |
| `kill --all` reverts every open fault from the local state file | D-01 | `scripts/chaos_e2e.py::kill_switch_drill` |
