"""Fail-closed independent OD-5a v2 execution freeze and evidence admission.

A validated manifest defines an immutable *prospective* contract, not a result.
The live manifest intentionally remains unsealed until all #461 gates pass.
"""
from __future__ import annotations

from hashlib import sha1, sha256
import json
from math import isfinite
from pathlib import Path
import os
import stat
import re
import subprocess
from typing import Any, Mapping


ARMS = (
    "baseline", "capacity_only", "topology_only",
    "coordination_only", "structural_combined",
)
COORDINATES = frozenset((
    "calendar_availability", "dependency_density",
    "routing_complexity", "nominal_service_distribution",
    "nominal_coordination_latency", "declared_intervention_cost_ratio",
))
LOAD_GRID = (0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6)
HASH_FIELDS = (
    "uv_lock_sha256", "preregistration_sha256",
    "pairability_evidence_sha256", "recovery_evidence_sha256",
    "verifier_sha256", "output_schema_sha256",
)
REQUIRED = frozenset((
    "protocol_id", "stage", "source_tree_oid", *HASH_FIELDS,
    "model_spec_hashes", "admission_hashes",
    "nonload_bounds", "coordinate_mapping",
    "design_seed", "root_seed", "n_strata", "load_grid",
    "replications", "arms", "run_count", "horizon", "warmup",
    "baseline_nominal_capacity", "service_distribution_family",
    "dependency_graph_constructor", "calendar_rule",
    "physical_resource_policy", "cost_schedule", "crn_streams",
    "censoring_rule",
))
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^[0-9a-f]{40}$")


def _finite(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} requires finite numeric value")
    result = float(value)
    if not isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{label} requires finite {'positive ' if positive else ''}numeric value")
    return result


def _hash(value: object, name: str, *, git: bool = False) -> None:
    pattern = _GIT_OID if git else _SHA256
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{name} must be an independently auditable hash")


def _check_unset(value: object) -> None:
    if isinstance(value, str) and ("UNSET" in value.upper() or "TBD" in value.upper()):
        raise ValueError("execution freeze contains UNSET or TBD admission inputs")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _check_unset(key)
            _check_unset(item)
    if isinstance(value, (list, tuple)):
        for item in value:
            _check_unset(item)


def _text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must have a registered nonempty physical definition")


def validate_od5a_execution_freeze(payload: Mapping[str, Any]) -> str:
    """Return canonical SHA-256 iff a *complete* pre-outcome manifest is admitted."""
    _validate_freeze_header(payload)
    _validate_freeze_hashes(payload)
    _validate_nonload_bounds(payload)
    _validate_coordinate_mapping(payload)
    _validate_design_geometry(payload)
    _validate_horizon_and_rules(payload)
    _validate_cost_schedule(payload)
    _validate_crn_streams(payload)
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _validate_freeze_header(payload: Mapping[str, Any]) -> None:
    _check_unset(payload)
    if set(payload) != REQUIRED:
        raise ValueError(
            f"unexpected or missing freeze fields: "
            f"{sorted(set(payload) ^ REQUIRED)}"
        )
    if payload["stage"] != "execution_freeze":
        raise ValueError("stage must be execution_freeze, never design_preregistration")
    if payload["protocol_id"] != "od5a-structure-capacity-v2":
        raise ValueError("OD-5a protocol ID conflicts with frozen design")


def _validate_freeze_hashes(payload: Mapping[str, Any]) -> None:
    _hash(payload["source_tree_oid"], "source_tree_oid", git=True)
    for field in HASH_FIELDS:
        _hash(payload[field], field)
    for field in ("model_spec_hashes", "admission_hashes"):
        hashes = payload[field]
        if not isinstance(hashes, Mapping) or set(hashes) != set(ARMS):
            raise ValueError(f"{field} must bind all five registered arms")
        for arm, value in hashes.items():
            _hash(value, f"{field}.{arm}")


def _validate_nonload_bounds(payload: Mapping[str, Any]) -> None:
    bounds = payload["nonload_bounds"]
    if not isinstance(bounds, Mapping) or set(bounds) != COORDINATES:
        raise ValueError("freeze requires exactly six non-load exogenous coordinates")
    for name, interval in bounds.items():
        if not isinstance(interval, Mapping) or set(interval) != {"low", "high"}:
            raise ValueError(f"invalid exogenous bound for {name}")
        low = _finite(interval["low"], name)
        high = _finite(interval["high"], name)
        if low >= high:
            raise ValueError(f"non-load bounds must be increasing for {name}")


