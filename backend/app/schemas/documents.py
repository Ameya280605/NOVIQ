import os
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_DOCUMENT_SIZE_BYTES = int(
    os.getenv("DOCUMENT_MAX_SIZE_BYTES", str(25 * 1024 * 1024))
)
if MAX_DOCUMENT_SIZE_BYTES < 1:
    raise ValueError("DOCUMENT_MAX_SIZE_BYTES must be at least 1")
ALLOWED_DOCUMENT_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "text/plain",
        "image/jpeg",
        "image/png",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
)

DocumentStatus = Literal[
    "pending_upload", "available", "deletion_pending", "failed", "deleted"
]


class DocumentUploadRequest(BaseModel):
    original_filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1, le=MAX_DOCUMENT_SIZE_BYTES)

    model_config = ConfigDict(extra="forbid")

    @field_validator("original_filename")
    @classmethod
    def filename_must_be_a_safe_basename(cls, value: str) -> str:
        filename = value.strip()
        if (
            not filename
            or "/" in filename
            or "\\" in filename
            or any(ord(character) < 32 or ord(character) == 127 for character in filename)
        ):
            raise ValueError("original_filename must be a plain filename")
        return filename

    @field_validator("content_type")
    @classmethod
    def content_type_must_be_allowed(cls, value: str) -> str:
        if value not in ALLOWED_DOCUMENT_CONTENT_TYPES:
            raise ValueError("content_type is not allowed")
        return value


class DocumentMetadata(BaseModel):
    id: int
    project_id: int
    uploaded_by: int
    original_filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    checksum: str | None = Field(default=None, max_length=128)
    status: DocumentStatus
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None

    model_config = ConfigDict(from_attributes=True)

    @field_validator("original_filename", "content_type")
    @classmethod
    def metadata_strings_cannot_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value cannot be blank")
        return value


class DocumentUploadResponse(BaseModel):
    document: DocumentMetadata
    upload_url: str
    headers: dict[str, str]
    expires_in: int


class DocumentListResponse(BaseModel):
    items: list[DocumentMetadata]
    page: int
    page_size: int
    total: int
    total_pages: int


class DocumentDownloadResponse(BaseModel):
    download_url: str
    expires_in: int


class DocumentDeletionResponse(BaseModel):
    document_id: int
    status: Literal["deletion_pending", "deleted"]
    cleanup_queued: bool
