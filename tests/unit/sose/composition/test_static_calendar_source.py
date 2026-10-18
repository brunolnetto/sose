"""#495 static UTC calendar contract falsifiers (never runtime permission)."""
from dataclasses import replace
import json
from datetime import date

import pytest

from sose.composition.calendar import (
    BUNDLED_UTC_CALENDAR_SHA256,
    BUNDLED_UTC_CALENDAR_BYTES,
    WEEKDAYS_MONDAY_ZERO,
    parse_static_calendar_source,
    verify_static_calendar_reference,
)
from sose.composition.organization import (
    OrganizationDefinition, OperationalConfigReference, ProcessComponent,
    validate_organization_admission,
)
from sose.composition.boundary import BoundaryConsumerRegistry
from sose.examples.catalog import builtin_catalog
from sose.examples.process_manifest import builtin_process_manifests


def _definition():
    component = ProcessComponent(
        component_id="fulfillment", domain="warehouse_fulfillment",
        owner_id="owner:fulfillment",
        statechart_types=("FulfillmentOrderChart",),
    )
    ref = OperationalConfigReference(
        component_id=component.component_id, kind="calendar",
        reference_id="sose:calendar:weekday-utc-v1",
        reference_version=1,
        source_sha256=BUNDLED_UTC_CALENDAR_SHA256,
        config_epoch=0,
    )
    return OrganizationDefinition(
        organization_id="organization:static-calendar",
        version=1, components=(component,), operational_configs=(ref,),
    )


def test_bundled_calendar_source_sha_is_independently_pinned_and_validated():
    assert BUNDLED_UTC_CALENDAR_SHA256 == (
        "c85fd9442be1913a652c245118484a1d272f7212c6cd8e2d15530f013c81e6e4"
    )
    source = parse_static_calendar_source(BUNDLED_UTC_CALENDAR_BYTES)
    assert source.source_sha256 == BUNDLED_UTC_CALENDAR_SHA256
    assert source.timezone == "UTC"
    assert len(source.windows) == 5
    assert source.windows[0].weekday == 0
    assert (source.windows[0].start, source.windows[0].end) == ("09:00", "17:00")
    definition = _definition()
    assert verify_static_calendar_reference(definition, definition.operational_configs[0]) == source
    assert verify_static_calendar_reference(
        replace(definition, components=tuple(definition.components)),
        definition.operational_configs[0],
    ) == source


@pytest.mark.parametrize("mutation,match", [
    ({"windows": []}, "windows"),
    ({"timezone": "America/Sao_Paulo"}, "UTC"),
    ({"schema_version": True}, "schema_version"),
    ({"config_epoch": -1}, "config_epoch"),
    ({"windows": [{"weekday": 0, "start": "18:00", "end": "09:00"}]}, "window"),
    ({"windows": [
        {"weekday": 0, "start": "09:00", "end": "12:00"},
        {"weekday": 0, "start": "11:00", "end": "13:00"},
    ]}, "overlap"),
    ({"windows": [{"weekday": 7, "start": "09:00", "end": "12:00"}]}, "weekday"),
    ({"windows": [{"weekday": 0, "start": "09:99", "end": "12:00"}]}, "time"),
])
def test_static_calendar_rejects_invalid_semantic_shapes(mutation, match):
    data = json.loads(BUNDLED_UTC_CALENDAR_BYTES)
    data.update(mutation)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ValueError, match=match):
        parse_static_calendar_source(canonical)


def test_static_calendar_rejects_unknown_fields_and_ambiguous_json():
    data = json.loads(BUNDLED_UTC_CALENDAR_BYTES)
    data["runtime_grant"] = True
    with pytest.raises(ValueError, match="fields"):
        parse_static_calendar_source(json.dumps(data, sort_keys=True, separators=(",", ":")).encode())
    with pytest.raises(ValueError, match="duplicate"):
        parse_static_calendar_source(b'{"schema_version":1,"schema_version":1}')


@pytest.mark.parametrize("change", [
    {"source_sha256": "a" * 64},
    {"reference_id": "self-attested:calendar"},
    {"reference_version": 2},
    {"config_epoch": 1},
    {"component_id": "another-component"},
    {"kind": "policy"},
])
def test_approved_static_calendar_does_not_accept_caller_digest_or_reference_claim(change):
    original = _definition()
    ref = replace(original.operational_configs[0], **change)
    if ref.config_epoch != original.config_epoch:
        with pytest.raises(ValueError, match="epoch"):
            replace(original, operational_configs=(ref,))
        return
    if ref.component_id not in {component.component_id for component in original.components}:
        with pytest.raises(ValueError, match="dangling component"):
            replace(original, operational_configs=(ref,))
        return
    # A syntactically valid forged reference MUST be declared in the supplied
    # organization before its ID, version and digest can be meaningfully
    # tested against trusted bytes. Passing the old reference only exercises
    # the membership guard and leaves the actual attestation untested.
    declared = replace(original, operational_configs=(ref,))
    with pytest.raises(ValueError, match="calendar|provenance|reference"):
        verify_static_calendar_reference(declared, declared.operational_configs[0])


