"""Adversarial analytical contracts: invalid input must never yield plausible metrics.

These tests deliberately cover real rejection and boundary paths, rather than
changing coverage thresholds or excluding branches from measurement.
"""
from __future__ import annotations

import math
import pytest

from sose.core.randomness import CounterRandomSource
from sose.testing.analytical import (
    _solve_linear_system, bottleneck_throughput_bound,
    calendar_effective_capacity, jackson_visit_rates, kingman_mean_wait,
    little_law_wip, mm1_mean_system_time, mmc_mean_system_time,
    saturated_station_throughput, simulate_mm1,
)


@pytest.mark.parametrize(("throughput", "cycle_time"), [(-1., 2.), (1., -2.)])
def test_little_law_negative_inputs_rejected(throughput, cycle_time):
    with pytest.raises(ValueError, match="non-negative"):
        little_law_wip(throughput, cycle_time)


@pytest.mark.parametrize(("fn", "args", "error"), [
    (mm1_mean_system_time, (0.0, 3.0), "arrival_rate"),
    (mm1_mean_system_time, (1.0, 0.0), "service_rate"),
    (mmc_mean_system_time, (1.0, 2.0, 0), "servers"),
    (mmc_mean_system_time, (0.0, 2.0, 2), "arrival_rate"),
    (mmc_mean_system_time, (1.0, -1.0, 2), "service_rate"),
])
def test_queue_parameters_reject_invalid_capacity_and_arrivals(fn, args, error):
    with pytest.raises(ValueError, match=error):
        fn(*args)


@pytest.mark.parametrize(("kwargs", "reason"), [
    ({"external": (), "routing": ()}, "square"),
    ({"external": (1., 2.), "routing": ((0.,),)}, "square"),
    ({"external": (1.,), "routing": ((0.,),), "tolerance": 0.}, "tolerance"),
    ({"external": (1.,), "routing": ((0.,),), "max_iterations": 0}, "max_iterations"),
    ({"external": (-1.,), "routing": ((0.,),)}, "external"),
    ({"external": (1.,), "routing": ((-0.1,),)}, "probabilities"),
    ({"external": (1.,), "routing": ((1.1,),)}, "probabilities"),
    ({"external": (1., 1.), "routing": ((0.6, 0.6), (0., 0.))}, "outgoing"),
    ({"external": (1.,), "routing": ((1.,),)}, "singular"),
])
def test_jackson_invalid_conservation_inputs_are_rejected(kwargs, reason):
    with pytest.raises(ValueError, match=reason):
        jackson_visit_rates(**kwargs)


def test_jackson_null_rates_normalize_to_exact_zero():
    assert jackson_visit_rates(
        external=(0., 1.), routing=((0., 0.), (0., 0.))
    ) == (0., 1.)


@pytest.mark.parametrize(("capacities", "demands", "reason"), [
    ((), (), "equal non-zero"),
    ((1.,), (1., 2.), "equal non-zero"),
    ((0.,), (1.,), "capacities"),
    ((1.,), (-1.,), "service demands"),
])
def test_bottleneck_invalid_bounds_rejected(capacities, demands, reason):
    with pytest.raises(ValueError, match=reason):
        bottleneck_throughput_bound(capacities=capacities, service_demands=demands)


@pytest.mark.parametrize(("capacity", "mean_service_time", "name"), [
    (0., 1., "capacity"), (1., 0., "mean_service_time"),
])
def test_saturated_station_positive_domain(capacity, mean_service_time, name):
    with pytest.raises(ValueError, match=name):
        saturated_station_throughput(capacity=capacity, mean_service_time=mean_service_time)


@pytest.mark.parametrize(("nominal", "availability", "reason"), [
    (-1., 0.5, "nominal_capacity"),
    (1., -0.01, "availability_fraction"),
    (1., 1.01, "availability_fraction"),
])
def test_calendar_must_not_synthesize_negative_or_supernominal_capacity(nominal, availability, reason):
    with pytest.raises(ValueError, match=reason):
        calendar_effective_capacity(
            nominal_capacity=nominal, availability_fraction=availability,
        )


@pytest.mark.parametrize(("kwargs", "reason"), [
    ({"arrival_rate": 0., "mean_service_time": 1., "arrival_cv": 1., "service_cv": 1.}, "arrival_rate"),
    ({"arrival_rate": 0.5, "mean_service_time": 0., "arrival_cv": 1., "service_cv": 1.}, "mean_service_time"),
    ({"arrival_rate": 0.5, "mean_service_time": 1., "arrival_cv": -1., "service_cv": 1.}, "variation"),
    ({"arrival_rate": 0.5, "mean_service_time": 1., "arrival_cv": 1., "service_cv": -1.}, "variation"),
    ({"arrival_rate": 1., "mean_service_time": 1., "arrival_cv": 1., "service_cv": 1.}, "stable"),
])
def test_kingman_rejects_unstable_or_unphysical_queue(kwargs, reason):
    with pytest.raises(ValueError, match=reason):
        kingman_mean_wait(**kwargs)


@pytest.mark.parametrize(("arrival_rate", "service_rate", "customers", "warmup", "reason"), [
    (0., 1., 10, 0, "arrival_rate"),
    (1., 0., 10, 0, "service_rate"),
    (1., 1., 10, 0, "stable"),
    (0.5, 1., 1, 0, "two"),
    (0.5, 1., 10, -1, "warmup"),
    (0.5, 1., 10, 9, "warmup"),
])
def test_sampled_mm1_rejects_invalid_or_nonidentifiable_samples(
    arrival_rate, service_rate, customers, warmup, reason,
):
    with pytest.raises(ValueError, match=reason):
        simulate_mm1(
            arrival_rate=arrival_rate, service_rate=service_rate,
            customers=customers, warmup=warmup, rng=CounterRandomSource(42),
        )


def test_mm1_without_warmup_uses_initial_departure_horizon():
    observed = simulate_mm1(
        arrival_rate=0.3, service_rate=1.0, customers=4, warmup=0,
        rng=CounterRandomSource(42),
    )
    assert math.isfinite(observed.mean_system_time)
    assert observed.throughput > 0


def test_linear_solver_partial_pivot_and_zero_elimination_are_semantically_checked():
    # This numerically valid system requires swapping the first pivot row.
    assert _solve_linear_system([[0., 1.], [2., 1.]], [3., 7.], tolerance=1e-12) == pytest.approx([2., 3.])
    # Its lower-left entry is exactly zero, so elimination must not divide by it.
    assert _solve_linear_system([[2., 1.], [0., 2.]], [5., 6.], tolerance=1e-12) == pytest.approx([1., 3.])


def test_linear_solver_rejects_singular_late_pivot_without_false_rates():
    with pytest.raises(ValueError, match="singular"):
        _solve_linear_system([[1., 1.], [0., 0.]], [1., 0.], tolerance=1e-12)
