# Maintenance, Repair and Overhaul (MRO) Organizational Experiment v1 — Official Synthetic Results

**Status:** Official A0 synthetic execution; audited scientific outcome report. **Not** empirical maintenance data.

- **Official execution:** [GitHub Actions run 37916705565](https://github.com/brunolnetto/sose/actions/runs/37916705565), source commit `3c356fea6adba2d27f583d3a1469e5df1c0b651b`.
- **Permanent archive:** [MRO v1 GitHub Release](https://github.com/brunolnetto/sose/releases/tag/mro-v1-evidence-37916705565-attempt-1), contains `mro-v1-evidence.tar.gz` and SHA-256 sidecar.
- **Actions bundle:** `official-mro-v1-evidence` (artifact 11609744496).
- **Frozen protocol SHA-256:** `a5fe4199bbadc284df51977b0ad6120dfb3ce24dbb8442ca549c7687b5175d63`.
- **Frozen plan SHA-256:** `17cc9ab49ad817122781a0416c2fec49e301d70e23869dddab8f63f98577d59b`.
- **Official result hash:** `7e23c5f230034c3fb4cf79786110332e9b8c6da299a18bd3245a56fc13228740`.
- **Freeze manifest Git blob:** `a6ff93510f8ff460ccf7430d89baa6c4d52cdc9b`.
- **Complete runtime Git tree:** `eeae0590f0ab84230a2cabfecd5225111d777592`; **uv.lock Git blob:** `5902ec2400ba5998f154831d080b1e5773da7413`.

## Preregistered design

Six Latin Hypercube `quantity` configurations in [1,20] parts, three arms (`baseline`, `spare_part_shortage`, `emergency_preemption`), A0 fixed policy, two deterministic replications, CRN paired design, zero warmup, horizon 24.

**Recorded:** 18 worlds, 36 runs, 36 typed raw evidence records, 252 eligible ground-truth assessments (all 252 passed), 108 paired intervention comparisons, **24 eligible and 84 ineligible**.

## Observed synthetic outcomes

| Arm | Mean lead time (s) | Material wait | Interruptions | Preemptions | Parts issued exactly once | Closing |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| Baseline | 3,600 | 0 | 0 | 0 | Yes | Closed |
| Spare-part shortage | 7,200 | 1 | 0 | 0 | Yes | Closed |
| Emergency preemption | 3,600 | 0 | 1 | 1 | Yes | Closed |

Across all six design points and arms, recorded parts consumed equaled the configured quantity, remaining parts equaled zero, and the issue count was exactly one.

| Eligible preregistered paired contrast | Mean treatment − baseline | Direction across six DOE points |
| --- | ---: | --- |
| Spare-part shortage: lead time | +3,600 s (1 hour) | 6/6 positive |
| Spare-part shortage: material wait | +1 | 6/6 positive |
| Emergency preemption: interruption count | +1 | 6/6 positive |
| Emergency preemption: preemption count | +1 | 6/6 positive |

**Null finding:** Emergency preemption records the expected interruption and preemption but does not increase lead time in this configuration. It is not evidence that preemption lacks consequences in other simulated configurations or real maintenance operations.

## Independent audit and limits

All seven canonical scientific files match the SHA-256 values in `evidence-manifest.json`. All 14 files listed in the final provenance manifest match their own hashes, with valid JSON and matching inventory in the downloaded Actions bundle. Permanent release assets exist; the local independent audit did not re-download the release tarball itself.

All 24 eligible effects are paired with **zero paired standard error**, reflecting the deterministic simulator and fixed replications. No statistical inference about real-world maintenance reliability, organizational adaptation, stochastic variance or industry-wide effects is supported. Of the 108 recorded effects, 84 are explicitly ineligible and must not be recruited post hoc for scientific claims.

**Promotion conclusion:** MRO satisfies its v1 A0 preregistered synthetic-mechanism, inventory conservation and typed-ground-truth checks. This supports architectural generality across resource-heavy workflows, not empirical calibration.