def _validate_coordinate_mapping(payload: Mapping[str, Any]) -> None:
    mapping = payload["coordinate_mapping"]
    if not isinstance(mapping, Mapping) or set(mapping) != COORDINATES:
        raise ValueError("coordinate mapping must cover the six registered dimensions")
    for coordinate, definition in mapping.items():
        if not isinstance(definition, Mapping) or set(definition) != {"mapping_id", "units"}:
            raise ValueError(f"invalid coordinate mapping for {coordinate}")
        for label, value in definition.items():
            _text(value, f"{coordinate}.{label}")


def _validate_design_geometry(payload: Mapping[str, Any]) -> None:
    for seed_name in ("design_seed", "root_seed"):
        seed = payload[seed_name]
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError(f"{seed_name} must be a nonnegative integer seed")
    _validate_fixed_counts(payload)


def _require_exact_count(payload: Mapping[str, Any], field: str, expected: int, message: str) -> None:
    # bool is an int subclass: True must not pass for 1.
    if payload[field] != expected or isinstance(payload[field], bool):
        raise ValueError(message)


def _validate_fixed_counts(payload: Mapping[str, Any]) -> None:
    _require_exact_count(
        payload, "n_strata", 32, "registered Sobol geometry requires 32 non-load strata",
    )
    if (not isinstance(payload["load_grid"], list)
            or payload["load_grid"] != list(LOAD_GRID)
            or any(isinstance(x, bool) for x in payload["load_grid"])):
        raise ValueError("load grid must equal the seven registered nominal loads")
    _require_exact_count(
        payload, "replications", 16, "registered design requires exactly 16 replications",
    )
    if payload["arms"] != list(ARMS):
        raise ValueError("registered design requires five fixed arms")
    _require_exact_count(
        payload, "run_count", 17920, "registered geometry requires exactly 17,920 executions",
    )


def _validate_horizon_and_rules(payload: Mapping[str, Any]) -> None:
    horizon = _finite(payload["horizon"], "horizon", positive=True)
    warmup = _finite(payload["warmup"], "warmup")
    if not 0 <= warmup < horizon:
        raise ValueError("warmup must be inside the observation horizon")
    _finite(payload["baseline_nominal_capacity"], "baseline nominal capacity", positive=True)
    for field in (
        "service_distribution_family", "dependency_graph_constructor",
        "calendar_rule", "physical_resource_policy", "censoring_rule",
    ):
        _text(payload[field], field)


_NUMERIC_COST_FIELDS = frozenset({
    "operating_cost_at_horizon", "transition_cost",
    "transition_time", "shadow_price_per_transition_time",
})
_JUSTIFICATION_FIELD = "zero_transition_cost_justification"


def _validate_cost_schedule(payload: Mapping[str, Any]) -> None:
    costs = payload["cost_schedule"]
    if not isinstance(costs, Mapping) or set(costs) != set(ARMS[1:]):
        raise ValueError("matched costs must cover exactly four treatment arms")
    totals: dict[str, float] = {}
    for arm, cost in costs.items():
        totals[arm] = _arm_lifecycle_total(arm, cost)
    reference = totals["capacity_only"]
    if any(abs(value - reference) / reference > 0.01 for value in totals.values()):
        raise ValueError("treatment budget mismatch exceeds 1%")


def _arm_lifecycle_total(arm: str, cost: Any) -> float:
    """Validate one treatment's lifecycle cost ledger and return its total."""
    if not _is_complete_cost_ledger(cost):
        raise ValueError(f"incomplete lifecycle cost schedule: {arm}")
    values = {
        key: _finite(cost[key], f"{arm}.{key}")
        for key in _NUMERIC_COST_FIELDS
    }
    if any(value < 0 for value in values.values()):
        raise ValueError("lifecycle costs cannot be negative")
    _validate_zero_cost_justification(arm, cost, values["transition_cost"])
    if values["transition_time"] > 0 and values["shadow_price_per_transition_time"] <= 0:
        raise ValueError("transition time must be priced explicitly")
    total = (
        values["operating_cost_at_horizon"] + values["transition_cost"]
        + values["transition_time"] * values["shadow_price_per_transition_time"]
    )
    if not isfinite(total) or total <= 0:
        raise ValueError("matched-cost budget must be finite and positive")
    return total


def _is_complete_cost_ledger(cost: Any) -> bool:
    return isinstance(cost, Mapping) and (
        set(cost) == _NUMERIC_COST_FIELDS
        or set(cost) == _NUMERIC_COST_FIELDS | {_JUSTIFICATION_FIELD}
    )


