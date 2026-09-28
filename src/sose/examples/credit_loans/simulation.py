from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.runtime import ResourceDefinition
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import (
    CollectionCase,
    CreditDecision,
    DelinquencyCase,
    Installment,
    Loan,
    LoanApplication,
    Payment,
    Restructure,
)
from .scenarios import ORIGIN
from .statecharts import (
    CollectionCaseChart,
    CreditDecisionChart,
    DelinquencyCaseChart,
    InstallmentChart,
    LoanApplicationChart,
    LoanChart,
    PaymentChart,
    RestructureChart,
)


FIRST_DUE_DELAY = timedelta(hours=2)
INSTALLMENT_INTERVAL = timedelta(hours=2)
OVERDUE_GRACE = timedelta(hours=1)
COLLECTION_FOLLOWUP = timedelta(hours=1)


def _usd_cents(value: float) -> int:
    scaled = float(value) * 100
    if not math.isfinite(scaled):
        raise ValueError("USD amount must be finite")

    # Float-facing References tolerate representational noise around an exact
    # minor-unit boundary, but never enough to reinterpret a true half-minor
    # value as integral. This avoids an arbitrary magnitude cutoff while keeping
    # the acceptance region strictly narrower than half a minor unit.
    ulp = math.ulp(scaled)
    nearest = round(scaled)
    half_minor_cap = math.nextafter(0.5, 0.0)
    tolerance = min(max(1e-7, 2 * ulp), half_minor_cap)
    if abs(scaled - nearest) > tolerance:
        raise ValueError("USD amount must not contain fractional cents")
    return int(nearest)


@dataclass(frozen=True, slots=True)
class CreditLoanEntities:
    application_id: str


def flow_correlation_id(application_id: str) -> str:
    return deterministic_id("credit-loans-flow", application_id)


def credit_decision_id(application_id: str) -> str:
    return deterministic_id(
        "entity", "credit_decision", "credit-loans-reference", application_id, "decision"
    )


def loan_id(application_id: str) -> str:
    return deterministic_id(
        "entity", "loan", "credit-loans-reference", application_id, "loan"
    )


def installment_id(loan_id_value: str, ordinal: int) -> str:
    return deterministic_id(
        "entity", "loan_installment", "credit-loans-reference", loan_id_value, "installment", ordinal
    )


def payment_id(installment_id_value: str, ordinal: int) -> str:
    return deterministic_id(
        "entity", "loan_payment", "credit-loans-reference", installment_id_value, "payment", ordinal
    )


def delinquency_case_id(loan_id_value: str, installment_id_value: str) -> str:
    return deterministic_id(
        "entity",
        "delinquency_case",
        "credit-loans-reference",
        loan_id_value,
        installment_id_value,
        "delinquency",
    )


def collection_case_id(delinquency_id: str) -> str:
    return deterministic_id(
        "entity", "loan_collection_case", "credit-loans-reference", delinquency_id, "collection"
    )


