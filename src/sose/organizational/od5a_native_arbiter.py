"""Ephemeral native SimPy grant witness for the recurring OD-5a source step.

Split out of od5a_recurring, which re-exports both classes.
"""
from __future__ import annotations

from sose.backends.simpy import SimPyBackend
from sose.core.identity import deterministic_id
from sose.core.runtime import ResourceDefinition, ResourceReservation

from .od5a_durable import _time


class _PhysicalStepPreview:
    """Speculative reducer ledger: no authoritative mutations or SimPy stepping.

    The same reducer used by the committed runner predicts precisely which
    physical leases must be released and requested in the next action. This
    disposable view never writes source events or changes persisted state.
    """

    def __init__(self, reservations: tuple[ResourceReservation, ...]) -> None:
        self.reservations: dict[str, ResourceReservation] = {
            entry.reservation_id: entry for entry in reservations
        }
        self.releases: list[ResourceReservation] = []
        self.grants: list[ResourceReservation] = []

    def get_resource_reservation(self, reservation_id: str) -> ResourceReservation | None:
        return self.reservations.get(reservation_id)

    def resource_reservations(self) -> tuple[ResourceReservation, ...]:
        return tuple(self.reservations.values())

    def save_resource_reservation(self, reservation: ResourceReservation) -> None:
        if reservation.reservation_id in self.reservations:
            raise RuntimeError("speculative OD-5a duplicate physical reservation")
        self.reservations[reservation.reservation_id] = reservation
        self.grants.append(reservation)

    def delete_resource_reservation(self, reservation_id: str) -> None:
        try:
            released = self.reservations.pop(reservation_id)
        except KeyError as exc:
            raise RuntimeError("speculative OD-5a release has no durable lease") from exc
        self.releases.append(released)

    def append_event(self, event: object) -> None:
        # Event identity and causal links are verified by the real UoW, not
        # certified by this nonauthoritative physical scheduling preview.
        pass


