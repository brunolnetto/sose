from __future__ import annotations

from dataclasses import dataclass
from math import log1p

from sose.core.randomness import CounterRandomSource


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


def jackson_visit_rates(
    *,
    external: tuple[float, ...],
    routing: tuple[tuple[float, ...], ...],
    tolerance: float = 1e-12,
    max_iterations: int = 100_000,
) -> tuple[float, ...]:
    size = len(external)
    if size == 0 or len(routing) != size or any(len(row) != size for row in routing):
        raise ValueError("routing matrix must be square and match external rates")
    if tolerance <= 0:
        raise ValueError("tolerance must be positive")
    if max_iterations < 1:
        raise ValueError("max_iterations must be at least one")
    if any(rate < 0 for rate in external):
        raise ValueError("external rates must be non-negative")
    if any(probability < 0 or probability > 1 for row in routing for probability in row):
        raise ValueError("routing probabilities must be between zero and one")
    if any(sum(row) > 1.0 + tolerance for row in routing):
        raise ValueError("outgoing routing probabilities cannot exceed one")

    matrix = [
        [
            (1.0 if row == column else 0.0) - routing[column][row]
            for column in range(size)
        ]
        for row in range(size)
    ]
    rates = _solve_linear_system(matrix, list(external), tolerance=tolerance)
    if any(rate < -tolerance for rate in rates):
        raise ValueError("Jackson traffic equations produced negative visit rates")
    normalized = tuple(0.0 if abs(rate) <= tolerance else rate for rate in rates)

    residual = max(
        abs(
            normalized[j]
            - external[j]
            - sum(normalized[i] * routing[i][j] for i in range(size))
        )
        for j in range(size)
    )
    scale = max(1.0, *(abs(rate) for rate in normalized), *(abs(rate) for rate in external))
    if residual > tolerance * scale:
        raise ValueError("Jackson traffic equations failed residual verification")
    return normalized


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


def _solve_linear_system(
    matrix: list[list[float]],
    vector: list[float],
    *,
    tolerance: float,
) -> list[float]:
    size = len(vector)
    augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]

    for column in range(size):
        pivot_row = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        pivot = augmented[pivot_row][column]
        row_scale = max(1.0, *(abs(value) for value in augmented[pivot_row][:-1]))
        if abs(pivot) <= tolerance * row_scale:
            raise ValueError("Jackson traffic equations are singular or ill-conditioned")
        if pivot_row != column:
            augmented[column], augmented[pivot_row] = augmented[pivot_row], augmented[column]

        pivot = augmented[column][column]
        for row in range(column + 1, size):
            factor = augmented[row][column] / pivot
            if factor == 0.0:
                continue
            for index in range(column, size + 1):
                augmented[row][index] -= factor * augmented[column][index]

    result = [0.0] * size
    for row in range(size - 1, -1, -1):
        rhs = augmented[row][size] - sum(
            augmented[row][column] * result[column]
            for column in range(row + 1, size)
        )
        pivot = augmented[row][row]
        if abs(pivot) <= tolerance:
            raise ValueError("Jackson traffic equations are singular or ill-conditioned")
        result[row] = rhs / pivot
    return result


def _exponential(uniform: float, rate: float) -> float:
    return -log1p(-uniform) / rate


def _require_positive(value: float, name: str) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")
