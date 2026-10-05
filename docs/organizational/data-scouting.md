# Organizational Dynamics — Phase 5 Data Scouting Memo

Status: Phase 5 deliverable

Purpose: identify which organizational mechanisms can be observed directly, inferred defensibly, or must remain assumed/swept before the first empirical canonical is selected.

This memo constrains model scope. Data availability is allowed to remove mechanisms from v0.1; the model must not pretend that an unobserved mechanism has been calibrated.

## 1. Evidence classes

SOSE uses the `ModelSpec` evidence classes:

- **Observed** — directly estimated from source data using an explicit extraction rule.
- **Inferable** — not recorded directly, but estimable from observable consequences under a declared model.
- **Assumed** — not identifiable from the available data; preregistered and swept over a plausible range.

Evidence class is parameter-specific, not source-specific. The same source may provide Observed values for one mechanism and no evidence for another.

## 2. Candidate data sources

### 2.1 GitHub public repository event data — primary v0.1 candidate

GitHub exposes timestamped pull-request and issue activity, reviews, assignments, labels, comments and workflow execution. Timeline events can identify review submissions and state changes; pull-request reviews expose submission time and review state; workflow-run/job APIs expose CI timing.

Strengths:

- public and reproducible for open-source repositories;
- item identity and timestamps are durable;
- PR and issue histories naturally support item-ledger calibration;
- repository history makes some policy/structural changes auditable;
- CI execution provides machine-service observations distinct from human review latency.

Limitations:

- no reliable record of private meetings, chat, attention shifts or cognitive reacquisition;
- requested review is not the same as when a reviewer actually noticed work;
- comments are an incomplete proxy for communication;
- branch-protection history and informal authority are generally not reconstructible from ordinary public event history;
- contributor availability calendars are not directly observed;
- public projects are self-selected and may not represent firms.

Decision: **GitHub is the preferred first scouting source for item-flow validation, not actor-ledger calibration.**

### 2.2 Jira Cloud changelog — strong private/controlled candidate

Jira issue changelogs expose timestamped field changes, including status transitions, and can reconstruct item-state trajectories when project access exists.

Strengths:

- explicit workflow states;
- status-transition history;
- assignee changes;
- issue metadata and timestamps;
- natural fit for item-ledger reconstruction.

Limitations:

- public datasets are uncommon;
- workflow semantics differ between organizations;
- time in a status does not uniquely identify root cause;
- actor effort, meetings and interruptions remain mostly invisible.

Decision: **Jira is preferred when a cooperating organization supplies historical data, but it is not required for v0.1.**

### 2.3 CI/CD execution logs

GitHub Actions and comparable CI systems expose machine job start/end times, conclusions and dependency structure.

Useful for:

- automation-service times;
- queueing before machine execution;
- retry/rework caused by failed builds;
- intervention studies where automation is introduced or changed.

These logs do not directly measure human coordination.

### 2.4 Calendar / communication telemetry

Calendar, Slack/Teams and similar telemetry could improve actor-ledger calibration, but this source class is intentionally excluded from the default public-data path because of privacy, ethics and interpretation risk.

If used, experiments must define:

- consent and access basis;
- minimization/aggregation rules;
- whether message count is treated only as activity evidence rather than semantic content;
- the impossibility of observing unrecorded coordination.

No v0.1 result may require this source class.

## 3. Observability matrix

