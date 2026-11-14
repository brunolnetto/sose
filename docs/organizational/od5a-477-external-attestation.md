# #477: independently pinned external-issuer attestation for nonconfirmatory evidence

The existing five-arm `scripts/verify_od5a_477_evidence.py` checks hashes but
returns `UNSIGNED_NO_TRUSTED_ISSUER`. A SHA-256 digest is not an issuer.
This separate offline gate requires a detached RSA/SHA-256 signature over a
canonical statement derived from **all five independently verified raw arm
files** and an **out-of-band public-key fingerprint**.

- The issuer provisions the private key outside SOSE/CI and gives the auditor
  only its trusted public PEM and independently approved SHA-256 DER SPKI pin.
- Generate the statement with the root/current data and external issuer ID:
  `python -m scripts.verify_od5a_477_attestation od5a-477-evidence --issuer-id od5a:independent-operator --emit-statement /secure/statement.json`
- The issuer signs *exactly that file* offline:
  `openssl dgst -sha256 -sign /secure/private-key.pem -out /secure/statement.sig /secure/statement.json`
- Independently derive the expected public-key fingerprint:
  `openssl pkey -pubin -in issuer-public.pem -outform DER | sha256sum`
- Independently preregister the **approved source Git commit OID** (40 lowercase hex) and the **approved `uv.lock` SHA-256** (64 lowercase hex) in the auditor's controlled release manifest. Never derive these pins from the five-arm evidence or its signed statement: a valid signature alone does not prove that the signer used the approved code and dependencies.
- Auditor verifies **all three independently provided pins**:
  `python -m scripts.verify_od5a_477_attestation od5a-477-evidence --issuer-id od5a:independent-operator --trusted-public-key /trusted/issuer-public.pem --expected-key-sha256 "$TRUSTED_SPKI_SHA256" --expected-source-git-oid "$APPROVED_GIT_OID" --expected-uv-lock-sha256 "$APPROVED_UV_LOCK_SHA256" --signature /secure/statement.sig`
- A missing, malformed or mismatched source/lock pin fails closed, even for a valid newly signed bundle. `auditor_provenance_pins_matched` indicates only that caller-supplied pins match independently rehashed raw evidence, **not** that the caller actually obtained them out-of-band.

CI unit tests use disposable test keys only; they cannot stand in for a real
independent issuer, nor can successful signature verification prove actual server
faults, fairness/preemption, multi-phase causal equivalence, or scientific
admission. All records stay `NON_CONFIRMATORY / NOT_ADMITTED` and issue #477
remains OPEN until the separate operational gates are certified.
