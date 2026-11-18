# Resilinet-AI (RAI): MVP Wireframes

| | |
| --- | --- |
| **Version** | v0.2 (reconstructed draft), 2026-11-17 |
| **Status** | Draft. **Pending review and approval by the author of record**, Trevor Kauyu (ADR-007) |
| **Supersedes** | Nothing. The v0.1 original (2026-11-13) has not been found; if it is, it takes precedence over this draft |

> **How this document was made.** The v0.1 wireframes were never exported, and the UI (`rai-web`, React and TypeScript, ADR-004) has not been built yet. These wireframes were drafted on 2026-11-17 by an AI assistant (Claude Code) from the requirements (FR-11, FR-12, FR-13, NFR-USE-1, NFR-USE-2) and the data the API already returns. They are **low-fidelity layouts, not the v0.1 designs**. Field names match the API, so every number shown has a source.

## 1. Rules for every screen

- **Persistent synthetic-data banner (FR-11.2)** on every page, which cannot be dismissed: *SYNTHETIC TEST DATA — NOT A PRODUCTION RESULT*. For an `authorized_snapshot` project it reads *AUTHORISED SNAPSHOT — SANDBOX ONLY*.
- **Every metric shows its definition, n and window (NFR-USE-1).** A number never appears without its denominator and the period it covers; hovering or focusing it shows the definition.
- **Themes (D10):** Midnight (dark, default), Clarity, Evergreen and High contrast. All four meet **WCAG 2.2 AA** (NFR-USE-2): contrast, keyboard operation, visible focus, no information carried by colour alone (every status also has a word and an icon).
- **Read-only by role.** Controls a role cannot use are hidden; a control that is unavailable for a reason (such as reviewing your own scenario) is shown disabled, with the reason.

## 2. Screens

| ID | Screen | Requirements |
| --- | --- | --- |
| W1 | Sign-in and tenant choice | FR-01.1 |
| W2 | Project overview | FR-03, FR-06 |
| W3 | Run detail: steps, observations, lineage | FR-06, FR-04.1, NFR-REP-1 |
| W4 | Chaos scenario: definition and review | FR-07.2, FR-12.1 |
| W5 | Recovery results | FR-08.1–08.3 |
| W6 | Executive dashboard | FR-11.1, NFR-USE-1 |
| W7 | Findings and waivers *(planned API)* | FR-10.1, FR-12.2 |
| W8 | Evidence export *(planned API)* | FR-13.1, FR-13.2 |

### W2. Project overview

```
+--------------------------------------------------------------------------------+
| ! SYNTHETIC TEST DATA - NOT A PRODUCTION RESULT                                 |
+--------------------------------------------------------------------------------+
| RAI  | Projects > sandboxbank-payments            [SANDBOX-01 v]   A. User (v)  |
+------+-------------------------------------------------------------------------+
| Over | Assets (4)                  Contracts           Last run                 |
| view | - transactions_raw  table   transactions_raw@1  passed  2 min ago        |
| Asse | - ledger_transactions table ledger@2            failed  1 h ago          |
| Plan | - fact_transaction  table   -                   -                        |
| Scen |                                                                         |
| Runs | Recent runs                                     [ Start run ]            |
| Keys | #  plan            trigger   state    verdict  started      duration     |
|      | 41 ci-contracts@3  ci a1b2c  passed   pass     12:01 UTC    18 s         |
|      | 40 outage-check@1  manual    failed   fail     11:02 UTC    4 min 10 s   |
+------+-------------------------------------------------------------------------+
```

### W3. Run detail

