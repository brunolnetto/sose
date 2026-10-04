from __future__ import annotations

from types import SimpleNamespace

import pytest

from sose.examples.aviation import definition as aviation_definition
from sose.examples.credit_loans import definition as credit_definition


class _FakePersistence:
    def __init__(self, entities: dict[tuple[str, str], object | None]):
        self._entities = entities

    def entity(self, kind: str, entity_id: str):
        return self._entities.get((kind, entity_id))


def _config_with(base, **updates):
    return base.model_copy(update=updates)


def _flight(*, flight_id: str, state: str):
    return SimpleNamespace(id=flight_id, state=state)


def test_aviation_reconcile_aog_branch_returns_false_without_work_order():
    persistence = _FakePersistence({})
    config = aviation_definition.definition.default_config()

    handled = aviation_definition._reconcile_aog_branch(
        persistence,
        object(),
        object(),
        config,
        SimpleNamespace(),
        flight_id="F1",
    )

    assert handled is False


@pytest.mark.parametrize(
    ("work_state", "expected_call"),
    [
        ("released", "maintenance"),
        ("waiting_bay", "maintenance"),
        ("in_progress", "complete"),
    ],
)
def test_aviation_reconcile_aog_branch_routes_work_states(monkeypatch, work_state, expected_call):
    flight_id = "F1"
    work_id = aviation_definition.maintenance_work_order_id(flight_id)
    demand_id = aviation_definition.part_demand_id(flight_id)
    work = SimpleNamespace(id=work_id, state=work_state)
    demand = SimpleNamespace(id=demand_id, state="issued")
    persistence = _FakePersistence(
        {
            ("aviation_maintenance_work_order", work_id): work,
            ("aviation_part_demand", demand_id): demand,
        }
    )
    config = _config_with(
        aviation_definition.definition.default_config(),
        auto_seed_aog_part=True,
    )
    calls: list[str] = []
    monkeypatch.setattr(
        aviation_definition,
        "seed_spare_part",
        lambda *args, **kwargs: calls.append("seed"),
    )
    monkeypatch.setattr(
        aviation_definition,
        "reconcile_part_issue",
        lambda *args, **kwargs: calls.append("issue"),
    )
    monkeypatch.setattr(
        aviation_definition,
        "reconcile_aog_maintenance",
        lambda *args, **kwargs: calls.append("maintenance"),
    )
    monkeypatch.setattr(
        aviation_definition,
        "complete_aog_maintenance",
        lambda *args, **kwargs: calls.append("complete"),
    )

    handled = aviation_definition._reconcile_aog_branch(
        persistence,
        object(),
        object(),
        config,
        SimpleNamespace(),
        flight_id=flight_id,
    )

    assert handled is True
    assert calls[0] == "seed"
    assert expected_call in calls


def test_aviation_reconcile_aog_branch_calls_part_issue_when_demand_missing(monkeypatch):
    flight_id = "F2"
    work_id = aviation_definition.maintenance_work_order_id(flight_id)
    persistence = _FakePersistence(
        {
            ("aviation_maintenance_work_order", work_id): SimpleNamespace(
                id=work_id,
                state="waiting_part",
            ),
            ("aviation_part_demand", aviation_definition.part_demand_id(flight_id)): None,
        }
    )
    calls: list[str] = []
    monkeypatch.setattr(
        aviation_definition,
        "reconcile_part_issue",
        lambda *args, **kwargs: calls.append("issue"),
    )

    handled = aviation_definition._reconcile_aog_branch(
        persistence,
        object(),
        object(),
        _config_with(
            aviation_definition.definition.default_config(),
            auto_seed_aog_part=False,
        ),
        SimpleNamespace(),
        flight_id=flight_id,
    )

    assert handled is True
    assert calls == ["issue"]


