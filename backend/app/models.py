from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    users: Mapped[list["User"]] = relationship(back_populates="tenant")

    projects: Mapped[list["Project"]] = relationship(back_populates="tenant")

    documents: Mapped[list["Document"]] = relationship(
        back_populates="tenant", foreign_keys="Document.tenant_id"
    )

    notifications: Mapped[list["Notification"]] = relationship(
        back_populates="tenant", foreign_keys="Notification.tenant_id"
    )

    outbox_events: Mapped[list["OutboxEvent"]] = relationship(
        back_populates="tenant"
    )


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("uq_users_tenant_id_id", "tenant_id", "id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)

    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[str] = mapped_column(String(50), default="member", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="users")

    assigned_tasks: Mapped[list["Task"]] = relationship(back_populates="assignee")

    managed_projects: Mapped[list["Project"]] = relationship(
        back_populates="project_manager",
        foreign_keys="Project.project_manager_id",
    )

    uploaded_documents: Mapped[list["Document"]] = relationship(
        back_populates="uploader", foreign_keys="Document.uploaded_by"
    )

    received_notifications: Mapped[list["Notification"]] = relationship(
        back_populates="recipient", foreign_keys="Notification.recipient_id"
    )


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(
            "status IN ('planned', 'active', 'on_hold', 'completed', 'cancelled')",
            name="ck_projects_status",
        ),
        Index("ix_projects_tenant_status", "tenant_id", "status"),
        Index("ix_projects_tenant_manager", "tenant_id", "project_manager_id"),
        Index("uq_projects_tenant_id_id", "tenant_id", "id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    status: Mapped[str] = mapped_column(
        String(50), default="planned", nullable=False
    )

    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    target_deadline: Mapped[date | None] = mapped_column(
        Date, nullable=True
    )

    completion_date: Mapped[date | None] = mapped_column(
        Date, nullable=True
    )

    project_manager_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="projects")

    tasks: Mapped[list["Task"]] = relationship(back_populates="project")

    project_manager: Mapped["User | None"] = relationship(
        back_populates="managed_projects",
        foreign_keys=[project_manager_id],
    )

    documents: Mapped[list["Document"]] = relationship(
        back_populates="project",
        foreign_keys="Document.project_id",
        overlaps="tenant,documents",
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_documents_tenant_project",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "uploaded_by"],
            ["users.tenant_id", "users.id"],
            name="fk_documents_tenant_uploader",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "deletion_requested_by"],
            ["users.tenant_id", "users.id"],
            name="fk_documents_tenant_deletion_requester",
        ),
        CheckConstraint(
            "status IN ('pending_upload', 'available', 'deletion_pending', 'failed', 'deleted')",
            name="ck_documents_status",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_documents_size_bytes"),
        UniqueConstraint("storage_key", name="uq_documents_storage_key"),
        Index("ix_documents_tenant_project", "tenant_id", "project_id"),
        Index("ix_documents_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )

    project_id: Mapped[int] = mapped_column(nullable=False)

    uploaded_by: Mapped[int] = mapped_column(nullable=False)

    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)

    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)

    content_type: Mapped[str] = mapped_column(String(255), nullable=False)

    size_bytes: Mapped[int] = mapped_column(nullable=False)

    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)

    status: Mapped[str] = mapped_column(
        String(50), default="pending_upload", nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    deletion_requested_by: Mapped[int | None] = mapped_column(nullable=True)

    tenant: Mapped["Tenant"] = relationship(
        back_populates="documents",
        foreign_keys=[tenant_id],
        overlaps="project,documents",
    )

    project: Mapped["Project"] = relationship(
        back_populates="documents",
        foreign_keys=[project_id],
        overlaps="tenant,documents",
    )

    uploader: Mapped["User"] = relationship(
        back_populates="uploaded_documents", foreign_keys=[uploaded_by]
    )


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('todo', 'in_progress', 'in_review', 'completed', 'blocked')",
            name="ck_tasks_status",
        ),
        CheckConstraint(
            "priority IN ('low', 'medium', 'high', 'critical')",
            name="ck_tasks_priority",
        ),
        Index("ix_tasks_project_status", "project_id", "status"),
        Index("ix_tasks_assigned_status", "assigned_to", "status"),
        Index("ix_tasks_due_date", "due_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)

    title: Mapped[str] = mapped_column(String(255), nullable=False)

    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    status: Mapped[str] = mapped_column(String(50), default="todo", nullable=False)

    priority: Mapped[str] = mapped_column(String(50), default="medium", nullable=False)

    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    due_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project: Mapped["Project"] = relationship(back_populates="tasks")

    assigned_to: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )

    assignee: Mapped["User | None"] = relationship(back_populates="assigned_tasks")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_tenant_created_at", "tenant_id", "created_at"),
        Index("ix_audit_logs_tenant_action", "tenant_id", "action"),
        Index("ix_audit_logs_tenant_resource_type", "tenant_id", "resource_type"),
        Index("ix_audit_logs_tenant_user", "tenant_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"),
        nullable=False
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        nullable=False
    )

    action: Mapped[str] = mapped_column(
        String(100),
        nullable=False
    )

    resource_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False
    )

    resource_id: Mapped[int | None] = mapped_column(
        nullable=True
    )

    details: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True
    )

    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata",
        JSON,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "recipient_id"],
            ["users.tenant_id", "users.id"],
            name="fk_notifications_tenant_recipient",
        ),
        ForeignKeyConstraint(
            ["source_event_id"],
            ["outbox_events.id"],
            name="fk_notifications_source_event",
        ),
        UniqueConstraint(
            "source_event_id",
            "recipient_id",
            name="uq_notifications_source_event_recipient",
        ),
        Index(
            "ix_notifications_tenant_recipient_read_created",
            "tenant_id",
            "recipient_id",
            "is_read",
            "created_at",
        ),
        Index(
            "ix_notifications_tenant_created_at",
            "tenant_id",
            "created_at",
        ),
        Index(
            "ix_notifications_tenant_event_type",
            "tenant_id",
            "event_type",
        ),
        Index(
            "ix_notifications_tenant_entity",
            "tenant_id",
            "entity_type",
            "entity_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )

    recipient_id: Mapped[int] = mapped_column(nullable=False)

    source_event_id: Mapped[int | None] = mapped_column(nullable=True)

    event_type: Mapped[str] = mapped_column(String(100), nullable=False)

    title: Mapped[str] = mapped_column(String(255), nullable=False)

    message: Mapped[str] = mapped_column(String(2000), nullable=False)

    entity_type: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )

    entity_id: Mapped[int | None] = mapped_column(nullable=True)

    is_read: Mapped[bool] = mapped_column(
        default=False, nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    tenant: Mapped["Tenant"] = relationship(
        back_populates="notifications", foreign_keys=[tenant_id]
    )

    recipient: Mapped["User"] = relationship(
        back_populates="received_notifications",
        foreign_keys=[recipient_id],
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'processed', 'failed')",
            name="ck_outbox_events_status",
        ),
        CheckConstraint(
            "retry_count >= 0",
            name="ck_outbox_events_retry_count",
        ),
        Index(
            "ix_outbox_events_tenant_status_created",
            "tenant_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_outbox_events_status_created",
            "status",
            "created_at",
        ),
        Index(
            "ix_outbox_events_tenant_event_type",
            "tenant_id",
            "event_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )

    event_type: Mapped[str] = mapped_column(String(100), nullable=False)

    event_payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20), default="pending", nullable=False
    )

    retry_count: Mapped[int] = mapped_column(default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    error_details: Mapped[str | None] = mapped_column(
        String(2000), nullable=True
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="outbox_events")
