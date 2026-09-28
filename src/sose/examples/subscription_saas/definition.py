from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import SubscriptionSaaSConfig
from .simulation import build_runtime, seed_reference

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

definition = DomainDefinition(
    name="subscription_saas",
    description="Subscription and SaaS lifecycle reference domain.",
    config_model=SubscriptionSaaSConfig,
    build_runtime=_build,
    seed=_seed,
)
