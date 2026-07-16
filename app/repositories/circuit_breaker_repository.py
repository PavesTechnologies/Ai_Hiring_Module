from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.config import CBState, CircuitBreakerState

_OPEN_COOLDOWN = timedelta(minutes=5)


class CircuitBreakerRepository:
    """
    CRUD for circuit_breaker_state. No HALF_OPEN probing logic — this is
    the minimal Phase 11 scope (record failures, flip to OPEN past
    threshold), not a full circuit-breaker state machine.
    """

    def __init__(self, db: Session):
        self.db = db

    def get_by_service_name(self, service_name: str) -> CircuitBreakerState | None:
        return (
            self.db.query(CircuitBreakerState)
            .filter(CircuitBreakerState.service_name == service_name)
            .first()
        )

    def get_or_create(
        self,
        service_name: str,
        failure_threshold: int = 10,
    ) -> CircuitBreakerState:
        state = self.get_by_service_name(service_name)
        if state is None:
            state = CircuitBreakerState(
                service_name=service_name,
                state=CBState.CLOSED,
                failure_count=0,
                failure_threshold=failure_threshold,
            )
            self.db.add(state)
            self.db.flush()
            self.db.refresh(state)
        return state

    def increment_failure(self, service_name: str) -> tuple[CircuitBreakerState, bool]:
        """
        Increments the failure count for service_name (creating a CLOSED
        row on first failure if none exists), flipping to OPEN once
        failure_count reaches failure_threshold. Returns (state,
        just_opened) — just_opened is True only on the call that actually
        caused the CLOSED -> OPEN transition, so the caller can decide
        whether to audit-log it exactly once.
        """
        state = self.get_or_create(service_name)
        was_open = state.state == CBState.OPEN

        state.failure_count += 1
        state.last_failure_at = datetime.now(timezone.utc)

        just_opened = False
        if not was_open and state.failure_count >= state.failure_threshold:
            state.state = CBState.OPEN
            state.opened_at = datetime.now(timezone.utc)
            state.retry_after = state.opened_at + _OPEN_COOLDOWN
            just_opened = True

        self.db.flush()
        self.db.refresh(state)
        return state, just_opened

    def reset(self, service_name: str) -> CircuitBreakerState | None:
        state = self.get_by_service_name(service_name)
        if state is None:
            return None
        state.state = CBState.CLOSED
        state.failure_count = 0
        state.opened_at = None
        state.retry_after = None
        self.db.flush()
        self.db.refresh(state)
        return state

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
