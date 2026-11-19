"""Credit-loan delinquency, collection, cure and default (split from simulation.py)."""

from __future__ import annotations

from .entities import (
    CollectionCase,
    DelinquencyCase,
)
from .simulation_runtime import (
    COLLECTION_FOLLOWUP,
    _dispatch,
    _entity,
    collection_case_id,
    delinquency_case_id,
    flow_correlation_id,
)


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
