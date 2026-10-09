# Order-to-Cash (O2C) Organizational Experiment v1 — Official Synthetic Results

**Status:** Official A0 synthetic execution; audited scientific outcome report. **Not** observed-factory/customer data or empirical validation.

- **Official execution:** [GitHub Actions run 37916705565](https://github.com/brunolnetto/sose/actions/runs/37916705565), source commit `3c356fea6adba2d27f583d3a1469e5df1c0b651b`.
- **Permanent archive:** [O2C v1 GitHub Release](https://github.com/brunolnetto/sose/releases/tag/o2c-v1-evidence-37916705565-attempt-1), contains `o2c-v1-evidence.tar.gz` and SHA-256 sidecar.
- **Actions bundle:** `official-o2c-v1-evidence` (artifact 11609239720).
- **Frozen protocol SHA-256:** `6480fe461e4e02981e6c6f3fc4144029901991231178852e4e07b9754a7a9c78`.
- **Frozen plan SHA-256:** `25303b47c6a2a0d6643ccedf4244411e44ec63a159a4b176c2a961d45032d39a`.
- **Official result hash:** `c51013cb109f67f5e39bb87385f0b666632c2db953d3084d7dd6ce95adc6500c`.
- **Freeze manifest Git blob:** `5c60565eb576afee0ff8bb16cfe938b8052e1bd5`.
- **Complete runtime Git tree:** `eeae0590f0ab84230a2cabfecd5225111d777592`; **uv.lock Git blob:** `5902ec2400ba5998f154831d080b1e5773da7413`.

## Preregistered design

Four Latin Hypercube `due_delay_hours` configurations in [1,5], quantized to four distinct one-hour logical due-time strata, three arms (`baseline`, `partial_fulfillment`, `overdue_collection`), A0 fixed policy, two deterministic replications, common random number pairing, zero warmup, horizon 24.

**Recorded:** 12 worlds, 24 runs, 24 typed raw evidence records, 144 eligible ground-truth assessments (all 144 passed), 56 paired intervention comparisons, **12 eligible and 44 ineligible**.

## Observed synthetic outcomes

| Arm | Mean collection time (s) | Partial fulfillment count | Overdue count | Collection case count | Escalation count | Collected |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Baseline | 12,600 | 0 | 0 | 0 | 0 | Yes |
| Partial fulfillment | 12,600 | 1 | 0 | 0 | 0 | Yes |
| Overdue collection | 23,400 | 0 | 1 | 1 | 1 | Yes |

| Eligible preregistered paired contrast | Mean treatment − baseline | Direction across four DOE points |
| --- | ---: | --- |
| Partial fulfillment: partial count | +1 | 4/4 positive |
| Overdue collection: overdue count | +1 | 4/4 positive |
| Overdue collection: collection time | +10,800 s (3 hours) | 4/4 positive |

The fixed value of the order is 250 monetary units. The experiment checks administrative workflow semantics and scheduled due dates, not the causal effect of changing the order's monetary amount. Partial fulfillment did **not** change collection lead time in this executable reference; no such effect should be claimed.

## Independent audit and limits

The downloaded Actions bundle has all seven scientific files matching `evidence-manifest.json` SHA-256 checks, and all 14 files listed in `final-provenance-manifest.json` match their hashes; strict JSON decoding and complete file-inventory reconciliation passed. The GitHub Release assets exist; the independent local digest audit described here applies to the Actions bundle, not a separately downloaded Release tarball.

All 12 eligible comparisons are paired and have paired standard error **zero**. Zero-width confidence bounds are a property of repeatable deterministic mechanics, not evidence of population precision. Four controlled synthetic timing strata are not independent real organizations.

The favorable/unfavorable label of adverse interventions describes business performance; demonstrating an adverse mechanism is a successful *mechanism verification*, not a performance improvement. The 44 ineligible effects remain in the audit bundle and cannot support scientific claims. No A1/A2 adaptation, empirical transportability, production accuracy or real-world outcome inference is established.

**Promotion conclusion:** O2C satisfies its v1 A0 preregistered synthetic-mechanism and typed-truth checks. The experiment supports cross-domain architectural feasibility, not general empirical validity.
