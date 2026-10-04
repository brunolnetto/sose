from sose.examples.subscription_saas.simulation import run_happy_path


def test_future_plan_change_replaces_entitlement_without_replacing_subscription():
    persistence, entities = run_happy_path()

    subscription = persistence.entity("saas_subscription", entities.subscription_id)
    assert subscription is not None and subscription.state == "active"
    assert subscription.attributes["plan_code"] == "pro"
    assert len(subscription.attributes["entitlement_ids"]) == 2
    assert len(subscription.attributes["occurrence_ids"]) == 1

    old_entitlement = persistence.entity(
        "saas_entitlement",
        subscription.attributes["entitlement_ids"][0],
    )
    new_entitlement = persistence.entity(
        "saas_entitlement",
        subscription.attributes["active_entitlement_id"],
    )
    occurrence = persistence.entity(
        "saas_subscription_occurrence",
        subscription.attributes["occurrence_ids"][0],
    )

    assert old_entitlement is not None and old_entitlement.state == "superseded"
    assert new_entitlement is not None and new_entitlement.state == "active"
    assert new_entitlement.attributes["plan_code"] == "pro"
    assert occurrence is not None and occurrence.state == "committed"
    assert occurrence.attributes["from_plan"] == "basic"
    assert occurrence.attributes["to_plan"] == "pro"
