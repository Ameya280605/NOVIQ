from dataclasses import dataclass
from typing import Callable, TypeVar


@dataclass(frozen=True)
class DomainEvent:
    tenant_id: int

    def __post_init__(self) -> None:
        if type(self.tenant_id) is not int or self.tenant_id < 1:
            raise ValueError("tenant_id must be a positive integer")


@dataclass(frozen=True)
class TaskAssigned(DomainEvent):
    task_id: int
    assigned_to: int
    assigned_by: int


@dataclass(frozen=True)
class TaskStatusChanged(DomainEvent):
    task_id: int
    previous_status: str
    new_status: str
    changed_by: int


@dataclass(frozen=True)
class ProjectStatusChanged(DomainEvent):
    project_id: int
    previous_status: str
    new_status: str
    changed_by: int


@dataclass(frozen=True)
class DocumentUploaded(DomainEvent):
    document_id: int
    project_id: int
    uploaded_by: int


@dataclass(frozen=True)
class DocumentDeleted(DomainEvent):
    document_id: int
    project_id: int
    deleted_by: int


EventType = TypeVar("EventType", bound=DomainEvent)
EventHandler = Callable[..., None]
RegisteredHandler = tuple[
    type[DomainEvent], int | None, EventHandler, bool
]


class EventPublisher:
    def __init__(self) -> None:
        self._handlers: list[RegisteredHandler] = []

    def register(
        self,
        event_type: type[EventType],
        handler: EventHandler,
        *,
        tenant_id: int | None,
        uses_db: bool = False,
    ) -> Callable[[], None]:
        if tenant_id is not None and (
            type(tenant_id) is not int or tenant_id < 1
        ):
            raise ValueError("tenant_id must be a positive integer")
        registration: RegisteredHandler = (
            event_type,
            tenant_id,
            handler,  # type: ignore[assignment]
            uses_db,
        )
        if registration in self._handlers:
            return lambda: self._handlers.remove(registration)
        self._handlers.append(registration)

        def unregister() -> None:
            if registration in self._handlers:
                self._handlers.remove(registration)

        return unregister

    def publish(
        self,
        event: DomainEvent,
        *,
        db: object | None = None,
        event_id: int | None = None,
    ) -> int:
        handled_count = 0
        for event_type, tenant_id, handler, uses_db in tuple(self._handlers):
            if (
                tenant_id is not None
                and tenant_id != event.tenant_id
            ) or not isinstance(event, event_type):
                continue
            if uses_db:
                if db is None or event_id is None:
                    raise ValueError(
                        "Database-aware handlers require db and event_id"
                    )
                handler(event, db, event_id)
            else:
                handler(event)
            handled_count += 1
        return handled_count

    def clear(self) -> None:
        self._handlers.clear()


event_publisher = EventPublisher()