def test_positive_static_calendar_does_not_grant_runtime_admission():
    definition = _definition()
    assert verify_static_calendar_reference(
        definition, definition.operational_configs[0],
    ).source_sha256 == BUNDLED_UTC_CALENDAR_SHA256
    with pytest.raises(ValueError, match="operational|audited runtime"):
        validate_organization_admission(
            definition,
            catalog=builtin_catalog(),
            manifests=builtin_process_manifests(),
            consumers=BoundaryConsumerRegistry(),
        )


def test_calendar_weekday_numbers_are_nameless_only_in_bytes_not_semantics():
    """Prevent ISO/JavaScript Sunday-zero reinterpretation after source freeze."""
    assert WEEKDAYS_MONDAY_ZERO == (
        "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday",
    )
    assert date(2026, 10, 19).weekday() == 0  # Monday
    assert date(2026, 10, 23).weekday() == 4  # Friday
    assert date(2026, 10, 25).weekday() == 6  # Sunday
    source = parse_static_calendar_source(BUNDLED_UTC_CALENDAR_BYTES)
    assert [WEEKDAYS_MONDAY_ZERO[window.weekday] for window in source.windows] == [
        "monday", "tuesday", "wednesday", "thursday", "friday",
    ]



@pytest.mark.parametrize("payload,pattern", [
    (None, "exact immutable"),
    ("{}", "exact immutable"),
    (b"\xff", "invalid calendar source"),
    (b"{", "invalid calendar source"),
    (b"[]", "fields"),
    (b"null", "fields"),
    (b"1", "fields"),
    (b'{"schema_version":1}', "fields"),
])
def test_static_calendar_rejects_untrusted_source_bytes(payload, pattern):
    with pytest.raises((ValueError, TypeError), match=pattern):
        parse_static_calendar_source(payload)


@pytest.mark.parametrize("key,value,pattern", [
    ("schema_version", 2, "schema_version"),
    ("reference_id", "", "reference_id"),
    ("reference_id", None, "reference_id"),
    ("reference_version", False, "reference_version"),
    ("reference_version", 2, "reference_version"),
    ("config_epoch", True, "config_epoch"),
    ("windows", "not-a-list", "windows"),
    ("windows", None, "windows"),
    ("windows", [{"weekday": 0}], "fields"),
    ("windows", [None], "fields"),
    ("windows", [{"weekday": True, "start": "09:00", "end": "17:00"}], "weekday"),
    ("windows", [{"weekday": -1, "start": "09:00", "end": "17:00"}], "weekday"),
    ("windows", [{"weekday": 0, "start": 9, "end": "17:00"}], "time"),
    ("windows", [{"weekday": 0, "start": "09:00", "end": 17}], "time"),
    ("windows", [{"weekday": 0, "start": "24:00", "end": "23:00"}], "time"),
    ("windows", [{"weekday": 0, "start": "09:00", "end": "24:00"}], "time"),
    ("windows", [{"weekday": 0, "start": "12:00", "end": "12:00"}], "positive"),
    ("windows", [
        {"weekday": 1, "start": "10:00", "end": "12:00"},
        {"weekday": 0, "start": "10:00", "end": "12:00"},
    ], "ordered"),
])
def test_calendar_parser_closed_schema_negative_witnesses(key, value, pattern):
    data = json.loads(BUNDLED_UTC_CALENDAR_BYTES)
    data[key] = value
    source = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with pytest.raises(ValueError, match=pattern):
        parse_static_calendar_source(source)


def test_calendar_parser_rejects_semantically_identical_but_noncanonical_bytes():
    data = json.loads(BUNDLED_UTC_CALENDAR_BYTES)
    noncanonical = json.dumps(data, sort_keys=False, indent=2).encode("utf-8")
    assert json.loads(noncanonical) == data
    with pytest.raises(ValueError, match="canonical"):
        parse_static_calendar_source(noncanonical)


def test_calendar_attestation_fails_closed_for_wrong_types_and_shipped_byte_tamper(
    monkeypatch,
):
    import sose.composition.calendar as trusted
    definition = _definition()
    ref = definition.operational_configs[0]
    with pytest.raises(TypeError, match="OrganizationDefinition"):
        verify_static_calendar_reference(object(), ref)
    with pytest.raises(TypeError, match="OperationalConfigReference"):
        verify_static_calendar_reference(definition, object())
    other = replace(
        definition,
        operational_configs=(replace(ref, reference_id="another-reference"),),
    )
    with pytest.raises(ValueError, match="provenance"):
        verify_static_calendar_reference(other, other.operational_configs[0])
    with pytest.raises(ValueError, match="declared"):
        verify_static_calendar_reference(other, ref)
    broken = replace(
        definition, components=(replace(
            definition.components[0], component_id="different-component",
        ),),
        operational_configs=(replace(ref, component_id="different-component"),),
    )
    with pytest.raises(ValueError, match="component|declared"):
        verify_static_calendar_reference(broken, ref)

    # No checked-in reference can self-attest bytes after source drift.
    monkeypatch.setattr(
        trusted, "BUNDLED_UTC_CALENDAR_BYTES",
        BUNDLED_UTC_CALENDAR_BYTES + b" ",
    )
    with pytest.raises(ValueError, match="provenance"):
        verify_static_calendar_reference(definition, ref)