class _RecurringNativePhysicalArbiter:
    """Native SimPy grant witness, reconstructed from a durable read snapshot.

    All SimPy event advancement occurs OUTSIDE persistence transactions.
    Inside the fenced UoW acquire/release only verify the already prepared
    physical identities; no foreign callback can execute under DB locks.
    """

    def __init__(
        self, backend: SimPyBackend, *,
        definitions: tuple[ResourceDefinition, ...],
        reservations: tuple[ResourceReservation, ...],
        resources: dict[str, str | None],
        job_id: str, due: float,
    ) -> None:
        self.backend = backend
        # (durable reservation ID, native instance lease ID, exact request ID)
        self.held: dict[str, tuple[str, str, str]] = {}
        self.expected: dict[str, str] = {}
        self.prepared_releases: dict[str, str] = {}
        # SOSE persists the *physical instances*. Derive ephemeral pooled
        # capacity from that immutable physical slot inventory, never from
        # an invented namespace-wide counter or shared organization clock.
        execution_slots = tuple(
            slot for slot in resources if slot.startswith("execution:")
        )
        if not execution_slots:
            raise ValueError("OD-5a requires at least one execution instance")
        self.execution_pool = f"{job_id}/execution-pool"
        self.pool_capacity = len(execution_slots)
        # reservation ID -> (native pool lease ID, stable pool request ID)
        self.pool_leases: dict[str, tuple[str, str]] = {}
        self.backend.create_resource(self.execution_pool, capacity=self.pool_capacity)
        if self.backend.now != _time(due):
            raise RuntimeError("OD-5a native recovery clock is not at source due boundary")
        definitions_by_name = {item.name: item for item in definitions}
        ledger_by_id = {item.reservation_id: item for item in reservations}
        for slot, reservation_id in resources.items():
            self._reconstruct_instance(
                f"{job_id}/{slot}", reservation_id,
                definitions_by_name, ledger_by_id, reservations,
            )

    def _reconstruct_instance(
        self, resource_name: str, reservation_id: str | None,
        definitions_by_name: dict[str, ResourceDefinition],
        ledger_by_id: dict[str, ResourceReservation],
        reservations: tuple[ResourceReservation, ...],
    ) -> None:
        definition = definitions_by_name.get(resource_name)
        if definition is None or definition.capacity != 1:
            raise ValueError(
                f"OD-5a native instance lacks authoritative unit capacity: {resource_name}"
            )
        self.backend.create_resource(resource_name, capacity=1)
        occupied = [r for r in reservations if r.resource_name == resource_name]
        if reservation_id is None:
            if occupied:
                raise RuntimeError(f"untracked foreign physical occupancy: {resource_name}")
            return
        saved = ledger_by_id.get(reservation_id)
        if saved is None or saved.resource_name != resource_name or len(occupied) != 1:
            raise RuntimeError(f"OD-5a physical lease contradicts ledger: {resource_name}")
        self.expected[resource_name] = reservation_id
        self._request(
            resource_name=resource_name, request_id=saved.request_id,
            reservation_id=reservation_id,
        )

    def _pooled(self, resource_name: str) -> bool:
        return resource_name.startswith(self.execution_pool.removesuffix("-pool") + ":")

    def _pool_lease_live(self, pool_lease: tuple[str, str] | None) -> bool:
        return pool_lease is not None and self.backend.resource_lease_active(
            pool_lease[0], resource_name=self.execution_pool,
            request_id=pool_lease[1],
        )

    def _request(self, *, resource_name: str, request_id: str, reservation_id: str) -> None:
        # A native pool with capacity N and a distinct per-instance native
        # resource must BOTH grant this request. The pool lease is a transient
        # projection of the durable per-instance reservation; no fake extra
        # ResourceReservation is saved for the pool.
        if resource_name.startswith(self.execution_pool.removesuffix("-pool") + ":"):
            pool_request_id = deterministic_id("od5a-native-pool-request", request_id)

            def pool_acquired(lease: object) -> None:
                snapshot = self.backend.resource_snapshot(self.execution_pool)
                if (
                    getattr(lease, "resource_name") != self.execution_pool
                    or getattr(lease, "request_id") != pool_request_id
                    or snapshot.in_use < 1
                    or snapshot.in_use > self.pool_capacity
                    or reservation_id in self.pool_leases
                ):
                    raise RuntimeError("OD-5a physical pool grant identity/capacity mismatch")
                self.pool_leases[reservation_id] = (
                    getattr(lease, "lease_id"), pool_request_id,
                )

            self.backend.request_resource(
                self.execution_pool,
                request_id=pool_request_id, on_acquired=pool_acquired,
            )

        def acquired(lease: object) -> None:
            if (
                getattr(lease, "resource_name") != resource_name
                or getattr(lease, "request_id") != request_id
                or self.backend.resource_snapshot(resource_name).in_use != 1
            ):
                raise RuntimeError("native SimPy lease identity or occupancy mismatch")
            if resource_name in self.held:
                raise RuntimeError("native SimPy allocated occupied physical instance")
            self.held[resource_name] = (
                reservation_id, getattr(lease, "lease_id"), request_id,
            )

        self.backend.request_resource(
            resource_name, request_id=request_id, on_acquired=acquired,
        )

    def prepare(
        self, *, releases: tuple[ResourceReservation, ...],
        grants: tuple[ResourceReservation, ...], at: float,
    ) -> None:
        """Process reconstructed holdings, release and next requests outside DB."""
        self.backend.run_until(_time(at))
        for resource_name, reservation_id in self.expected.items():
            self._verify_reconstructed(resource_name, reservation_id)

        for released in releases:
            self._release_native(released)

        for reservation in grants:
            if reservation.resource_name in self.held:
                raise RuntimeError(
                    f"native resource not released before new grant: {reservation.resource_name}"
                )
            self._request(
                resource_name=reservation.resource_name,
                request_id=reservation.request_id,
                reservation_id=reservation.reservation_id,
            )

        self.backend.run_until(_time(at))
        for reservation in grants:
            self._verify_granted(reservation)

    def _verify_reconstructed(self, resource_name: str, reservation_id: str) -> None:
        current = self.held.get(resource_name)
        if current is None or current[0] != reservation_id:
            raise RuntimeError(f"native lease reconstruction failed: {resource_name}")
        if not self.backend.resource_lease_active(
            current[1], resource_name=resource_name, request_id=current[2],
        ):
            raise RuntimeError("reconstructed physical instance native lease ended")
        if self._pooled(resource_name):
            if not self._pool_lease_live(self.pool_leases.get(reservation_id)):
                raise RuntimeError("native pool occupancy reconstruction failed")

    def _release_native(self, released: ResourceReservation) -> None:
        resource_name = released.resource_name
        current = self.held.get(resource_name)
        if current is None or current[0] != released.reservation_id:
            raise RuntimeError(f"native physical release was not reconstructed: {resource_name}")
        self.backend.release_resource(current[1])
        del self.held[resource_name]
        if self._pooled(resource_name):
            pool_lease = self.pool_leases.pop(released.reservation_id, None)
            if not self._pool_lease_live(pool_lease):
                raise RuntimeError("native execution pool release lacks live permit")
            self.backend.release_resource(pool_lease[0])
        self.prepared_releases[resource_name] = released.reservation_id

    def _verify_granted(self, reservation: ResourceReservation) -> None:
        current = self.held.get(reservation.resource_name)
        if current is None or current[0] != reservation.reservation_id:
            raise RuntimeError(
                f"native grant unavailable, durable wait not admitted: {reservation.resource_name}"
            )
        if self.backend.resource_snapshot(reservation.resource_name).in_use != 1:
            raise RuntimeError("native lease no longer occupies physical capacity")
        if not self.backend.resource_lease_active(
            current[1], resource_name=reservation.resource_name,
            request_id=reservation.request_id,
        ):
            raise RuntimeError("native instance lease ended before source commit")
        if self._pooled(reservation.resource_name):
            if not self._pool_lease_live(self.pool_leases.get(reservation.reservation_id)):
                raise RuntimeError(
                    "native execution pool did not hold matching live permit"
                )

    def acquire(self, *, resource_name: str, request_id: str, at: float) -> bool:
        # Fenced authoritative commit never advances SimPy or runs callbacks.
        if self.backend.now != _time(at):
            raise RuntimeError("native physical grant clock differs from source action")
        reservation_id = deterministic_id("od5a-durable-reservation", request_id)
        current = self.held.get(resource_name)
        instance_ok = (
            current is not None
            and current[0] == reservation_id
            and current[2] == request_id
            and self.backend.resource_lease_active(
                current[1], resource_name=resource_name, request_id=request_id,
            )
        )
        if not instance_ok:
            return False
        if not resource_name.startswith(self.execution_pool.removesuffix("-pool") + ":"):
            return True
        pool_lease = self.pool_leases.get(reservation_id)
        return (
            pool_lease is not None
            and pool_lease[1] == deterministic_id("od5a-native-pool-request", request_id)
            and self.backend.resource_lease_active(
                pool_lease[0], resource_name=self.execution_pool,
                request_id=pool_lease[1],
            )
        )

    def release(self, *, resource_name: str, reservation_id: str) -> None:
        # The real reducer's release must exactly match a preflight release.
        # Actual native release has already run outside the SQLite/PG UoW.
        prepared = self.prepared_releases.pop(resource_name, None)
        if prepared != reservation_id:
            raise RuntimeError("authoritative release has no matching native completion")

    def verify_committed_step(self) -> None:
        if self.prepared_releases:
            raise RuntimeError("planned native releases were not consumed by source step")
