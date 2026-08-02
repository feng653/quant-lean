from pathlib import Path

from backend.api.storage_paths import redact_model_storage_paths
from backend.config import settings


def test_model_api_paths_are_relative_and_outside_paths_are_redacted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    model_root = tmp_path / "models"
    monkeypatch.setattr(settings, "MODEL_STORE_DIR", str(model_root))

    projected = redact_model_storage_paths(
        {
            "id": 1,
            "model_file_path": str(model_root / "experiment_1" / "model.joblib"),
            "metadata_file_path": str(tmp_path / "outside.json"),
        }
    )

    assert projected["model_storage_key"] == "experiment_1/model.joblib"
    assert projected["metadata_storage_key"] is None
    assert "model_file_path" not in projected
    assert "metadata_file_path" not in projected


def test_model_api_rejects_relative_path_traversal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "MODEL_STORE_DIR", str(tmp_path / "models"))

    projected = redact_model_storage_paths(
        {
            "model_file_path": "../../secret.joblib",
            "metadata_file_path": "../metadata.json",
        }
    )

    assert projected["model_storage_key"] is None
    assert projected["metadata_storage_key"] is None
