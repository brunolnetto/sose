# SOSE Organizational Experiments — Three-domain Scientific Comparison (v1)

**Status:** Audited cross-domain comparison of three independently frozen, deterministic A0 synthetic experiments. **Not** a claim of empirically calibrated organizational dynamics, general causal transferability, or validated human agency.

## Provenance and outcomes

| Domain | Frozen result hash | Worlds / runs | Typed truth checks | Eligible / all comparisons | Official release |
| --- | --- | ---: | ---: | ---: | --- |
| Manufacturing | `f79bd89fc50ca5fadc0788c657cc752d1291dd464fbf1643e660a67667c4338b` | 18 / 36 | 216/216 passed | 24 / 84 | [Manufacturing evidence](https://github.com/brunolnetto/sose/releases/tag/manufacturing-v1-evidence-37869182046) |
| O2C | `c51013cb109f67f5e39bb87385f0b666632c2db953d3084d7dd6ce95adc6500c` | 12 / 24 | 144/144 passed | 12 / 56 | [O2C evidence](https://github.com/brunolnetto/sose/releases/tag/o2c-v1-evidence-37916705565-attempt-1) |
| MRO | `7e23c5f230034c3fb4cf79786110332e9b8c6da299a18bd3245a56fc13228740` | 18 / 36 | 252/252 passed | 24 / 108 | [MRO evidence](https://github.com/brunolnetto/sose/releases/tag/mro-v1-evidence-37916705565-attempt-1) |
| **Total** | Three domain-specific freezes | **48 / 96** | **612/612 passed** | **60 / 248** | Separate permanent, hash-addressed archives |

**Independent audit scope:** All three downloaded GitHub Actions evidence bundles have (i) seven canonical files matching the SHA-256 evidence manifest, (ii) all files in the final provenance manifest matching the SHA-256 checksum, and (iii) a final file inventory exactly matching the manifest. Manufacturing final provenance covers 12 files; O2C and MRO each cover 14. The GitHub Releases publish archives and SHA-256 sidecars; this audit independently inspected the Actions bundles, **not** separately downloaded Release archives.

## What each organization falsifies

| Dimension | Manufacturing | O2C | MRO |
| --- | --- | --- | --- |
| Semantics | production, WIP, yield and machine capacity | administrative order, fulfillment, due time, collection and escalation | finite spare parts, maintenance capacity, inventory and preemption |
| Exogenous DOE input | quantity | due delay [1,5] with four effective tick strata | part quantity [1,20] |
| Intervention A | downtime: +7,200 s lead time, +1 breakdown | partial fulfillment: +1 partial event; no lead-time change | shortage: +3,600 s lead time, +1 material wait |
| Intervention B | yield degradation: −0.2 yield ratio; mean output −98.578697 | overdue collection: +10,800 s collection time, +1 overdue occurrence | emergency preemption: +1 interruption and +1 preemption; no lead-time change |
| Verified invariant examples | output/yield/WIP/material transitions | collected receivable, terminal state and expected events | exactly-once issue, part conservation, closure and preemption |
| Policy | fixed A0 | fixed A0 | fixed A0 |

Measured business-effect magnitudes **cannot** be ranked or averaged across these three independent reference organizations: their tasks, simulated time semantics, currencies and outcome definitions are different. The correct shared claim is that the **same experiment infrastructure** generated immutable worlds, paired runs, typed ground truths, explicit eligibility, reports and provenance across three distinct operational structures.

### Framework validation versus scientific validation

1. **Implemented framework conformance:** the same nine Gate-A checks are executed against all three domain adapters, using `test_crossdomain_gatea_matrix.py` (merged PR #384). The reference-runtime engine and CRN framework did not require three separate generic code paths.
2. **Domain-specific scientific checks:** all 612 eligible ground-truth assessments passed; all 60 eligible treatment contrasts recover the expected signs/deterministic mechanisms. The other **188** effects are retained, explicitly ineligible and excluded from claims.
3. **Deterministic inference limit:** all eligible paired standard errors are zero. Replication is a reproducibility check here, not a sample of independent organizations. Nominal 95% CIs should not be interpreted as empirically estimated confidence.
4. **Other limitations:** no A1/A2 agency, adaptive managerial policy, real-world process observations, cross-domain equivalence of productivity metrics, external validity, stationary queueing or organization-wide parameter-map coverage was tested.

## Refactoring decision based on actual duplication

The repetitive `build_*_official_plan_v1` setup for fixed A0, seeds, statistical metadata and experiment sizing is real. However, intervention eligibility, typed projections, exogenous parameter limits and runtime mechanics are **not** accidental duplication, even when method signatures match. Those should remain domain-owned.

The v1 freezes pin the entire executable `src/sose` tree and `uv.lock` for O2C and MRO. A shared runtime/protocol-builder change now would invalidate those immutable source-tree hashes and require a new versioned scientific freeze. **Do not modify frozen v1 adapters or generic runtime merely to reduce boilerplate.**

The safe near-term cross-domain extraction is in **reporting/audit tooling outside `src/sose`**, where common evidence manifests and checksums are already structurally equivalent. Any future v2 core/helper refactor needs (a) characterization tests from all three domains; (b) no change to the historical v1 result hashes when run against original source commits; (c) independent v2 freeze manifests for modified code; (d) explicit choice whether normalized plan defaults truly encode shared experiment policy.

## Trading Company PC6 boundary

Trading Company composition is a distinct concern from three-domain organizational experiments. PR #386 provides a shared contract matrix for the existing customer-demand and replenishment paths, retaining Warehouse Management as the only stock owner. PC6 promotion remains conditioned on complete typed ingress/egress provenance, duplicate/restart/fencing and cross-adapter persistence conformance. It does not follow automatically from the 612/612 A0 experiment checks.

**Conclusion:** The evidence supports **synthetic, deterministic cross-domain framework generality at A0** across production, administrative and resource-intensive processes. It does not support claims about empirical organizational performance or managerial adaptivity.
