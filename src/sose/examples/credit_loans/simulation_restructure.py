"""Credit-loan restructuring of delinquent loans (split from simulation.py)."""

from __future__ import annotations

from .entities import (
    Installment,
    Restructure,
)
from .simulation_runtime import (
    FIRST_DUE_DELAY,
    _dispatch,
    _entity,
    collection_case_id,
    delinquency_case_id,
    flow_correlation_id,
    installment_id,
    restructure_id,
)


def _ensure_restructure_record(
    persistence,
    engine,
    *,
    loan,
    restructure_ordinal: int,
):
    rid = restructure_id(loan.id, restructure_ordinal)
    restructure = persistence.entity("loan_restructure", rid)
    if restructure is not None:
        return restructure

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
    return restructure


def _active_installments_for_restructure(persistence, loan):
    return [
        item
        for item in (
            persistence.entity("loan_installment", installment_id(loan.id, ordinal))
            for ordinal in range(1, int(loan.attributes["installment_count"]) + 1)
        )
        if item is not None and item.state in {"scheduled", "due", "partially_paid", "overdue"}
    ]


def _mark_installments_restructured(
    persistence,
    engine,
    *,
    installments,
    restructure,
    restructure_ordinal: int,
    correlation_id: str,
) -> None:
    for item in installments:
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


def _ensure_replacement_installment(
    persistence,
    engine,
    backend,
    *,
    loan,
    restructure,
    restructure_ordinal: int,
    correlation_id: str,
):
    new_ordinal = int(loan.attributes["installment_count"]) + restructure_ordinal
    replacement = persistence.entity(
        "loan_installment",
        installment_id(loan.id, new_ordinal),
    )
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

    has_due = engine.scheduler.find_pending(
        entity_type="loan_installment",
        entity_id=replacement.id,
        name="make_due",
    )
    if has_due is None and replacement.state == "scheduled":
        due_at = backend.now + FIRST_DUE_DELAY
        command = engine.context.commands.create(
            "make_due",
            target=replacement,
            due_at=due_at,
            correlation_id=correlation_id,
            key=("credit-loans", replacement.id, "restructured-due"),
        )
        engine.context.schedules.at(due_at, command=command)
    return replacement


def _resolve_restructure_collection(
    persistence,
    engine,
    *,
    delinquency,
    correlation_id: str,
) -> None:
    if delinquency.state in {"opened", "collection"}:
        _dispatch(
            engine,
            delinquency,
            "restructure",
            key=("credit-delinquency", delinquency.id, "restructure"),
            correlation_id=correlation_id,
        )
    collection = persistence.entity("loan_collection_case", collection_case_id(delinquency.id))
    if collection is None or collection.state not in {"contacted", "promised", "escalated"}:
        return
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
    restructure = persistence.entity(
        "loan_restructure",
        restructure_id(loan.id, restructure_ordinal),
    )
    if loan.state not in {"delinquent", "restructuring"} and not (
        loan.state == "servicing" and restructure is not None
    ):
        raise RuntimeError("restructure requires delinquent loan or existing restructure")
    restructure = _ensure_restructure_record(
        persistence,
        engine,
        loan=loan,
        restructure_ordinal=restructure_ordinal,
    )
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

    active = _active_installments_for_restructure(persistence, loan)
    _mark_installments_restructured(
        persistence,
        engine,
        installments=active,
        restructure=restructure,
        restructure_ordinal=restructure_ordinal,
        correlation_id=correlation_id,
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

    replacement = _ensure_replacement_installment(
        persistence,
        engine,
        backend,
        loan=loan,
        restructure=restructure,
        restructure_ordinal=restructure_ordinal,
        correlation_id=correlation_id,
    )

    delinquency = _entity(
        persistence,
        "delinquency_case",
        delinquency_case_id(loan.id, installment.id),
    )
    _resolve_restructure_collection(
        persistence,
        engine,
        delinquency=delinquency,
        correlation_id=correlation_id,
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
