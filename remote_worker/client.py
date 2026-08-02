"""HTTP transport for the frozen remote-training protocol."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .errors import DatasetValidationError, RemoteTrainingError
from .manifest import normalize_server_manifest, validate_task_uuid


class RemoteTrainingHTTPClient:
    """Small synchronous client with no token-bearing log output."""

    def __init__(
        self,
        server: str,
        task_id: str,
        token: str,
        *,
        timeout_seconds: float = 120.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not token:
            raise ValueError("remote training token is required")
        self.task_id = validate_task_uuid(task_id)
        self._server = self._normalize_server(server)
        self._task_url = (
            f"{self._server}/api/remote-training/tasks/{self.task_id}"
        )
        self._headers = {"X-Training-Token": token}
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=20.0),
            follow_redirects=False,
        )

    @staticmethod
    def _normalize_server(server: str) -> str:
        parsed = urlsplit(server)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("--server must be an HTTP(S) origin or base path")
        path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    def __enter__(self) -> "RemoteTrainingHTTPClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _check(self, response: httpx.Response, operation: str) -> httpx.Response:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RemoteTrainingError(
                f"{operation} failed with HTTP {response.status_code}"
            ) from exc
        return response

    def get_manifest(self) -> dict[str, Any]:
        response = self._check(
            self._client.get(
                f"{self._task_url}/bundle",
                headers=self._headers,
            ),
            "manifest download",
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RemoteTrainingError(
                "manifest response is not valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise RemoteTrainingError("manifest response must be a JSON object")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RemoteTrainingError(
                "manifest response must contain a data object"
            )
        return normalize_server_manifest(
            data,
            expected_task_uuid=self.task_id,
        )

    def _validated_dataset_url(self, manifest_url: str) -> str:
        expected = f"{self._task_url}/data"
        resolved = urljoin(f"{self._task_url}/", manifest_url)
        actual_parts = urlsplit(resolved)
        expected_parts = urlsplit(expected)
        if (
            actual_parts.scheme != expected_parts.scheme
            or actual_parts.netloc != expected_parts.netloc
            or actual_parts.path.rstrip("/") != expected_parts.path
            or actual_parts.query
            or actual_parts.fragment
        ):
            raise DatasetValidationError(
                "dataset.url must resolve to this task's /data endpoint"
            )
        return expected

    def download_dataset(
        self,
        manifest_url: str,
        destination: Path,
        expected_sha256: str,
    ) -> int:
        url = self._validated_dataset_url(manifest_url)
        digest = hashlib.sha256()
        written = 0
        try:
            with self._client.stream(
                "GET",
                url,
                headers=self._headers,
            ) as response:
                self._check(response, "dataset download")
                with destination.open("xb") as handle:
                    for chunk in response.iter_bytes():
                        if not chunk:
                            continue
                        handle.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        if digest.hexdigest() != expected_sha256:
            destination.unlink(missing_ok=True)
            raise DatasetValidationError("Parquet SHA-256 mismatch")
        return written

    def start(self) -> None:
        self._check(
            self._client.post(
                f"{self._task_url}/start",
                headers=self._headers,
            ),
            "start report",
        )

    def progress(self, progress: float, message: str | None = None) -> None:
        payload: dict[str, Any] = {"progress": progress}
        if message is not None:
            payload["message"] = message
        self._check(
            self._client.post(
                f"{self._task_url}/progress",
                headers=self._headers,
                json=payload,
            ),
            "progress report",
        )

    def complete(
        self,
        report: dict[str, Any],
        artifact_path: Path,
    ) -> None:
        report_json = json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with artifact_path.open("rb") as artifact:
            files = {
                "report_json": (
                    None,
                    report_json,
                    "application/json",
                ),
                "artifact": (
                    artifact_path.name,
                    artifact,
                    "application/octet-stream",
                ),
            }
            self._check(
                self._client.post(
                    f"{self._task_url}/complete",
                    headers=self._headers,
                    files=files,
                ),
                "completion upload",
            )

    def fail(self, error: str) -> None:
        self._check(
            self._client.post(
                f"{self._task_url}/fail",
                headers=self._headers,
                json={"error": error},
            ),
            "failure report",
        )