| Mechanism / parameter | GitHub | Jira | CI logs | Default class for public v0.1 | Notes |
|---|---|---|---|---|---|
| item creation time | direct | direct | n/a | Observed | timestamp |
| item completion/merge time | direct | direct | direct for jobs | Observed | definition must be preregistered |
| item lead time | derived from timestamps | derived | derived | Observed | deterministic extraction |
| review submission time | direct | project-dependent | n/a | Observed | GitHub review `submitted_at` |
| review decision state | direct | workflow-dependent | n/a | Observed | approved / changes requested / commented |
| issue/PR assignment changes | event/history | changelog | n/a | Observed | authority is not implied by assignment |
| workflow/status transitions | partial timeline | direct | direct job state | Observed | source-specific semantics |
| CI service duration | direct | linked only | direct | Observed | machine capacity only |
| CI failure/retry | direct | linked only | direct | Observed | useful quality/rework proxy |
| queue before human review | timestamps only | timestamps only | n/a | Inferable | must define event boundaries |
| reviewer service time | no | no | n/a | Assumed / Inferable | submission latency is not service time |
| human utilization | no | no | no | Assumed | cannot be recovered from item logs |
| context-switch count | no | no | no | Assumed | event activity is not attention state |
| reacquisition cost | no | no | no | Assumed | sweep; no folklore constant |
| meeting load | no | no | no | Assumed | unless independent calendar data exists |
| actor calendar availability | weak proxy | weak proxy | machine calendar only | Assumed | weekends are not sufficient evidence |
| capability/proficiency | weak proxy | weak proxy | n/a | Inferable / Assumed | prior contribution history is only a proxy |
| defect/rework occurrence | reopen/revision proxy | reopen/status proxy | failed jobs direct | Inferable | domain definition required |
| quality escape | usually no | usually no | partial | Assumed / Inferable | downstream defect linkage required |
| authority graph | CODEOWNERS / explicit config only | workflow config if available | n/a | Inferable | assignment != authority |
| communication graph | comments/reviews only | comments only | n/a | Inferable | incomplete graph |
| task dependency graph | linked issues / explicit references | issue links | job DAG direct | Inferable / Observed | depends on source discipline |
| interruption policy | no | no | no | Assumed | A1/A2 policy, not measured by default |
| managerial pressure | no | no | no | Assumed | cannot be inferred from deadline misses alone |
| operating cost | no | possibly business data | infra usage partial | Assumed | separate economic model |
| transition cost/time | repository history may reveal time | project history may reveal time | partial | Inferable / Assumed | depends on intervention |

## 4. What the public-data v0.1 can credibly calibrate

A GitHub-first canonical may directly calibrate or validate:

- arrival process of PRs/issues;
- item lead-time distribution;
- time from opening to first review event;
- time from review request/event to submitted review where available;
- number and timing of review cycles;
- merge/close outcomes;
- reopen/revision-like loops when explicitly observable;
- CI job duration and failure/retry distributions;
- WIP measured as simultaneously open items under a declared item definition;
- throughput over a finite or stationary observation window;
- path-specific behavior where repository configuration (for example CODEOWNERS) is versioned in Git.

The public-data v0.1 **cannot** claim direct calibration of:

- cognitive interruption cost;
- human service effort;
- utilization;
- meeting burden;
- tacit communication;
- motivation or pressure;
- trust;
- A3 behavior.

Those mechanisms remain Assumed unless an independent source is supplied.

## 5. Item ledger versus actor ledger

Data availability confirms the specification's two-ledger distinction.

### Item ledger

Public engineering logs are suitable for external validation of item-level observables such as elapsed time, transitions, review waiting and CI waiting/execution.

### Actor ledger

Public engineering logs are not sufficient to reconstruct conservation of actor-time. Activity timestamps show that an action occurred, not what the actor was doing during the interval before it.

Therefore:

> The actor ledger is a simulation accounting construct in v0.1, not an empirically reconstructed timesheet.

Actor-ledger parameters must be reported as Observed only when an independent source actually measures them.

## 6. Candidate natural experiments

The following are candidate *experiment families*. A concrete repository/event date must be selected and preregistered before analysis.

### 6.1 CODEOWNERS introduction or path reassignment

Observable intervention:

- repository commit adds/removes/changes CODEOWNERS entries for a subset of paths.

Potential treatment:

- PRs touching affected paths.

Potential controls:

- PRs in unaffected paths over the same time window.

Outcomes:

- time to first review;
- review-cycle count;
- merge lead time;
- review participation concentration.

Why useful:

- the structural diff is auditable in Git;
- partial path coverage permits difference-in-differences rather than a simple before/after comparison.

Threats:

- concurrent staffing/process changes;
- path composition changes;
- contributor learning;
- CODEOWNERS may formalize an already-existing informal authority structure.

### 6.2 CI automation introduction or major workflow replacement

Observable intervention:

- workflow file introduced or materially changed in repository history.

Outcomes:

- time to feedback;
- failed/retried workflow frequency;
- PR merge lead time;
- review cycles after CI failure.

Threats:

- workflow changes often accompany broader engineering changes;
- faster machine feedback may shift human behavior.

### 6.3 Review-policy or contribution-process change documented in repository history

Observable intervention:

- CONTRIBUTING/review-policy/config change with a clearly dated commit.

Examples of candidate treatment mechanisms:

- mandatory review stage;
- changed ownership responsibility;
- bot-mediated triage;
- changed merge policy.

