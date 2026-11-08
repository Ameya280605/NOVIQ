import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.schemas.assistant import AssistantRequest, AssistantResponse
from backend.app.services.assistant import AssistantUnavailable, answer_question
from backend.app.services.assistant_tools import (
    AssistantAccessDenied,
    AssistantResourceNotFound,
)
from backend.app.utils.roles import require_roles

router = APIRouter(prefix="/assistant", tags=["Assistant"])
logger = logging.getLogger(__name__)
_ASSISTANT_ROLES = ("owner", "admin", "manager", "member")


@router.post("/query", response_model=AssistantResponse)
def query_assistant(
    request: AssistantRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_ASSISTANT_ROLES)),
):
    try:
        result = answer_question(
            db=db,
            tenant_id=int(current_user["tenant_id"]),
            user_id=int(current_user["sub"]),
            question=request.question,
            project_id=request.project_id,
        )
    except AssistantUnavailable as exc:
        logger.info(
            "assistant_request_failed",
            extra={"assistant_outcome": "provider_unavailable"},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except AssistantResourceNotFound as exc:
        logger.info(
            "assistant_request_failed",
            extra={"assistant_outcome": "resource_not_found"},
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AssistantAccessDenied as exc:
        logger.info(
            "assistant_request_failed",
            extra={"assistant_outcome": "access_denied"},
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    logger.info(
        "assistant_request_completed",
        extra={
            "assistant_intent": result["intent"],
            "assistant_outcome": "refused" if result["intent"] == "refused" else "ok",
            "assistant_provider": "local_rules",
        },
    )
    return result