def _validate_zero_cost_justification(arm: str, cost: Mapping[str, Any], transition_cost: float) -> None:
    if transition_cost == 0:
        reason = cost.get(_JUSTIFICATION_FIELD)
        if not isinstance(reason, str) or len(reason.strip()) < 20:
            raise ValueError(
                f"zero transition cost requires prospective substantive justification: {arm}"
            )
    elif _JUSTIFICATION_FIELD in cost:
        raise ValueError(f"zero-cost justification is invalid for a charged treatment: {arm}")


def _validate_crn_streams(payload: Mapping[str, Any]) -> None:
    crn = payload["crn_streams"]
    if not isinstance(crn, Mapping) or set(crn) != {
        "arrival", "service", "coordination", "entity_identity", "draw_index",
    }:
        raise ValueError("all unaffected counter-CRN stream identities are required")
    for key, value in crn.items():
        _text(value, f"crn.{key}")
    if len({crn[name] for name in ("arrival", "service", "coordination")}) != 3:
        raise ValueError("mechanism counter-CRN streams must have unique identities")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate execution-freeze key: {key}")
        result[key] = value
    return result


def load_od5a_execution_freeze(path: str | Path) -> tuple[dict[str, Any], str]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(payload, dict):
        raise ValueError("execution freeze must be a JSON object")
    return payload, validate_od5a_execution_freeze(payload)


def _check_tracked_source_bytes(root: Path) -> None:
    """Verify real worktree bytes/modes against frozen HEAD, bypassing index flags.

    Git status can miss files marked assume-unchanged or skip-worktree.
    Inspect HEAD blobs and hash the bytes that Python would actually import.
    """
    records = subprocess.check_output(
        ["git", "ls-tree", "-rz", "HEAD", "--", "src/sose"], cwd=root,
    )
    if not records:
        raise ValueError("frozen source tree contains no tracked files")
    for record in records.split(b"\x00"):
        if not record:
            continue
        _check_tracked_record(root, record)


def _check_tracked_record(root: Path, record: bytes) -> None:
    """One `git ls-tree -z` entry: type, mode and actual bytes must match HEAD."""
    meta, relative = record.split(b"\t", 1)
    mode, kind, expected_oid = meta.split(b" ", 2)
    if kind != b"blob" or mode not in (b"100644", b"100755", b"120000"):
        raise ValueError("frozen source tree has unsupported Git entry")
    path = root / os.fsdecode(relative)
    try:
        disk = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError("dirty or missing tracked source file") from exc
    if mode == b"120000" or stat.S_ISLNK(disk.st_mode):
        # Link text is frozen, but external target bytes are not. Disallow
        # links completely until independently frozen target provenance exists.
        raise ValueError("source symlinks are not admitted by OD-5a execution freeze")
    if not stat.S_ISREG(disk.st_mode):
        raise ValueError("tracked source file type mismatch")
    if bool(disk.st_mode & 0o111) != (mode == b"100755"):
        raise ValueError("tracked source file mode mismatch")
    contents = path.read_bytes()
    actual_oid = sha1(
        b"blob " + str(len(contents)).encode("ascii") + b"\x00" + contents
    ).hexdigest().encode("ascii")
    if actual_oid != expected_oid:
        raise ValueError("dirty tracked source byte mismatch with frozen Git blob")


def verify_od5a_checkout_identity(payload: Mapping[str, Any], root: str | Path) -> str:
    """Verify actual checked-out source tree, lock bytes and protocol before run."""
    manifest_hash = validate_od5a_execution_freeze(payload)
    root = Path(root).resolve()
    actual_root = Path(subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], cwd=root, text=True,
    ).strip()).resolve()
    if root != actual_root:
        raise ValueError("execution-freeze root must be the actual Git checkout root")
    # HEAD:src/sose excludes modified, staged, untracked and ignored local code.
    # Admitted scientific source identity must match the actual checkout.
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all", "--ignored=matching",
         "--", "src/sose"],
        cwd=root, text=True,
    )
    if dirty.strip():
        raise ValueError("dirty source checkout differs from independently frozen Git tree")
    source_tree_oid = subprocess.check_output(
        ["git", "rev-parse", "HEAD:src/sose"], cwd=root, text=True,
    ).strip()
    if source_tree_oid != payload["source_tree_oid"]:
        raise ValueError("source tree differs from independently frozen source_tree_oid")
    _check_tracked_source_bytes(root)
    for name, field in (
        ("uv.lock", "uv_lock_sha256"),
        ("docs/organizational/preregistration/od5a-structure-capacity-v2-design.json",
         "preregistration_sha256"),
    ):
        actual = sha256((root / name).read_bytes()).hexdigest()
        if actual != payload[field]:
            raise ValueError(f"independent frozen file hash changed: {name}")
    return manifest_hash
