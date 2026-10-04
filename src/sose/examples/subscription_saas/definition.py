from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import SubscriptionSaaSConfig
from .simulation import (
    build_runtime,
    reconcile_plan_change,
    request_plan_change,
    seed_reference,
)

def _build(persistence: Persistence, config: SubscriptionSaaSConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: SubscriptionSaaSConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        customer_id=config.customer_id,
        initial_plan=config.initial_plan,
        term_duration=config.term_duration,
    )

def _subscription_or_error(persistence, entities):
    subscription = persistence.entity(
        "saas_subscription",
        entities.subscription_id,
    )
    if subscription is None:
        raise RuntimeError("configured subscription was not persisted")
    return subscription


def _effective_plan_change_at(*, config, backend):
    desired = config.start_at + config.plan_change_after
    if desired > backend.now:
        return desired
    return backend.now + config.tick_step


def _request_initial_plan_change(
    persistence,
    engine,
    backend,
    config,
    *,
    entities,
) -> None:
    request_plan_change(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        target_plan=config.target_plan,
        effective_at=_effective_plan_change_at(config=config, backend=backend),
    )


def _reconcile_tick(persistence, engine, backend, config, entities):
    if not config.auto_progress_plan_change:
        return
    subscription = _subscription_or_error(persistence, entities)
    if subscription.state != "active" or config.target_plan is None:
        return
    if subscription.attributes["plan_code"] == config.target_plan:
        return

    change_ids = [
        str(value)
        for value in subscription.attributes.get("change_request_ids", [])
    ]
    if not change_ids:
        _request_initial_plan_change(
            persistence,
            engine,
            backend,
            config,
            entities=entities,
        )
        return

    change = persistence.entity("saas_change_request", change_ids[-1])
    if change is not None and change.state == "applied":
        reconcile_plan_change(
            persistence,
            engine,
            entities=entities,
            change_id=change.id,
        )


definition = DomainDefinition(
    name="subscription_saas",
    description="Subscription and SaaS lifecycle reference domain.",
    config_model=SubscriptionSaaSConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
    runtime_mutable_fields=frozenset(["tick_step","random_seed","auto_progress_plan_change","target_plan","plan_change_after"]),
)