def restructure_id(loan_id_value: str, ordinal: int = 1) -> str:
    return deterministic_id(
        "entity", "loan_restructure", "credit-loans-reference", loan_id_value, "restructure", ordinal
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 842,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("loan_application", LoanApplicationChart))
    registry.register(EntityType("credit_decision", CreditDecisionChart))
    registry.register(EntityType("loan", LoanChart))
    registry.register(EntityType("loan_installment", InstallmentChart))
    registry.register(EntityType("loan_payment", PaymentChart))
    registry.register(EntityType("delinquency_case", DelinquencyCaseChart))
    registry.register(EntityType("loan_collection_case", CollectionCaseChart))
    registry.register(EntityType("loan_restructure", RestructureChart))
    return context, Engine(
        context=context,
        registry=registry,
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    principal: float = 1200.0,
    installment_count: int = 3,
) -> CreditLoanEntities:
    principal_cents = _usd_cents(principal)
    if principal_cents <= 0:
        raise ValueError("principal must be positive")
    if installment_count <= 0:
        raise ValueError("installment_count must be positive")
    context, _ = build_runtime(persistence, now=now)
    application = context.entities.create(
        LoanApplication,
        key=("credit-loans-reference", "application-1"),
        state="submitted",
        attributes={
            "principal": principal_cents / 100,
            "currency": "USD",
            "installment_count": installment_count,
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(application)
        uow.save_resource_definition(ResourceDefinition("credit_analyst", capacity=1))
        uow.save_resource_definition(ResourceDefinition("collection_agent", capacity=1))
    return CreditLoanEntities(application_id=application.id)


def _entity(persistence, entity_type, entity_id):
    value = persistence.entity(entity_type, entity_id)
    if value is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return value


def _dispatch(engine, entity, event, *, key, correlation_id):
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=correlation_id,
        key=key,
    )
    engine.dispatch(command)


def reconcile_underwriting(
    persistence,
    engine,
    backend,
    *,
    entities,
    approve: bool,
):
    application = _entity(persistence, "loan_application", entities.application_id)
    decision = persistence.entity("credit_decision", credit_decision_id(application.id))
    correlation_id = flow_correlation_id(application.id)
    request_id = f"credit-analyst:{application.id}"

    if application.state in {"approved", "rejected"}:
        expected_state = application.state
        expected_event = "approve" if expected_state == "approved" else "reject"
        if decision is None:
            decision = engine.context.entities.create(
                CreditDecision,
                key=("credit-loans-reference", application.id, "decision"),
                state="pending",
                attributes={"application_id": application.id},
            )
            with persistence.transaction() as uow:
                uow.save_entity(decision)
        decision = _entity(persistence, "credit_decision", decision.id)
        if decision.state == "pending":
            _dispatch(
                engine,
                decision,
                expected_event,
                key=("credit-decision", decision.id, expected_event),
                correlation_id=correlation_id,
            )
        engine.resources.withdraw(backend, request_id)
        return expected_state == "approved"

    if not engine.context.scenarios.attribute("credit_loans.underwriting.available", True):
        engine.resources.withdraw(backend, request_id)
        return False

    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="credit_analyst",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    application = _entity(persistence, "loan_application", application.id)
    if application.state == "submitted":
        _dispatch(
            engine,
            application,
            "start_analysis",
            key=("credit-loans", application.id, "start-analysis"),
            correlation_id=correlation_id,
        )

    if decision is None:
        decision = engine.context.entities.create(
            CreditDecision,
            key=("credit-loans-reference", application.id, "decision"),
            state="pending",
            attributes={"application_id": application.id},
        )
        with persistence.transaction() as uow:
            uow.save_entity(decision)

    application = _entity(persistence, "loan_application", application.id)
    decision = _entity(persistence, "credit_decision", decision.id)
    event = "approve" if approve else "reject"
    if decision.state == "pending":
        _dispatch(
            engine,
            decision,
            event,
            key=("credit-decision", decision.id, event),
            correlation_id=correlation_id,
        )
    if application.state == "under_analysis":
        _dispatch(
            engine,
            application,
            event,
            key=("credit-loans", application.id, event),
            correlation_id=correlation_id,
        )

    engine.resources.withdraw(backend, request_id)
    return _entity(persistence, "loan_application", application.id).state == "approved"


def ensure_loan(persistence, engine, *, entities):
    existing = persistence.entity("loan", loan_id(entities.application_id))
    if existing is not None:
        return existing
    application = _entity(persistence, "loan_application", entities.application_id)
    decision = _entity(persistence, "credit_decision", credit_decision_id(application.id))
    if application.state != "approved" or decision.state != "approved":
        raise RuntimeError("loan creation requires approved application and credit decision")
    value = engine.context.entities.create(
        Loan,
        key=("credit-loans-reference", application.id, "loan"),
        state="approved",
        attributes={
            "application_id": application.id,
            "principal": float(application.attributes["principal"]),
            "outstanding_balance": float(application.attributes["principal"]),
            "currency": application.attributes["currency"],
            "installment_count": int(application.attributes["installment_count"]),
            "applied_payment_ids": [],
            "delinquency_case_ids": [],
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(value)
    return value


def disburse_and_schedule(
    persistence,
    engine,
    backend,
    *,
    entities,
    first_due_delay: timedelta = FIRST_DUE_DELAY,
    installment_interval: timedelta = INSTALLMENT_INTERVAL,
):
    loan = ensure_loan(persistence, engine, entities=entities)
    correlation_id = flow_correlation_id(entities.application_id)
    if loan.state == "approved":
        _dispatch(
            engine,
            loan,
            "disburse",
            key=("credit-loans", loan.id, "disburse"),
            correlation_id=correlation_id,
        )
        loan = _entity(persistence, "loan", loan.id)
    if loan.state == "disbursed":
        _dispatch(
            engine,
            loan,
            "activate",
            key=("credit-loans", loan.id, "activate"),
            correlation_id=correlation_id,
        )

    count = int(loan.attributes["installment_count"])
    principal_cents = _usd_cents(float(loan.attributes["principal"]))
    base_cents = principal_cents // count
    due_dates = []
    for ordinal in range(1, count + 1):
        amount_cents = (
            base_cents
            if ordinal < count
            else principal_cents - base_cents * (count - 1)
        )
        amount = amount_cents / 100
        iid = installment_id(loan.id, ordinal)
        installment = persistence.entity("loan_installment", iid)
        if installment is None:
            installment = engine.context.entities.create(
                Installment,
                key=("credit-loans-reference", loan.id, "installment", ordinal),
                state="scheduled",
                attributes={
                    "loan_id": loan.id,
                    "ordinal": ordinal,
                    "amount": amount,
                    "paid_amount": 0.0,
                    "currency": loan.attributes["currency"],
                    "applied_payment_ids": [],
                },
            )
            with persistence.transaction() as uow:
                uow.save_entity(installment)
        existing = engine.scheduler.find_pending(
            entity_type="loan_installment",
            entity_id=installment.id,
            name="make_due",
        )
        if installment.state == "scheduled" and existing is None:
            due_at = backend.now + first_due_delay + installment_interval * (ordinal - 1)
            command = engine.context.commands.create(
                "make_due",
                target=installment,
                due_at=due_at,
                correlation_id=correlation_id,
                key=("credit-loans", installment.id, "make-due"),
            )
            engine.context.schedules.at(due_at, command=command)
        else:
            due_at = backend.now if existing is None else existing.work.due_at
        due_dates.append(due_at)
    return tuple(due_dates)


def schedule_overdue(persistence, engine, backend, *, installment_id_value, delay=OVERDUE_GRACE):
    installment = _entity(persistence, "loan_installment", installment_id_value)
    if installment.state not in {"due", "partially_paid"}:
        return backend.now
    existing = engine.scheduler.find_pending(
        entity_type="loan_installment",
        entity_id=installment.id,
        name="miss",
    )
    if existing is not None:
        return existing.work.due_at
    due_at = backend.now + delay
    command = engine.context.commands.create(
        "miss",
        target=installment,
        due_at=due_at,
        correlation_id=flow_correlation_id(
            str(_entity(persistence, "loan", installment.attributes["loan_id"]).attributes["application_id"])
        ),
        key=("credit-loans", installment.id, "miss"),
    )
    engine.context.schedules.at(due_at, command=command)
    return due_at


def post_payment(
    persistence,
    engine,
    *,
    installment_id_value,
    payment_ordinal: int,
    amount: float,
):
    amount_cents = _usd_cents(amount)
    if amount_cents <= 0:
        raise ValueError("payment amount must be positive")
    amount = amount_cents / 100
    installment = _entity(persistence, "loan_installment", installment_id_value)
    loan = _entity(persistence, "loan", installment.attributes["loan_id"])
    pid = payment_id(installment.id, payment_ordinal)
    payment = persistence.entity("loan_payment", pid)
    correlation_id = flow_correlation_id(str(loan.attributes["application_id"]))

    payable_states = {"due", "partially_paid", "overdue"}
    if payment is None and installment.state not in payable_states:
        raise RuntimeError(
            f"new payment requires payable installment, got {installment.state}"
        )
    if payment is not None and payment.state == "initiated" and installment.state not in payable_states:
        raise RuntimeError(
            f"initiated payment cannot post against {installment.state} installment"
        )

    if payment is None:
        remaining = float(installment.attributes["amount"]) - float(
            installment.attributes["paid_amount"]
        )
        if amount > remaining + 1e-9:
            raise ValueError("payment exceeds installment remaining balance")
        payment = engine.context.entities.create(
            Payment,
            key=("credit-loans-reference", installment.id, "payment", payment_ordinal),
            state="initiated",
            attributes={
                "loan_id": loan.id,
                "installment_id": installment.id,
                "ordinal": payment_ordinal,
                "amount": float(amount),
                "currency": loan.attributes["currency"],
            },
        )
        with persistence.transaction() as uow:
            uow.save_entity(payment)
        _dispatch(
            engine,
            payment,
            "post",
            key=("credit-payment", payment.id, "post"),
            correlation_id=correlation_id,
        )
        payment = _entity(persistence, "loan_payment", payment.id)
    else:
        if abs(float(payment.attributes["amount"]) - float(amount)) > 1e-9:
            raise ValueError("payment identity already exists with a different amount")
        if payment.state == "initiated":
            _dispatch(
                engine,
                payment,
                "post",
                key=("credit-payment", payment.id, "post"),
                correlation_id=correlation_id,
            )
            payment = _entity(persistence, "loan_payment", payment.id)
        if payment.state != "posted":
            raise RuntimeError(f"payment cannot be applied from {payment.state}")

    installment = _entity(persistence, "loan_installment", installment.id)
    loan = _entity(persistence, "loan", loan.id)
    applied = list(installment.attributes.get("applied_payment_ids", []))
    if payment.id not in applied:
        installment.attributes["paid_amount"] = round(
            float(installment.attributes["paid_amount"]) + float(payment.attributes["amount"]), 2
        )
        applied.append(payment.id)
        installment.attributes["applied_payment_ids"] = applied

        loan_applied = list(loan.attributes.get("applied_payment_ids", []))
        if payment.id not in loan_applied:
            loan.attributes["outstanding_balance"] = round(
                max(
                    0.0,
                    float(loan.attributes["outstanding_balance"])
                    - float(payment.attributes["amount"]),
                ),
                2,
            )
            loan_applied.append(payment.id)
            loan.attributes["applied_payment_ids"] = loan_applied
        replacement = None
        if installment.state == "restructured":
            restructure_id_value = installment.attributes.get(
                "superseded_by_restructure_id"
            )
            if restructure_id_value is not None:
                restructure = _entity(
                    persistence,
                    "loan_restructure",
                    str(restructure_id_value),
                )
                replacement_id = restructure.attributes.get(
                    "replacement_installment_id"
                )
                if replacement_id is not None:
                    replacement = _entity(
                        persistence,
                        "loan_installment",
                        str(replacement_id),
                    )
                    replacement.attributes["amount"] = round(
                        max(
                            0.0,
                            float(replacement.attributes["amount"])
                            - float(payment.attributes["amount"]),
                        ),
                        2,
                    )
        with persistence.transaction() as uow:
            uow.save_entity(installment)
            uow.save_entity(loan)
            if replacement is not None:
                uow.save_entity(replacement)

    installment = _entity(persistence, "loan_installment", installment.id)
    paid = float(installment.attributes["paid_amount"])
    required = float(installment.attributes["amount"])
    if paid + 1e-9 >= required:
        if installment.state in {"due", "partially_paid", "overdue"}:
            _dispatch(
                engine,
                installment,
                "complete",
                key=("credit-installment", installment.id, "complete"),
                correlation_id=correlation_id,
            )
        engine.scheduler.cancel_pending(
            entity_type="loan_installment",
            entity_id=installment.id,
            name="miss",
        )
    elif installment.state in {"due", "overdue"}:
        _dispatch(
            engine,
            installment,
            "record_partial",
            key=("credit-installment", installment.id, "partial", payment.id),
            correlation_id=correlation_id,
        )

    loan = _entity(persistence, "loan", loan.id)
    if float(loan.attributes["outstanding_balance"]) <= 1e-9 and loan.state in {
        "servicing",
        "delinquent",
    }:
        _dispatch(
            engine,
            loan,
            "pay_off",
            key=("credit-loans", loan.id, "pay-off"),
            correlation_id=correlation_id,
        )
    return _entity(persistence, "loan_payment", payment.id)


def ensure_delinquency(persistence, engine, *, installment_id_value):
    installment = _entity(persistence, "loan_installment", installment_id_value)
    if installment.state != "overdue":
        raise RuntimeError("delinquency requires overdue installment")
    loan = _entity(persistence, "loan", installment.attributes["loan_id"])
    correlation_id = flow_correlation_id(str(loan.attributes["application_id"]))
    did = delinquency_case_id(loan.id, installment.id)
    case = persistence.entity("delinquency_case", did)
    if case is None:
        case = engine.context.entities.create(
            DelinquencyCase,
            key=("credit-loans-reference", loan.id, installment.id, "delinquency"),
            state="opened",
            attributes={"loan_id": loan.id, "installment_id": installment.id},
        )
    case_ids = list(loan.attributes.get("delinquency_case_ids", []))
    if case.id not in case_ids:
        case_ids.append(case.id)
        loan.attributes["delinquency_case_ids"] = case_ids
        with persistence.transaction() as uow:
            uow.save_entity(case)
            uow.save_entity(loan)
    elif persistence.entity("delinquency_case", case.id) is None:
        with persistence.transaction() as uow:
            uow.save_entity(case)
    loan = _entity(persistence, "loan", loan.id)
    if loan.state == "servicing":
        _dispatch(
            engine,
            loan,
            "mark_delinquent",
            key=("credit-loans", loan.id, installment.id, "delinquent"),
            correlation_id=correlation_id,
        )
    return case


def reconcile_collection(
    persistence,
    engine,
    backend,
    *,
    installment_id_value,
    promise: bool = False,
):
    delinquency = ensure_delinquency(
        persistence, engine, installment_id_value=installment_id_value
    )
    loan = _entity(persistence, "loan", delinquency.attributes["loan_id"])
    correlation_id = flow_correlation_id(str(loan.attributes["application_id"]))
    cid = collection_case_id(delinquency.id)
    collection = persistence.entity("loan_collection_case", cid)
    if collection is None:
        collection = engine.context.entities.create(
            CollectionCase,
            key=("credit-loans-reference", delinquency.id, "collection"),
            state="opened",
            attributes={"delinquency_id": delinquency.id, "loan_id": loan.id},
        )
        with persistence.transaction() as uow:
            uow.save_entity(collection)

    request_id = f"collection-agent:{collection.id}"
    if not engine.context.scenarios.attribute("credit_loans.collection.available", True):
        engine.resources.withdraw(backend, request_id)
        return False
    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="collection_agent",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    delinquency = _entity(persistence, "delinquency_case", delinquency.id)
    if delinquency.state == "opened":
        _dispatch(
            engine,
            delinquency,
            "begin_collection",
            key=("credit-delinquency", delinquency.id, "begin-collection"),
            correlation_id=correlation_id,
        )
    collection = _entity(persistence, "loan_collection_case", collection.id)
    if collection.state == "opened":
        _dispatch(
            engine,
            collection,
            "assign",
            key=("credit-collection", collection.id, "assign"),
            correlation_id=correlation_id,
        )
        collection = _entity(persistence, "loan_collection_case", collection.id)
    if collection.state == "assigned":
        _dispatch(
            engine,
            collection,
            "contact",
            key=("credit-collection", collection.id, "contact"),
            correlation_id=correlation_id,
        )
        collection = _entity(persistence, "loan_collection_case", collection.id)
    if promise and collection.state == "contacted":
        _dispatch(
            engine,
            collection,
            "promise",
            key=("credit-collection", collection.id, "promise"),
            correlation_id=correlation_id,
        )
        collection = _entity(persistence, "loan_collection_case", collection.id)
        if engine.scheduler.find_pending(
            entity_type="loan_collection_case",
            entity_id=collection.id,
            name="escalate",
        ) is None:
            due_at = backend.now + COLLECTION_FOLLOWUP
            command = engine.context.commands.create(
                "escalate",
                target=collection,
                due_at=due_at,
                correlation_id=correlation_id,
                key=("credit-collection", collection.id, "escalate"),
            )
            engine.context.schedules.at(due_at, command=command)

    engine.resources.withdraw(backend, request_id)
    return True


def cure_delinquency(persistence, engine, *, installment_id_value):
    installment = _entity(persistence, "loan_installment", installment_id_value)
    if installment.state != "paid":
        raise RuntimeError("cure requires paid installment")
    loan = _entity(persistence, "loan", installment.attributes["loan_id"])
    did = delinquency_case_id(loan.id, installment.id)
    delinquency = _entity(persistence, "delinquency_case", did)
    collection = persistence.entity("loan_collection_case", collection_case_id(delinquency.id))
    correlation_id = flow_correlation_id(str(loan.attributes["application_id"]))

    if delinquency.state in {"opened", "collection"}:
        _dispatch(
            engine,
            delinquency,
            "cure",
            key=("credit-delinquency", delinquency.id, "cure"),
            correlation_id=correlation_id,
        )
    if collection is not None and collection.state in {"contacted", "promised", "escalated"}:
        _dispatch(
            engine,
            collection,
            "resolve",
            key=("credit-collection", collection.id, "resolve"),
            correlation_id=correlation_id,
        )
        engine.scheduler.cancel_pending(
            entity_type="loan_collection_case",
            entity_id=collection.id,
            name="escalate",
        )
    loan = _entity(persistence, "loan", loan.id)
    active_delinquencies = [
        _entity(persistence, "delinquency_case", case_id)
        for case_id in loan.attributes.get("delinquency_case_ids", [])
        if persistence.entity("delinquency_case", case_id) is not None
        and persistence.entity("delinquency_case", case_id).state
        in {"opened", "collection"}
    ]
    if loan.state == "delinquent" and not active_delinquencies:
        _dispatch(
            engine,
            loan,
            "cure",
            key=("credit-loans", loan.id, "cure"),
            correlation_id=correlation_id,
        )
    return True


def apply_restructure(
    persistence,
    engine,
    backend,
    *,
    installment_id_value,
    restructure_ordinal: int = 1,
):
    installment = _entity(persistence, "loan_installment", installment_id_value)
    loan = _entity(persistence, "loan", installment.attributes["loan_id"])
    correlation_id = flow_correlation_id(str(loan.attributes["application_id"]))
    rid = restructure_id(loan.id, restructure_ordinal)
    restructure = persistence.entity("loan_restructure", rid)
    if loan.state not in {"delinquent", "restructuring"} and not (
        loan.state == "servicing" and restructure is not None
    ):
        raise RuntimeError("restructure requires delinquent loan or existing restructure")
    if restructure is None:
        restructure = engine.context.entities.create(
            Restructure,
            key=("credit-loans-reference", loan.id, "restructure", restructure_ordinal),
            state="proposed",
            attributes={
                "loan_id": loan.id,
                "ordinal": restructure_ordinal,
                "amount": float(loan.attributes["outstanding_balance"]),
                "currency": loan.attributes["currency"],
            },
        )
        with persistence.transaction() as uow:
            uow.save_entity(restructure)
    if restructure.state == "proposed":
        _dispatch(
            engine,
            restructure,
            "accept",
            key=("credit-restructure", restructure.id, "accept"),
            correlation_id=correlation_id,
        )

    loan = _entity(persistence, "loan", loan.id)
    if loan.state == "delinquent":
        _dispatch(
            engine,
            loan,
            "begin_restructure",
            key=("credit-loans", loan.id, "begin-restructure"),
            correlation_id=correlation_id,
        )

    active = [
        item
        for item in (
            persistence.entity("loan_installment", installment_id(loan.id, ordinal))
            for ordinal in range(1, int(loan.attributes["installment_count"]) + 1)
        )
        if item is not None and item.state in {"scheduled", "due", "partially_paid", "overdue"}
    ]
    for item in active:
        item.attributes["superseded_by_restructure_id"] = restructure.id
        with persistence.transaction() as uow:
            uow.save_entity(item)
        _dispatch(
            engine,
            item,
            "restructure",
            key=("credit-installment", item.id, "restructure", restructure_ordinal),
            correlation_id=correlation_id,
        )
        engine.scheduler.cancel_pending(
            entity_type="loan_installment",
            entity_id=item.id,
            name="make_due",
        )
        engine.scheduler.cancel_pending(
            entity_type="loan_installment",
            entity_id=item.id,
            name="miss",
        )

    restructure = _entity(persistence, "loan_restructure", restructure.id)
    if restructure.state == "accepted":
        _dispatch(
            engine,
            restructure,
            "apply",
            key=("credit-restructure", restructure.id, "apply"),
            correlation_id=correlation_id,
        )

    new_ordinal = int(loan.attributes["installment_count"]) + restructure_ordinal
    replacement = persistence.entity("loan_installment", installment_id(loan.id, new_ordinal))
    if replacement is None:
        replacement = engine.context.entities.create(
            Installment,
            key=("credit-loans-reference", loan.id, "installment", new_ordinal),
            state="scheduled",
            attributes={
                "loan_id": loan.id,
                "ordinal": new_ordinal,
                "amount": float(loan.attributes["outstanding_balance"]),
                "paid_amount": 0.0,
                "currency": loan.attributes["currency"],
                "applied_payment_ids": [],
                "restructure_id": restructure.id,
            },
        )
        with persistence.transaction() as uow:
            uow.save_entity(replacement)
    restructure = _entity(persistence, "loan_restructure", restructure.id)
    if restructure.attributes.get("replacement_installment_id") != replacement.id:
        restructure.attributes["replacement_installment_id"] = replacement.id
        with persistence.transaction() as uow:
            uow.save_entity(restructure)
    if engine.scheduler.find_pending(
        entity_type="loan_installment",
        entity_id=replacement.id,
        name="make_due",
    ) is None and replacement.state == "scheduled":
        due_at = backend.now + FIRST_DUE_DELAY
        command = engine.context.commands.create(
            "make_due",
            target=replacement,
            due_at=due_at,
            correlation_id=correlation_id,
            key=("credit-loans", replacement.id, "restructured-due"),
        )
        engine.context.schedules.at(due_at, command=command)

    delinquency = _entity(
        persistence,
        "delinquency_case",
        delinquency_case_id(loan.id, installment.id),
    )
    if delinquency.state in {"opened", "collection"}:
        _dispatch(
            engine,
            delinquency,
            "restructure",
            key=("credit-delinquency", delinquency.id, "restructure"),
            correlation_id=correlation_id,
        )
    collection = persistence.entity("loan_collection_case", collection_case_id(delinquency.id))
    if collection is not None and collection.state in {"contacted", "promised", "escalated"}:
        _dispatch(
            engine,
            collection,
            "resolve",
            key=("credit-collection", collection.id, "restructure-resolve"),
            correlation_id=correlation_id,
        )
        engine.scheduler.cancel_pending(
            entity_type="loan_collection_case",
            entity_id=collection.id,
            name="escalate",
        )

    loan = _entity(persistence, "loan", loan.id)
    if loan.state == "restructuring":
        _dispatch(
            engine,
            loan,
            "resume_servicing",
            key=("credit-loans", loan.id, "resume-servicing"),
            correlation_id=correlation_id,
        )
    return replacement


def default_loan(persistence, engine, *, installment_id_value):
    installment = _entity(persistence, "loan_installment", installment_id_value)
    loan = _entity(persistence, "loan", installment.attributes["loan_id"])
    delinquency = _entity(
        persistence,
        "delinquency_case",
        delinquency_case_id(loan.id, installment.id),
    )
    if delinquency.state != "collection":
        raise RuntimeError("default requires active collection")
    collection = _entity(persistence, "loan_collection_case", collection_case_id(delinquency.id))
    if collection.state != "escalated":
        raise RuntimeError("default requires escalated collection")
    correlation_id = flow_correlation_id(str(loan.attributes["application_id"]))
    _dispatch(
        engine,
        delinquency,
        "default",
        key=("credit-delinquency", delinquency.id, "default"),
        correlation_id=correlation_id,
    )
    loan = _entity(persistence, "loan", loan.id)
    if loan.state == "delinquent":
        _dispatch(
            engine,
            loan,
            "default",
            key=("credit-loans", loan.id, "default"),
            correlation_id=correlation_id,
        )
    return True