@pytest.mark.parametrize(
    ("flight_state", "auto_land", "expected"),
    [
        ("due", True, "depart"),
        ("airborne", True, "land"),
        ("inspection", False, "inspect"),
    ],
)
def test_aviation_reconcile_tick_dispatches_state_handlers(
    monkeypatch,
    flight_state,
    auto_land,
    expected,
):
    entities = SimpleNamespace(leg1_id="L1", leg2_id="L2")
    persistence = _FakePersistence(
        {
            ("aviation_flight", "L1"): _flight(flight_id="L1", state=flight_state),
            ("aviation_flight", "L2"): _flight(flight_id="L2", state="ready"),
        }
    )
    calls: list[str] = []
    monkeypatch.setattr(aviation_definition, "schedule_departure", lambda *a, **k: None)
    monkeypatch.setattr(aviation_definition, "_reconcile_aog_branch", lambda *a, **k: False)
    monkeypatch.setattr(
        aviation_definition,
        "reconcile_departure",
        lambda *args, **kwargs: calls.append("depart"),
    )
    monkeypatch.setattr(
        aviation_definition,
        "land_flight",
        lambda *args, **kwargs: calls.append("land"),
    )
    monkeypatch.setattr(
        aviation_definition,
        "reconcile_inspection",
        lambda *args, **kwargs: calls.append("inspect"),
    )
    config = _config_with(
        aviation_definition.definition.default_config(),
        auto_land=auto_land,
    )

    aviation_definition._reconcile_tick(
        persistence,
        object(),
        object(),
        config,
        entities,
    )

    assert calls == [expected]


def test_aviation_reconcile_tick_raises_for_missing_persisted_flight(monkeypatch):
    entities = SimpleNamespace(leg1_id="L1", leg2_id="L2")
    persistence = _FakePersistence(
        {
            ("aviation_flight", "L1"): None,
            ("aviation_flight", "L2"): _flight(flight_id="L2", state="ready"),
        }
    )
    monkeypatch.setattr(aviation_definition, "schedule_departure", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="aviation flight was not persisted"):
        aviation_definition._reconcile_tick(
            persistence,
            object(),
            object(),
            aviation_definition.definition.default_config(),
            entities,
        )


def _installment(*, installment_id: str, state: str, amount: float, paid_amount: float):
    return SimpleNamespace(
        id=installment_id,
        state=state,
        attributes={
            "amount": amount,
            "paid_amount": paid_amount,
        },
    )


def test_credit_process_due_installment_auto_pay_posts_remaining_amount(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        credit_definition,
        "post_payment",
        lambda *args, **kwargs: calls.append("pay"),
    )
    monkeypatch.setattr(
        credit_definition,
        "schedule_overdue",
        lambda *args, **kwargs: calls.append("overdue"),
    )
    config = _config_with(
        credit_definition.definition.default_config(),
        auto_pay_due_installments=True,
    )

    handled = credit_definition._process_due_installment(
        object(),
        object(),
        object(),
        config,
        installment=_installment(
            installment_id="I1",
            state="due",
            amount=100,
            paid_amount=20,
        ),
    )

    assert handled is True
    assert calls == ["pay"]


def test_credit_process_due_installment_schedules_overdue_when_auto_pay_disabled(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        credit_definition,
        "schedule_overdue",
        lambda *args, **kwargs: calls.append("overdue"),
    )
    config = _config_with(
        credit_definition.definition.default_config(),
        auto_pay_due_installments=False,
    )

    handled = credit_definition._process_due_installment(
        object(),
        object(),
        object(),
        config,
        installment=_installment(
            installment_id="I1",
            state="partially_paid",
            amount=100,
            paid_amount=50,
        ),
    )

    assert handled is True
    assert calls == ["overdue"]


def test_credit_process_overdue_installment_auto_pay_and_cure(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        credit_definition,
        "reconcile_collection",
        lambda *args, **kwargs: calls.append("collect"),
    )
    monkeypatch.setattr(
        credit_definition,
        "post_payment",
        lambda *args, **kwargs: calls.append("pay"),
    )
    monkeypatch.setattr(
        credit_definition,
        "cure_delinquency",
        lambda *args, **kwargs: calls.append("cure"),
    )
    config = _config_with(
        credit_definition.definition.default_config(),
        auto_pay_due_installments=True,
    )

    handled = credit_definition._process_overdue_installment(
        object(),
        object(),
        object(),
        config,
        installment=_installment(
            installment_id="I2",
            state="overdue",
            amount=80,
            paid_amount=0,
        ),
    )

    assert handled is True
    assert calls == ["collect", "pay", "cure"]


