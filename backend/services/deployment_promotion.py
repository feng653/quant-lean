"""Fail-closed research-promotion binding for simulation deployments."""

from __future__ import annotations

from typing import Any, Mapping

from backend.config import settings
from backend.services.research_manifest import canonical_sha256
from backend.services.research_workflow import (
    ResearchWorkflowService,
    WorkflowError,
)


PROMOTION_BINDING_SCHEMA = "research-promotion-binding/v1"


class DeploymentPromotionError(ValueError):
    """A deployment cannot execute with the supplied promotion evidence."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        blockers: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.blockers = blockers or []

    def detail(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.blockers:
            result["blockers"] = self.blockers
        return result


def _required_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise DeploymentPromotionError(
            "promotion_binding_incomplete",
            f"Deployment promotion binding is missing {field}",
        ) from exc


async def resolve_deployment_promotion(
    *,
    promotion_id: int,
    owner_user_id: int,
    experiment_id: int,
    strategy_id: str,
    params_hash: str,
    model_artifact_id: int | None,
) -> dict[str, Any]:
    """Resolve and hash current approved evidence before persisting a binding."""
    service = ResearchWorkflowService(
        settings.abs_path(settings.EXPERIMENT_DB)
    )
    try:
        identity = await service.verify_deployment_binding(
            promotion_id,
            owner_user_id=owner_user_id,
            experiment_id=experiment_id,
            strategy_id=strategy_id,
            params_hash=params_hash,
            model_artifact_id=model_artifact_id,
        )
    except WorkflowError as exc:
        raise DeploymentPromotionError(
            exc.code,
            exc.message,
            blockers=exc.blockers,
        ) from exc
    identity["binding_hash"] = canonical_sha256(identity)
    return identity


async def verify_deployment_promotion(
    deployment: Mapping[str, Any],
) -> dict[str, Any]:
    """Revalidate approval, ownership, trial, manifest, and model evidence."""
    promotion_id = _required_int(
        deployment.get("research_promotion_id"),
        "research_promotion_id",
    )
    owner_user_id = _required_int(deployment.get("user_id"), "user_id")
    experiment_id = _required_int(
        deployment.get("source_experiment_id"),
        "source_experiment_id",
    )
    stored_binding_hash = deployment.get("promotion_binding_hash")
    if not isinstance(stored_binding_hash, str) or len(stored_binding_hash) != 64:
        raise DeploymentPromotionError(
            "promotion_binding_incomplete",
            "Deployment promotion binding hash is missing or invalid",
        )
    current = await resolve_deployment_promotion(
        promotion_id=promotion_id,
        owner_user_id=owner_user_id,
        experiment_id=experiment_id,
        strategy_id=str(deployment.get("strategy_id") or ""),
        params_hash=str(deployment.get("params_hash") or ""),
        model_artifact_id=(
            int(deployment["source_model_artifact_id"])
            if deployment.get("source_model_artifact_id") is not None
            else None
        ),
    )
    stored_identity = {
        "schema_version": PROMOTION_BINDING_SCHEMA,
        "promotion_id": promotion_id,
        "promotion_version": _required_int(
            deployment.get("promotion_version"),
            "promotion_version",
        ),
        "report_id": _required_int(
            deployment.get("promotion_report_id"),
            "promotion_report_id",
        ),
        "report_hash": deployment.get("promotion_report_hash"),
        "experiment_id": experiment_id,
        "manifest_hash": deployment.get("promotion_manifest_hash"),
        "model_artifact_id": (
            int(deployment["promotion_model_artifact_id"])
            if deployment.get("promotion_model_artifact_id") is not None
            else None
        ),
        "model_sha256": deployment.get("promotion_model_sha256"),
        "model_evidence_hash": deployment.get("promotion_evidence_hash"),
    }
    if (
        canonical_sha256(stored_identity) != stored_binding_hash
        or current != {**stored_identity, "binding_hash": stored_binding_hash}
    ):
        raise DeploymentPromotionError(
            "promotion_binding_changed",
            "Deployment promotion evidence differs from its immutable binding",
        )
    return current
