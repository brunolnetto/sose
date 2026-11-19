from __future__ import annotations

from dataclasses import dataclass
from math import log1p

from sose.core.randomness import CounterRandomSource
# Re-exported: the Jackson network solver lives in sose.testing._jackson.
from sose.testing._jackson import _solve_linear_system as _solve_linear_system
from sose.testing._jackson import jackson_visit_rates as jackson_visit_rates


def little_law_wip(throughput: float, cycle_time: float) -> float:
    if throughput < 0 or cycle_time < 0:
        raise ValueError("throughput and cycle_time must be non-negative")
    return throughput * cycle_time


def mm1_mean_system_time(arrival_rate: float, service_rate: float) -> float:
    _require_positive(arrival_rate, "arrival_rate")
    _require_positive(service_rate, "service_rate")
    if arrival_rate >= service_rate:
        raise ValueError("M/M/1 requires a stable queue: arrival_rate < service_rate")
    return 1.0 / (service_rate - arrival_rate)


def mmc_mean_system_time(arrival_rate: float, service_rate: float, servers: int) -> float:
    _require_positive(arrival_rate, "arrival_rate")
    _require_positive(service_rate, "service_rate")
    if servers < 1:
        raise ValueError("servers must be at least one")
    if arrival_rate >= servers * service_rate:
        raise ValueError("M/M/c requires a stable queue: arrival_rate < servers * service_rate")

    offered_load = arrival_rate / service_rate
    utilization = offered_load / servers
    erlang_b = 1.0
    for capacity in range(1, servers + 1):
        erlang_b = (offered_load * erlang_b) / (capacity + offered_load * erlang_b)
    probability_wait = erlang_b / (1.0 - utilization + utilization * erlang_b)
    mean_wait = probability_wait / (servers * service_rate - arrival_rate)
    return mean_wait + 1.0 / service_rate


def bottleneck_throughput_bound(
    *,
    capacities: tuple[float, ...],
    service_demands: tuple[float, ...],
) -> float:
    if not capacities or len(capacities) != len(service_demands):
        raise ValueError("capacities and service_demands must have equal non-zero length")
    if any(capacity <= 0 for capacity in capacities):
        raise ValueError("capacities must be positive")
    if any(demand <= 0 for demand in service_demands):
        raise ValueError("service demands must be positive")
    return min(capacity / demand for capacity, demand in zip(capacities, service_demands, strict=True))


def rework_visit_ratio(rework_probability: float) -> float:
    if not 0.0 <= rework_probability < 1.0:
        raise ValueError("rework probability must satisfy 0 <= p < 1")
    return 1.0 / (1.0 - rework_probability)


def saturated_station_throughput(*, capacity: float, mean_service_time: float) -> float:
    _require_positive(capacity, "capacity")
    _require_positive(mean_service_time, "mean_service_time")
    return capacity / mean_service_time


def calendar_effective_capacity(*, nominal_capacity: float, availability_fraction: float) -> float:
    if nominal_capacity < 0:
        raise ValueError("nominal_capacity must be non-negative")
    if not 0.0 <= availability_fraction <= 1.0:
        raise ValueError("availability_fraction must be between zero and one")
    return nominal_capacity * availability_fraction


def kingman_mean_wait(
    *,
    arrival_rate: float,
    mean_service_time: float,
    arrival_cv: float,
    service_cv: float,
) -> float:
    _require_positive(arrival_rate, "arrival_rate")
    _require_positive(mean_service_time, "mean_service_time")
    if arrival_cv < 0 or service_cv < 0:
        raise ValueError("coefficients of variation must be non-negative")
    utilization = arrival_rate * mean_service_time
    if utilization >= 1.0:
        raise ValueError("Kingman approximation requires a stable queue")
    variability = (arrival_cv**2 + service_cv**2) / 2.0
    return (utilization / (1.0 - utilization)) * variability * mean_service_time


@dataclass(frozen=True, slots=True)
class MM1Sample:
    mean_system_time: float
    throughput: float


def simulate_mm1(
    *,
    arrival_rate: float,
    service_rate: float,
    customers: int,
    warmup: int,
    rng: CounterRandomSource,
) -> MM1Sample:
    _require_positive(arrival_rate, "arrival_rate")
    _require_positive(service_rate, "service_rate")
    if arrival_rate >= service_rate:
        raise ValueError("M/M/1 requires a stable queue")
    if customers < 2:
        raise ValueError("customers must be at least two")
    if warmup < 0 or warmup >= customers - 1:
        raise ValueError("warmup must leave at least two observed customers")

    arrivals: list[float] = []
    departures: list[float] = []
    arrival = 0.0
    departure = 0.0
    for index in range(customers):
        interarrival = _exponential(
            rng.uniform(stream="arrival", entity_id=index, mechanism="mm1_interarrival"),
            arrival_rate,
        )
        service = _exponential(
            rng.uniform(stream="service_time", entity_id=index, mechanism="mm1_service"),
            service_rate,
        )
        arrival += interarrival
        departure = max(arrival, departure) + service
        arrivals.append(arrival)
        departures.append(departure)

    observed_arrivals = arrivals[warmup:]
    observed_departures = departures[warmup:]
    mean_system_time = sum(
        departed - arrived
        for arrived, departed in zip(observed_arrivals, observed_departures, strict=True)
    ) / len(observed_arrivals)

    if warmup == 0:
        throughput_horizon = departures[-1]
        completion_count = customers
    else:
        throughput_horizon = departures[-1] - departures[warmup - 1]
        completion_count = customers - warmup
    throughput = completion_count / throughput_horizon
    return MM1Sample(mean_system_time=mean_system_time, throughput=throughput)


def _exponential(uniform: float, rate: float) -> float:
    return -log1p(-uniform) / rate


def _require_positive(value: float, name: str) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")