def test_credit_reconcile_application_state_submitted_and_rejected(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        credit_definition,
        "reconcile_underwriting",
        lambda *args, **kwargs: calls.append("underwriting"),
    )
    persistence = _FakePersistence({})
    config = credit_definition.definition.default_config()
    entities = SimpleNamespace()

    submitted = SimpleNamespace(id="A1", state="submitted")
    loan, handled = credit_definition._reconcile_application_state(
        persistence,
        object(),
        object(),
        config,
        entities=entities,
        application=submitted,
    )
    assert (loan, handled) == (None, True)
    assert calls == ["underwriting"]

    rejected = SimpleNamespace(id="A2", state="rejected")
    loan, handled = credit_definition._reconcile_application_state(
        persistence,
        object(),
        object(),
        config,
        entities=entities,
        application=rejected,
    )
    assert (loan, handled) == (None, True)


@pytest.mark.parametrize("loan_state", [None, "approved", "disbursed"])
def test_credit_reconcile_application_state_disburses_approved_application(
    monkeypatch,
    loan_state,
):
    calls: list[str] = []
    monkeypatch.setattr(
        credit_definition,
        "disburse_and_schedule",
        lambda *args, **kwargs: calls.append("disburse"),
    )
    loan = None if loan_state is None else SimpleNamespace(state=loan_state, id="L1")
    persistence = _FakePersistence(
        {
            ("loan", credit_definition.loan_id("A1")): loan,
        }
    )

    result_loan, handled = credit_definition._reconcile_application_state(
        persistence,
        object(),
        object(),
        credit_definition.definition.default_config(),
        entities=SimpleNamespace(),
        application=SimpleNamespace(id="A1", state="approved"),
    )

    assert (result_loan, handled) == (None, True)
    assert calls == ["disburse"]


def test_credit_reconcile_application_state_returns_existing_loan_when_not_handled():
    loan = SimpleNamespace(id="L9", state="active")
    persistence = _FakePersistence(
        {
            ("loan", credit_definition.loan_id("A9")): loan,
        }
    )

    result_loan, handled = credit_definition._reconcile_application_state(
        persistence,
        object(),
        object(),
        credit_definition.definition.default_config(),
        entities=SimpleNamespace(),
        application=SimpleNamespace(id="A9", state="active"),
    )

    assert result_loan is loan
    assert handled is False


def test_credit_process_overdue_installment_auto_pay_disabled_short_circuits(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        credit_definition,
        "reconcile_collection",
        lambda *args, **kwargs: calls.append("collect"),
    )
    monkeypatch.setattr(
        credit_definition,
        "post_payment",
        lambda *args, **kwargs: calls.append("pay"),
    )
    monkeypatch.setattr(
        credit_definition,
        "cure_delinquency",
        lambda *args, **kwargs: calls.append("cure"),
    )
    config = _config_with(
        credit_definition.definition.default_config(),
        auto_pay_due_installments=False,
    )

    handled = credit_definition._process_overdue_installment(
        object(),
        object(),
        object(),
        config,
        installment=_installment(
            installment_id="I3",
            state="overdue",
            amount=50,
            paid_amount=0,
        ),
    )

    assert handled is True
    assert calls == ["collect"]


def test_credit_reconcile_installments_skips_missing_and_returns_on_overdue(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        credit_definition,
        "_process_due_installment",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        credit_definition,
        "_process_overdue_installment",
        lambda *args, **kwargs: calls.append("overdue") or True,
    )
    loan = SimpleNamespace(id="L1", attributes={"installment_count": 2})
    present = _installment(
        installment_id=credit_definition.installment_id("L1", 2),
        state="overdue",
        amount=10,
        paid_amount=0,
    )
    persistence = _FakePersistence(
        {
            ("loan_installment", credit_definition.installment_id("L1", 1)): None,
            ("loan_installment", credit_definition.installment_id("L1", 2)): present,
        }
    )

    credit_definition._reconcile_installments(
        persistence,
        object(),
        object(),
        credit_definition.definition.default_config(),
        loan=loan,
    )
    assert calls == ["overdue"]


def test_credit_reconcile_tick_returns_when_no_loan_is_available(monkeypatch):
    monkeypatch.setattr(
        credit_definition,
        "_application_or_error",
        lambda persistence, entities: SimpleNamespace(id="A1"),
    )
    monkeypatch.setattr(
        credit_definition,
        "_reconcile_application_state",
        lambda *args, **kwargs: (None, False),
    )
    monkeypatch.setattr(
        credit_definition,
        "_reconcile_installments",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("installment reconciliation should not run without loan")
        ),
    )

    credit_definition._reconcile_tick(
        object(),
        object(),
        object(),
        credit_definition.definition.default_config(),
        SimpleNamespace(application_id="A1"),
    )
