"""HTTP boundary for the preregistered research workflow."""

from __future__ import annotations

from typing import Any, Awaitable

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api.research_workflow_schemas import (
    CreateGroupBody,
    CreateHypothesisBody,
    CreatePromotionBody,
    CreateReportBody,
    GroupTransitionBody,
    LinkTrialBody,
    PromotionTransitionBody,
    UpdateHypothesisBody,
    VersionBody,
)
from backend.config import settings
from backend.dependencies import get_current_user, require_permission
from backend.services.research_workflow import (
    ResearchWorkflowService,
    WorkflowError,
)


router = APIRouter(
    prefix="/api/research/workflows",
    tags=["Research Workflow"],
)


def _service() -> ResearchWorkflowService:
    return ResearchWorkflowService(
        settings.abs_path(settings.EXPERIMENT_DB)
    )


async def _response(awaitable: Awaitable[Any]) -> dict[str, Any]:
    try:
        return {"data": await awaitable}
    except WorkflowError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail(),
        ) from exc


@router.post("/hypotheses")
async def create_hypothesis(
    body: CreateHypothesisBody,
    user: dict[str, Any] = Depends(
        require_permission("experiments:create")
    ),
) -> dict[str, Any]:
    return await _response(
        _service().create_hypothesis(
            body.model_dump(mode="json"),
            user,
        )
    )


@router.get("/hypotheses/{hypothesis_id}")
async def get_hypothesis(
    hypothesis_id: int,
    user: dict[str, Any] = Depends(
        require_permission("experiments:read")
    ),
) -> dict[str, Any]:
    return await _response(_service().get_hypothesis(hypothesis_id, user))


@router.put("/hypotheses/{hypothesis_id}")
async def update_hypothesis(
    hypothesis_id: int,
    body: UpdateHypothesisBody,
    user: dict[str, Any] = Depends(
        require_permission("experiments:create")
    ),
) -> dict[str, Any]:
    return await _response(
        _service().update_hypothesis(
            hypothesis_id,
            body.model_dump(mode="json"),
            user,
        )
    )


@router.post("/hypotheses/{hypothesis_id}/submit")
async def submit_hypothesis(
    hypothesis_id: int,
    body: VersionBody,
    user: dict[str, Any] = Depends(
        require_permission("experiments:create")
    ),
) -> dict[str, Any]:
    return await _response(
        _service().transition_hypothesis(
            hypothesis_id,
            target_status="submitted",
            expected_version=body.expected_version,
            user=user,
        )
    )


@router.post("/hypotheses/{hypothesis_id}/withdraw")
async def withdraw_hypothesis(
    hypothesis_id: int,
    body: VersionBody,
    user: dict[str, Any] = Depends(
        require_permission("experiments:create")
    ),
) -> dict[str, Any]:
    return await _response(
        _service().transition_hypothesis(
            hypothesis_id,
            target_status="withdrawn",
            expected_version=body.expected_version,
            user=user,
        )
    )


@router.post("/groups")
async def create_group(
    body: CreateGroupBody,
    user: dict[str, Any] = Depends(
        require_permission("experiments:create")
    ),
) -> dict[str, Any]:
    return await _response(
        _service().create_group(body.model_dump(mode="json"), user)
    )


@router.get("/groups/{group_id}")
async def get_group(
    group_id: int,
    user: dict[str, Any] = Depends(
        require_permission("experiments:read")
    ),
) -> dict[str, Any]:
    return await _response(_service().get_group(group_id, user))


@router.post("/groups/{group_id}/transition")
async def transition_group(
    group_id: int,
    body: GroupTransitionBody,
    user: dict[str, Any] = Depends(
        require_permission("experiments:create")
    ),
) -> dict[str, Any]:
    return await _response(
        _service().transition_group(
            group_id,
            target_status=body.target_status,
            expected_version=body.expected_version,
            user=user,
        )
    )


@router.post("/groups/{group_id}/trials")
async def link_trial(
    group_id: int,
    body: LinkTrialBody,
    user: dict[str, Any] = Depends(
        require_permission("experiments:create")
    ),
) -> dict[str, Any]:
    return await _response(
        _service().link_trial(
            group_id,
            body.model_dump(mode="json"),
            user,
        )
    )


@router.post("/groups/{group_id}/reports")
async def create_report(
    group_id: int,
    body: CreateReportBody,
    user: dict[str, Any] = Depends(
        require_permission("experiments:create")
    ),
) -> dict[str, Any]:
    return await _response(
        _service().create_report(
            group_id,
            body.model_dump(mode="json"),
            user,
        )
    )


@router.post("/groups/{group_id}/promotions")
async def create_promotion(
    group_id: int,
    body: CreatePromotionBody,
    user: dict[str, Any] = Depends(
        require_permission("experiments:create")
    ),
) -> dict[str, Any]:
    return await _response(
        _service().create_promotion(
            group_id,
            body.model_dump(mode="json"),
            user,
        )
    )


@router.get("/promotions/{promotion_id}")
async def get_promotion(
    promotion_id: int,
    user: dict[str, Any] = Depends(
        require_permission("experiments:read")
    ),
) -> dict[str, Any]:
    return await _response(_service().get_promotion(promotion_id, user))


@router.post("/promotions/{promotion_id}/transition")
async def transition_promotion(
    promotion_id: int,
    body: PromotionTransitionBody,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    if (
        body.target_status == "reviewed"
        and not user.get("is_admin")
        and "experiments:create" not in user.get("permissions", [])
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "experiments_create_required",
                "message": "experiments:create permission is required",
            },
        )
    return await _response(
        _service().transition_promotion(
            promotion_id,
            body.model_dump(mode="json"),
            user,
        )
    )


@router.get("/audit/events")
async def list_audit_events(
    entity_type: str = Query(
        pattern=r"^(hypothesis|group|trial|report|promotion)$"
    ),
    entity_id: int = Query(ge=1),
    user: dict[str, Any] = Depends(
        require_permission("experiments:read")
    ),
) -> dict[str, Any]:
    return await _response(
        _service().list_events(
            entity_type=entity_type,
            entity_id=entity_id,
            user=user,
        )
    )
