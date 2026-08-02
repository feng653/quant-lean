"""Errors raised by the remote training worker."""


class RemoteTrainingError(RuntimeError):
    """Base class for expected, user-actionable worker failures."""


class ManifestValidationError(RemoteTrainingError):
    """The server-provided task manifest is invalid or unsafe."""


class DatasetValidationError(RemoteTrainingError):
    """The downloaded Parquet dataset violates the training contract."""


class StrategyValidationError(RemoteTrainingError):
    """The requested strategy is unavailable or does not match its manifest."""


class ArtifactError(RemoteTrainingError):
    """The trained model artifact cannot be safely published."""
