from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.config import CBState, CircuitBreakerState

_OPEN_COOLDOWN = timedelta(minutes=5)


class CircuitBreakerRepository:

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

    def transition_to_half_open_if_due(self, service_name: str) -> CircuitBreakerState:
       
        state = self.get_or_create(service_name)
        if (
            state.state == CBState.OPEN
            and state.retry_after is not None
            and datetime.now(timezone.utc) >= state.retry_after
        ):
            state.state = CBState.HALF_OPEN
            self.db.flush()
            self.db.refresh(state)
        return state

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