```
+--------------------------------------------------------------------------------+
| ! SYNTHETIC TEST DATA - NOT A PRODUCTION RESULT                                 |
+--------------------------------------------------------------------------------+
| Run 40  outage-check@1    FAILED (fail)     queued 11:02 > running > evaluating |
| Pinned: plan outage-check@1 (sha 9f3c..)  scenario ledger-outage@2, approval    |
|         by R. Reviewer 10:58  environment SANDBOX-01         [ View pins JSON ] |
+--------------------------------------------------------------------------------+
| Steps                                                                           |
|  item-1  scenario ledger-outage@2   done   fail  RTO 142 s exceeds 120 s        |
|  item-2  ledger.consistency@1       done   pass  0 missing / 0 duplicated       |
+--------------------------------------------------------------------------------+
| Observations (8)   [ All | Fail | Error ]                                       |
|  item-1.fault-1  fault.onset      metric  pass   onset 11:02:14Z                |
|  item-1          recovery.rto     metric  FAIL   142.0 s  (objective 120 s)     |
|  item-1          recovery.rpo     metric  pass   0 s, 0 rows lost  (n = 1)      |
+--------------------------------------------------------------------------------+
| Lineage   ledger_transactions --sha256 4be1..(190,002 rows)--> fact_transaction  |
+--------------------------------------------------------------------------------+
```

### W4. Chaos scenario: definition and review

```
+--------------------------------------------------------------------------------+
| ledger-outage  v2   status: PENDING REVIEW        author: A. Engineer           |
+--------------------------------------------------------------------------------+
| Blast radius: [ledger-db-proxy] [payments-api]   (all on SANDBOX-01 allow-list) |
| Faults, in order:                                                               |
|  1  connection_drop   ledger-db-proxy   60 s                                    |
|  2  container_stop    payments-api      30 s                                    |
| Recovery: probes payments-health; ledger payments-ledger;                      |
|           RTO <= 120 s, RPO <= 0 s; window 300 s; 3 stable checks              |
+--------------------------------------------------------------------------------+
| Review history (latest counts)                                                  |
|  10:58  R. Reviewer   approve   "radius limited to P1"                          |
|                                                                                 |
| [ Approve ] [ Reject ]   <- disabled for the author:                            |
|                             "You wrote this scenario; another reviewer decides" |
+--------------------------------------------------------------------------------+
```

### W5. Recovery results

```
+--------------------------------------------------------------------------------+
| Recovery - ledger-outage, last 30 days (window shown)                           |
+--------------------------------------------------------------------------------+
| RTO objective met     4 of 5 runs        definition: onset -> validated restore |
| RPO objective met     5 of 5 runs        definition: onset - earliest lost ack  |
| Consistency passed    5 of 5 runs        missing/duplicated/unexpected/mismatch |
+--------------------------------------------------------------------------------+
| Run   RTO      objective  RPO (ledger)  RPO (seeding log)  agree   consistent    |
| 40    142 s    120 s  X   0 s           0 s                yes     yes           |
| 37    88 s     120 s  ok  0 s           0 s                yes     yes           |
+--------------------------------------------------------------------------------+
```

### W6. Executive dashboard

```
+--------------------------------------------------------------------------------+
| ! SYNTHETIC TEST DATA - NOT A PRODUCTION RESULT                                 |
+--------------------------------------------------------------------------------+
| Period: 2026-11-01 to 2026-11-30 (UTC)        Environment: SANDBOX-01           |
+-------------------------+-------------------------+----------------------------+
| Batches passing         | Recovery objectives met | Open critical findings     |
| 412 of 430 (95.8%)      | 9 of 11 runs            | 2 (1 waived to 2026-12-15) |
| n = 430 batches, 3 feeds| n = 11 scenario runs    | n = 37 findings            |
+-------------------------+-------------------------+----------------------------+
| Gate decisions: 28 PRs checked, 3 blocked, 0 overridden                         |
| Each tile: [i] definition, numerator / denominator, window, data source         |
+--------------------------------------------------------------------------------+
```

W7 and W8 follow the same layout. They wait on the findings, waiver and evidence APIs (B-33 to B-35) and are not drawn here.

## Not recoverable from the repository

- The v0.1 wireframes themselves, and any screens they had beyond W1–W8.
