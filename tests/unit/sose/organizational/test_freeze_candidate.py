from __future__ import annotations

import json
from pathlib import Path

import pytest

from sose.organizational.freeze_candidate import DEPENDENCIES, export_candidate


@pytest.mark.parametrize("domain", ["o2c", "mro"])
def test_freeze_candidate_is_complete_deterministic_and_not_official(tmp_path: Path, domain: str) -> None:
    root = Path.cwd()
    left = tmp_path / f"{domain}-first"
    right = tmp_path / f"{domain}-second"

    first = export_candidate(domain=domain, root=root, destination=left)
    second = export_candidate(domain=domain, root=root, destination=right)
    assert first == second
    assert first["status"] == "candidate-not-frozen"
    assert first["official_execution_performed"] is False
    assert first["scientific_claims_authorized"] is False
    assert set(first["files"]) == set(DEPENDENCIES[domain])
    assert first["protocol_hash"] != first["plan_hash"]
    assert len(first["protocol_hash"]) == len(first["plan_hash"]) == 64
    assert json.loads((left / "protocol.json").read_text()) == json.loads(
        (right / "protocol.json").read_text()
    )
    assert json.loads((left / "plan.json").read_text()) == json.loads(
        (right / "plan.json").read_text()
    )
    with pytest.raises(FileExistsError):
        export_candidate(domain=domain, root=root, destination=left)


def test_freeze_candidate_rejects_missing_inputs(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        export_candidate(domain="o2c", root=tmp_path, destination=tmp_path / "missing")


def test_freeze_candidate_rejects_unsupported_domain(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        export_candidate(domain="unknown", root=Path.cwd(), destination=tmp_path / "unknown")


def test_candidate_publication_never_exposes_partial_final_dir(tmp_path: Path, monkeypatch) -> None:
    from sose.organizational import freeze_candidate

    original = freeze_candidate._write_json
    writes = 0

    def fail_second_write(path, value):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected write failure")
        return original(path, value)

    monkeypatch.setattr(freeze_candidate, "_write_json", fail_second_write)
    destination = tmp_path / "o2c"
    with pytest.raises(OSError, match="injected write failure"):
        export_candidate(domain="o2c", root=Path.cwd(), destination=destination)
    assert not destination.exists()
    assert (tmp_path / "o2c.staging" / "protocol.json").is_file()
