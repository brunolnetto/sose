"""Export prospective O2C/MRO scientific freeze candidates without running worlds.

Candidate manifests are NOT authority: only reviewed, merged, independently
anchored frozen artifacts may authorize official experimental execution.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path

from .o2c_protocol import build_o2c_official_plan_v1
from .mro_protocol import build_mro_official_plan_v1


DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "o2c": (
        "src/sose/organizational/o2c_protocol.py",
        "src/sose/organizational/o2c_reference.py",
        "src/sose/examples/order_to_cash/process_audit.py",
        "docs/examples/order-to-cash/specification.md",
        "docs/product/requirements/prd-0005-o2c-experiment-v1.md",
        "docs/technical/requirements/trd-0005-o2c-experiment-v1.md",
        "docs/organizational/preregistration/o2c-experiment-v1-proposal.md",
    ),
    "mro": (
        "src/sose/organizational/mro_protocol.py",
        "src/sose/organizational/mro_reference.py",
        "src/sose/examples/mro/process_audit.py",
        "docs/examples/mro/specification.md",
        "docs/product/requirements/prd-0006-mro-experiment-v1.md",
        "docs/technical/requirements/trd-0006-mro-experiment-v1.md",
        "docs/organizational/preregistration/mro-experiment-v1-proposal.md",
    ),
}

BUILDERS = {"o2c": build_o2c_official_plan_v1, "mro": build_mro_official_plan_v1}


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def export_candidate(*, domain: str, root: Path, destination: Path) -> dict[str, object]:
    """Export reproducible configuration artifacts, rejecting incomplete evidence."""
    if domain not in BUILDERS:
        raise ValueError(f"unsupported prospective domain: {domain}")
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite existing candidate: {destination}")
    plan = BUILDERS[domain]()
    files = {}
    for name in DEPENDENCIES[domain]:
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(f"missing dependency: {name}")
        files[name] = sha256(path.read_bytes()).hexdigest()

    manifest = {
        "status": "candidate-not-frozen",
        "domain": domain,
        "protocol_hash": plan.protocol.protocol_hash,
        "plan_hash": plan.plan_hash,
        "files": files,
        "official_execution_performed": False,
        "scientific_claims_authorized": False,
    }
    staging = destination.with_name(destination.name + ".staging")
    if staging.exists():
        raise FileExistsError(f"unresolved candidate staging directory: {staging}")
    staging.mkdir(parents=True, exist_ok=False)
    _write_json(staging / "protocol.json", plan.protocol.canonical_payload())
    _write_json(staging / "plan.json", plan.canonical_payload())
    _write_json(staging / "freeze-candidate-manifest.json", manifest)
    staging.rename(destination)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", choices=sorted(BUILDERS), required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export_candidate(domain=args.domain, root=args.root, destination=args.destination), sort_keys=True))


if __name__ == "__main__":
    main()
