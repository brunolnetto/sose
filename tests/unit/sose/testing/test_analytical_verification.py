from __future__ import annotations

import math

import pytest

from sose.core.randomness import CounterRandomSource
from sose.testing.analytical import (
    bottleneck_throughput_bound,
    calendar_effective_capacity,
    jackson_visit_rates,
    kingman_mean_wait,
    little_law_wip,
    mm1_mean_system_time,
    mmc_mean_system_time,
    rework_visit_ratio,
    saturated_station_throughput,
    simulate_mm1,
)


def test_little_law() -> None:
    assert little_law_wip(throughput=4.0, cycle_time=2.5) == pytest.approx(10.0)


def test_mm1_closed_form() -> None:
    assert mm1_mean_system_time(arrival_rate=2.0, service_rate=3.0) == pytest.approx(1.0)


def test_mmc_reduces_to_mm1_for_one_server() -> None:
    assert mmc_mean_system_time(arrival_rate=2.0, service_rate=3.0, servers=1) == pytest.approx(
        mm1_mean_system_time(arrival_rate=2.0, service_rate=3.0)
    )


def test_mmc_more_servers_reduce_delay() -> None:
    one = mmc_mean_system_time(arrival_rate=0.8, service_rate=1.0, servers=1)
    two = mmc_mean_system_time(arrival_rate=0.8, service_rate=1.0, servers=2)
    assert two < one


def test_mmc_is_numerically_stable_for_large_server_counts() -> None:
    result = mmc_mean_system_time(arrival_rate=100.0, service_rate=1.0, servers=200)
    assert math.isfinite(result)
    assert result >= 1.0
    assert result < 1.01


def test_jackson_visit_rates_solve_flow_balance() -> None:
    rates = jackson_visit_rates(
        external=(1.0, 0.0),
        routing=((0.0, 0.2), (0.5, 0.0)),
    )
    assert rates[0] == pytest.approx(1.1111111111, rel=1e-8)
    assert rates[1] == pytest.approx(0.2222222222, rel=1e-8)


def test_jackson_visit_rates_are_scale_aware_near_closed_loop() -> None:
    rates = jackson_visit_rates(
        external=(1e-12,),
        routing=((0.999999,),),
    )
    assert rates == pytest.approx((1e-6,), rel=1e-8)


def test_operational_bottleneck_bound() -> None:
    assert bottleneck_throughput_bound(
        capacities=(2.0, 1.0),
        service_demands=(0.5, 0.4),
    ) == pytest.approx(2.5)


def test_rework_visit_ratio() -> None:
    assert rework_visit_ratio(0.2) == pytest.approx(1.25)


def test_saturated_capacity_scaling() -> None:
    assert saturated_station_throughput(capacity=4, mean_service_time=2.0) == pytest.approx(2.0)


def test_calendar_effective_capacity() -> None:
    assert calendar_effective_capacity(nominal_capacity=8.0, availability_fraction=0.75) == pytest.approx(6.0)


def test_kingman_matches_mm1_wait_for_exponential_variability() -> None:
    arrival_rate = 0.7
    service_rate = 1.0
    mean_service = 1 / service_rate
    kingman = kingman_mean_wait(
        arrival_rate=arrival_rate,
        mean_service_time=mean_service,
        arrival_cv=1.0,
        service_cv=1.0,
    )
    mm1_wait = mm1_mean_system_time(arrival_rate, service_rate) - mean_service
    assert kingman == pytest.approx(mm1_wait)


def test_counter_based_mm1_simulation_converges_to_closed_form() -> None:
    arrival_rate = 0.6
    service_rate = 1.0
    sample = simulate_mm1(
        arrival_rate=arrival_rate,
        service_rate=service_rate,
        customers=30_000,
        warmup=3_000,
        rng=CounterRandomSource(20261005),
    )
    expected = mm1_mean_system_time(arrival_rate, service_rate)
    assert sample.mean_system_time == pytest.approx(expected, rel=0.06)
    assert sample.throughput == pytest.approx(arrival_rate, rel=0.04)


def test_mm1_throughput_uses_matching_departure_boundary_after_warmup() -> None:
    seed = 271828
    arrival_rate = 0.95
    service_rate = 1.0
    customers = 1002
    warmup = 1000
    rng = CounterRandomSource(seed)

    departure = 0.0
    arrival = 0.0
    departures: list[float] = []
    for index in range(customers):
        u_arrival = rng.uniform(stream="arrival", entity_id=index, mechanism="mm1_interarrival")
        u_service = rng.uniform(stream="service_time", entity_id=index, mechanism="mm1_service")
        arrival += -math.log1p(-u_arrival) / arrival_rate
        service = -math.log1p(-u_service) / service_rate
        departure = max(arrival, departure) + service
        departures.append(departure)

    expected = (customers - warmup) / (departures[-1] - departures[warmup - 1])
    sample = simulate_mm1(
        arrival_rate=arrival_rate,
        service_rate=service_rate,
        customers=customers,
        warmup=warmup,
        rng=CounterRandomSource(seed),
    )
    assert sample.throughput == pytest.approx(expected)


def test_unstable_or_invalid_queueing_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="stable"):
        mm1_mean_system_time(arrival_rate=1.0, service_rate=1.0)
    with pytest.raises(ValueError, match="stable"):
        mmc_mean_system_time(arrival_rate=2.0, service_rate=1.0, servers=2)
    with pytest.raises(ValueError, match="rework probability"):
        rework_visit_ratio(1.0)