This family is acceptable only when the operational change is independently evidenced. Documentation text alone is not sufficient proof that behavior changed.

### 6.4 WIP-limit intervention in a cooperating team

Preferred private-data natural experiment:

- a team adopts an explicit WIP limit at a known date.

Outcomes:

- cycle-time distribution;
- WIP;
- throughput;
- queue age;
- blocked-time proxies.

This is a stronger causal candidate than public-repository before/after comparisons if implementation fidelity can be verified.

## 7. Recommended first empirical target

The first empirical exercise should **not** attempt to validate the full organizational thesis.

Recommended target:

> Fit and validate an item-flow model for pull-request review using a public GitHub repository, with human effort parameters left Assumed, and test whether the model predicts held-out lead-time/WIP distributions.

Why this target:

1. item timestamps are observable;
2. review events are explicit;
3. CI activity can be separated from human review activity;
4. no private telemetry is required;
5. failure is informative — inability to predict held-out flow invalidates moving to richer organizational claims.

A structural/natural-experiment study should follow only after this L5 calibration succeeds.

## 8. Candidate ModelSpec evidence mapping for the first empirical target

Example parameter classification:

| ModelSpec parameter | Initial evidence class |
|---|---|
| PR arrival distribution | Observed |
| PR size proxy distribution | Observed (files/lines changed if used) |
| CI duration distribution | Observed |
| CI failure probability | Observed |
| time-to-first-review distribution | Observed as an item-level delay distribution |
| review-cycle revisit probability | Observed / Inferable depending on definition |
| reviewer service effort | Assumed |
| actor availability calendar | Assumed |
| context-reacquisition cost | Assumed |
| interruption rate | Assumed |
| review authority for CODEOWNERS paths | Inferable when configuration exists |
| latent defect propensity | Assumed unless downstream defect linkage exists |
| quality escape cost | Assumed |
| intervention transition cost | Assumed / Inferable per intervention |

Important distinction: using the observed time-to-first-review distribution as a delay station is acceptable for a baseline flow model, but it does **not** identify reviewer service time or human utilization.

## 9. Data extraction contract for a GitHub pilot

A pilot extractor should produce a normalized event table with at least:

- repository;
- item type and item id;
- item creation timestamp;
- close/merge timestamp;
- author identity pseudonym/key;
- assignee changes;
- requested-reviewer events where available;
- submitted review timestamp and state;
- commits pushed to the PR;
- CI workflow/job start, completion and conclusion;
- labels/status-like events used by the selected repository;
- touched paths or path groups;
- repository configuration version relevant to the experiment.

Extraction must preserve raw event identity and timestamps so the normalized item ledger can be regenerated deterministically.

No extractor may manufacture actor-time intervals from gaps between events.

## 10. Data quality gates

A candidate dataset is usable only if:

- event timestamps are sufficiently complete for the selected item definition;
- intervention/configuration timing is auditable when causal analysis is attempted;
- the pre/post windows contain enough items for the preregistered statistical design;
- bot events can be identified or treated explicitly;
- deleted/private events are acknowledged as missingness;
- item-selection rules can be reproduced;
- repository activity does not collapse to a single contributor unless that is the target model.

The scouting phase must publish rejection reasons for candidate datasets rather than silently selecting the dataset that produces the desired result.

## 11. Identifiability rules

1. A timestamp gap is not actor service time.
2. Assignment is not decision authority unless the workflow/configuration states it.
3. Comment count is not coordination cost.
4. Review request is not attention start.
5. Merge latency is not review latency unless intermediate intervals are separated.
6. CI waiting is machine queueing only when runner/job semantics support that interpretation.
7. A parameter remains Assumed if two materially different mechanisms can produce the same observed event history and the source cannot distinguish them.

## 12. Phase 5 conclusion

The data scout does not block the Organizational Dynamics program, but it narrows the first empirical scope.

The strongest immediately available external evidence is **item-flow evidence**, especially from public GitHub histories. Actor-capacity mechanisms central to the broader theory remain mostly unidentifiable without independent organizational telemetry.

Therefore the first canonical should separate:

- **calibrated item-flow mechanisms**, which may be confronted with held-out data;
- **assumed actor mechanisms**, which must be swept and reported as sensitivity dimensions.

Recommended next step: Phase 6 should preregister a GitHub pull-request flow pilot and define its DOE/statistical protocol before choosing a repository/date window.