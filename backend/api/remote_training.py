"""HTTP contract for server-managed remote model training."""

from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.dependencies import get_strategy_registry, require_permission
from backend.services.remote_training import (
    RemoteTrainingError,
    RemoteTrainingService,
)

router = APIRouter(prefix="/api/remote-training", tags=["Remote Training"])


class CreateRemoteTrainingTaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment_id: int = Field(ge=1)
    train_start: str | None = None
    train_end: str | None = None

    @model_validator(mode="after")
    def validate_window_override(self):
        if bool(self.train_start) != bool(self.train_end):
            raise ValueError("train_start 和 train_end 必须同时提供")
        return self


class ProgressBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    progress: float = Field(ge=0, lt=1)
    message: str | None = Field(None, max_length=500)


class FailureBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: str = Field(min_length=1, max_length=8000)


def get_remote_training_service() -> RemoteTrainingService:
    return RemoteTrainingService()


def _api_error(exc: RemoteTrainingError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.post("/tasks")
async def create_remote_training_task(
    body: CreateRemoteTrainingTaskBody,
    user: dict[str, Any] = Depends(require_permission("experiments:create")),
    registry: Any = Depends(get_strategy_registry),
    service: RemoteTrainingService = Depends(get_remote_training_service),
) -> dict[str, Any]:
    """Freeze one training window and return its worker token exactly once."""
    try:
        task, token = await service.create_task(
            experiment_id=body.experiment_id,
            user_id=int(user["id"]),
            registry=registry,
            train_start=body.train_start,
            train_end=body.train_end,
        )
    except RemoteTrainingError as exc:
        raise _api_error(exc) from exc
    return {"data": {**task, "task_token": token}}


@router.get("/tasks")
async def list_remote_training_tasks(
    experiment_id: int | None = Query(None, ge=1),
    user: dict[str, Any] = Depends(require_permission("experiments:read")),
    service: RemoteTrainingService = Depends(get_remote_training_service),
) -> dict[str, Any]:
    try:
        tasks = await service.list_tasks(
            user_id=int(user["id"]),
            experiment_id=experiment_id,
        )
    except RemoteTrainingError as exc:
        raise _api_error(exc) from exc
    return {"data": tasks}


@router.get("/tasks/{task_uuid}")
async def get_remote_training_task(
    task_uuid: str,
    user: dict[str, Any] = Depends(require_permission("experiments:read")),
    service: RemoteTrainingService = Depends(get_remote_training_service),
) -> dict[str, Any]:
    try:
        task = await service.get_task(
            task_uuid=task_uuid,
            user_id=int(user["id"]),
        )
    except RemoteTrainingError as exc:
        raise _api_error(exc) from exc
    return {"data": task}


@router.post("/tasks/{task_uuid}/cancel")
async def cancel_remote_training_task(
    task_uuid: str,
    user: dict[str, Any] = Depends(require_permission("experiments:create")),
    service: RemoteTrainingService = Depends(get_remote_training_service),
) -> dict[str, Any]:
    try:
        task = await service.cancel_task(
            task_uuid=task_uuid,
            user_id=int(user["id"]),
        )
    except RemoteTrainingError as exc:
        raise _api_error(exc) from exc
    return {"data": task}


@router.get("/tasks/{task_uuid}/bundle")
async def get_remote_training_bundle(
    task_uuid: str,
    training_token: str | None = Header(None, alias="X-Training-Token"),
    service: RemoteTrainingService = Depends(get_remote_training_service),
) -> dict[str, Any]:
    try:
        manifest = await service.worker_manifest(
            task_uuid=task_uuid,
            token=training_token,
        )
    except RemoteTrainingError as exc:
        raise _api_error(exc) from exc
    return {"data": manifest}


@router.get("/tasks/{task_uuid}/data")
async def download_remote_training_data(
    task_uuid: str,
    training_token: str | None = Header(None, alias="X-Training-Token"),
    service: RemoteTrainingService = Depends(get_remote_training_service),
) -> FileResponse:
    try:
        path = await service.worker_data_path(
            task_uuid=task_uuid,
            token=training_token,
        )
    except RemoteTrainingError as exc:
        raise _api_error(exc) from exc
    return FileResponse(
        path,
        media_type="application/vnd.apache.parquet",
        filename="training-data.parquet",
    )


@router.post("/tasks/{task_uuid}/start")
async def start_remote_training_task(
    task_uuid: str,
    training_token: str | None = Header(None, alias="X-Training-Token"),
    service: RemoteTrainingService = Depends(get_remote_training_service),
) -> dict[str, Any]:
    try:
        task = await service.worker_start(
            task_uuid=task_uuid,
            token=training_token,
        )
    except RemoteTrainingError as exc:
        raise _api_error(exc) from exc
    return {"data": task}


@router.post("/tasks/{task_uuid}/progress")
async def update_remote_training_progress(
    task_uuid: str,
    body: ProgressBody,
    training_token: str | None = Header(None, alias="X-Training-Token"),
    service: RemoteTrainingService = Depends(get_remote_training_service),
) -> dict[str, Any]:
    try:
        task = await service.worker_progress(
            task_uuid=task_uuid,
            token=training_token,
            progress=body.progress,
            message=body.message,
        )
    except RemoteTrainingError as exc:
        raise _api_error(exc) from exc
    return {"data": task}


@router.post("/tasks/{task_uuid}/complete")
async def complete_remote_training_task(
    task_uuid: str,
    report_json: str = Form(...),
    artifact: UploadFile = File(...),
    training_token: str | None = Header(None, alias="X-Training-Token"),
    service: RemoteTrainingService = Depends(get_remote_training_service),
) -> dict[str, Any]:
    try:
        task = await service.worker_complete(
            task_uuid=task_uuid,
            token=training_token,
            report_json=report_json,
            artifact=artifact,
        )
    except RemoteTrainingError as exc:
        raise _api_error(exc) from exc
    finally:
        await artifact.close()
    return {"data": task}


@router.post("/tasks/{task_uuid}/fail")
async def fail_remote_training_task(
    task_uuid: str,
    body: FailureBody,
    training_token: str | None = Header(None, alias="X-Training-Token"),
    service: RemoteTrainingService = Depends(get_remote_training_service),
) -> dict[str, Any]:
    try:
        task = await service.worker_fail(
            task_uuid=task_uuid,
            token=training_token,
            error=body.error,
        )
    except RemoteTrainingError as exc:
        raise _api_error(exc) from exc
    return {"data": task}
