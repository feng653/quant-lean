"""Read-only cross-pool audit for legacy Parquet market-data caches."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

import numpy as np
import pandas as pd

from backend.data.cache import DataCache

LEGACY_CACHE_AUDIT_SCHEMA_VERSION = "legacy-price-cache-audit/v1"
DEFAULT_AUDIT_SCOPES = ("csi300", "csi500", "csi800", "csi1000")
_FIELDS = ("open", "high", "low", "close", "volume")
_OHLC = ("open", "high", "low", "close")
_REL_TOLERANCE = 1e-10


def _digest(value: Any) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _column_map(frame: pd.DataFrame) -> dict[tuple[str, str], Any]:
    if not isinstance(frame.columns, pd.MultiIndex):
        return {}
    return {
        (str(column[0]), str(column[-1]).lower()): column
        for column in frame.columns
    }


def _source_identity(metadata: dict[str, Any]) -> dict[str, Any]:
    provenance = metadata.get("source_provenance")
    if isinstance(provenance, dict):
        providers = sorted(str(item) for item in provenance.get("providers", []))
        endpoints = sorted(str(item) for item in provenance.get("endpoints", []))
        if providers and endpoints:
            return {
                "verified": True,
                "provider": "+".join(providers),
                "dataset": "+".join(endpoints),
                "version": str(provenance.get("schema_version") or "unknown"),
            }
    return {
        "verified": False,
        "provider": "legacy-unverified",
        "dataset": (
            f"schema-{metadata.get('schema_version') or 'unknown'}-daily-cache"
        ),
        "version": "unversioned",
    }


def _pair_source(
    left: dict[str, Any],
    right: dict[str, Any],
) -> tuple[dict[str, str], bool]:
    left_source = left["source"]
    right_source = right["source"]
    if (
        left_source["verified"]
        and right_source["verified"]
        and (
            left_source["provider"],
            left_source["dataset"],
            left_source["version"],
        )
        != (
            right_source["provider"],
            right_source["dataset"],
            right_source["version"],
        )
    ):
        return {}, False
    if left_source["verified"] and right_source["verified"]:
        return {
            "provider": str(left_source["provider"]),
            "dataset": str(left_source["dataset"]),
            "version": str(left_source["version"]),
        }, True
    return {
        "provider": "legacy-unverified",
        "dataset": "cross-pool-overlap",
        "version": "unversioned",
    }, True


async def audit_legacy_price_caches(
    cache: DataCache,
    *,
    start: str,
    end: str,
    scope_ids: Iterable[str] = DEFAULT_AUDIT_SCOPES,
    security_codes: Iterable[str] = (),
    limit: int = 1_000,
) -> dict[str, Any]:
    """Compare cache overlaps without fetching, modifying, or choosing winners."""

    required_start = pd.Timestamp(start).normalize()
    required_end = pd.Timestamp(end).normalize()
    if required_start > required_end:
        raise ValueError("start must not exceed end")
    if not 1 <= int(limit) <= 10_000:
        raise ValueError("limit is invalid")
    requested_codes = {
        str(item).strip()
        for item in security_codes
        if str(item).strip()
    }
    scopes = list(dict.fromkeys(str(item).strip() for item in scope_ids))
    if not scopes:
        raise ValueError("at least one scope_id is required")

    loaded: dict[str, dict[str, Any]] = {}
    unavailable: list[dict[str, str]] = []
    for scope_id in scopes:
        try:
            frame = await cache.load_legacy_pivot_for_audit(scope_id)
            metadata = await cache.get_cache_info(scope_id)
        except Exception as exc:
            unavailable.append(
                {"scope_id": scope_id, "reason": type(exc).__name__}
            )
            continue
        if frame is None or frame.empty:
            unavailable.append(
                {"scope_id": scope_id, "reason": "cache_missing"}
            )
            continue
        normalized = frame.copy()
        if not isinstance(normalized.index, pd.DatetimeIndex):
            normalized.index = pd.to_datetime(normalized.index, errors="coerce")
        normalized = normalized[
            (normalized.index >= required_start)
            & (normalized.index <= required_end)
        ].sort_index()
        columns = _column_map(normalized)
        codes = sorted(
            {
                code
                for code, field in columns
                if field in _FIELDS
                and (not requested_codes or code in requested_codes)
            }
        )
        adjustment = str(metadata.get("price_adjustment") or "unknown")
        loaded[scope_id] = {
            "frame": normalized,
            "columns": columns,
            "codes": codes,
            "schema_version": metadata.get("schema_version"),
            "adjustment": adjustment,
            "source": _source_identity(metadata),
        }

    conflict_fields: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    geometry_conflicts: set[tuple[Any, ...]] = set()
    return_conflicts: set[tuple[Any, ...]] = set()
    variant_digests: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    mixed_adjustment_fields: dict[
        tuple[Any, ...], set[str]
    ] = defaultdict(set)
    mixed_adjustment_geometry_conflicts: set[tuple[Any, ...]] = set()
    mixed_adjustment_return_conflicts: set[tuple[Any, ...]] = set()
    isolated_source_pairs: list[list[str]] = []
    mixed_adjustment_pairs: list[list[str]] = []
    compared_pairs = 0

    scope_names = sorted(loaded)
    for left_index, left_scope in enumerate(scope_names):
        left = loaded[left_scope]
        for right_scope in scope_names[left_index + 1 :]:
            right = loaded[right_scope]
            mixed_adjustment = (
                left["adjustment"] != right["adjustment"]
            )
            if mixed_adjustment:
                mixed_adjustment_pairs.append([left_scope, right_scope])
                source = {
                    "provider": "mixed-adjustment-overlap",
                    "dataset": "legacy-parquet-cache",
                    "version": "unversioned",
                }
            else:
                source, comparable = _pair_source(left, right)
                if not comparable:
                    isolated_source_pairs.append([left_scope, right_scope])
                    continue
            compared_pairs += 1
            adjustment = (
                f"{left['adjustment']}:{right['adjustment']}"
                if mixed_adjustment
                else str(left["adjustment"])
            )
            common_codes = sorted(set(left["codes"]) & set(right["codes"]))
            for code in common_codes:
                available_fields = [
                    field
                    for field in _FIELDS
                    if (code, field) in left["columns"]
                    and (code, field) in right["columns"]
                ]
                if not available_fields:
                    continue
                left_values = left["frame"][
                    [left["columns"][(code, field)] for field in available_fields]
                ].copy()
                right_values = right["frame"][
                    [
                        right["columns"][(code, field)]
                        for field in available_fields
                    ]
                ].copy()
                left_values.columns = available_fields
                right_values.columns = available_fields
                common_dates = left_values.index.intersection(
                    right_values.index
                )
                if common_dates.empty:
                    continue
                left_values = left_values.loc[common_dates].astype(float)
                right_values = right_values.loc[common_dates].astype(float)
                valid = np.isfinite(left_values) & np.isfinite(right_values)
                different = valid & ~np.isclose(
                    left_values,
                    right_values,
                    rtol=_REL_TOLERANCE,
                    atol=0.0,
                )
                for field in available_fields:
                    for timestamp in common_dates[different[field].to_numpy()]:
                        key = (
                            code,
                            timestamp.strftime("%Y-%m-%d"),
                            source["provider"],
                            source["dataset"],
                            source["version"],
                            adjustment,
                            left_scope,
                            right_scope,
                        )
                        (
                            mixed_adjustment_fields
                            if mixed_adjustment
                            else conflict_fields
                        )[key].add(field)
                        variant_digests[key].update(
                            {
                                _digest(
                                    {
                                        "scope_id": left_scope,
                                        field: float(
                                            left_values.at[timestamp, field]
                                        ),
                                    }
                                ),
                                _digest(
                                    {
                                        "scope_id": right_scope,
                                        field: float(
                                            right_values.at[timestamp, field]
                                        ),
                                    }
                                ),
                            }
                        )

                if all(field in available_fields for field in _OHLC):
                    left_geometry = left_values[list(_OHLC)].div(
                        left_values["close"],
                        axis=0,
                    )
                    right_geometry = right_values[list(_OHLC)].div(
                        right_values["close"],
                        axis=0,
                    )
                    geometry_valid = (
                        np.isfinite(left_geometry)
                        & np.isfinite(right_geometry)
                    ).all(axis=1)
                    geometry_changed = geometry_valid & (
                        ~np.isclose(
                            left_geometry,
                            right_geometry,
                            rtol=_REL_TOLERANCE,
                            atol=0.0,
                        )
                    ).any(axis=1)
                    for timestamp in common_dates[
                        geometry_changed.to_numpy()
                    ]:
                        (
                            mixed_adjustment_geometry_conflicts
                            if mixed_adjustment
                            else geometry_conflicts
                        ).add(
                            (
                                code,
                                timestamp.strftime("%Y-%m-%d"),
                                source["provider"],
                                source["dataset"],
                                source["version"],
                                adjustment,
                                left_scope,
                                right_scope,
                            )
                        )
                if "close" in available_fields:
                    left_return = left_values["close"].pct_change(
                        fill_method=None
                    )
                    right_return = right_values["close"].pct_change(
                        fill_method=None
                    )
                    return_valid = np.isfinite(left_return) & np.isfinite(
                        right_return
                    )
                    changed_return = return_valid & ~np.isclose(
                        left_return,
                        right_return,
                        rtol=_REL_TOLERANCE,
                        atol=0.0,
                    )
                    for timestamp in common_dates[
                        changed_return.to_numpy()
                    ]:
                        (
                            mixed_adjustment_return_conflicts
                            if mixed_adjustment
                            else return_conflicts
                        ).add(
                            (
                                code,
                                timestamp.strftime("%Y-%m-%d"),
                                source["provider"],
                                source["dataset"],
                                source["version"],
                                adjustment,
                                left_scope,
                                right_scope,
                            )
                        )

    ordered_keys = sorted(conflict_fields)
    ordered_mixed_keys = sorted(mixed_adjustment_fields)
    conflicts: list[dict[str, Any]] = []
    for key in ordered_keys[:limit]:
        fields = sorted(conflict_fields[key])
        classifications = ["absolute_price_conflict"]
        if "volume" in fields:
            classifications.append("volume_conflict")
        if key in geometry_conflicts:
            classifications.append("ohlc_geometry_conflict")
        if key in return_conflicts:
            classifications.append("return_conflict")
        if (
            key[5] in {"qfq", "hfq"}
            and key not in geometry_conflicts
            and key not in return_conflicts
            and any(field in fields for field in _OHLC)
            and "volume" not in fields
        ):
            classifications.append(
                f"{key[5]}_constant_anchor_conflict"
            )
        conflicts.append(
            {
                "security_code": key[0],
                "date": key[1],
                "source": {
                    "provider": key[2],
                    "dataset": key[3],
                    "version": key[4],
                },
                "adjustment": key[5],
                "scope_ids": [key[6], key[7]],
                "fields": fields,
                "classifications": classifications,
                "variant_digests": sorted(variant_digests[key]),
            }
        )

    mixed_adjustment_examples: list[dict[str, Any]] = []
    for key in ordered_mixed_keys[:limit]:
        classifications = [
            "mixed_adjustment_absolute_prices_not_comparable"
        ]
        if key in mixed_adjustment_geometry_conflicts:
            classifications.append("ohlc_geometry_conflict")
        if key in mixed_adjustment_return_conflicts:
            classifications.append("return_conflict")
        mixed_adjustment_examples.append(
            {
                "security_code": key[0],
                "date": key[1],
                "adjustments": str(key[5]).split(":", maxsplit=1),
                "scope_ids": [key[6], key[7]],
                "fields": sorted(mixed_adjustment_fields[key]),
                "classifications": classifications,
                "variant_digests": sorted(variant_digests[key]),
            }
        )

    schema3_scopes = sorted(
        scope_id
        for scope_id, item in loaded.items()
        if item["schema_version"] != 4
    )
    qfq_scopes = sorted(
        scope_id
        for scope_id, item in loaded.items()
        if item["adjustment"] == "qfq"
    )
    unverified_source_scopes = sorted(
        scope_id
        for scope_id, item in loaded.items()
        if not item["source"]["verified"]
    )
    negative_price_scopes: list[str] = []
    for scope_id, item in loaded.items():
        for (code, field), column in item["columns"].items():
            if field not in _OHLC:
                continue
            if requested_codes and code not in requested_codes:
                continue
            values = pd.to_numeric(item["frame"][column], errors="coerce")
            if bool((values.dropna() <= 0).any()):
                negative_price_scopes.append(scope_id)
                break

    limitations: list[str] = []
    if unavailable:
        limitations.append("cache_scope_unavailable")
    if schema3_scopes:
        limitations.append("legacy_schema3_cache_present")
    if qfq_scopes:
        limitations.append("qfq_cache_not_canonical_hfq_evidence")
    if unverified_source_scopes:
        limitations.append("cache_source_identity_unverified")
    if mixed_adjustment_pairs:
        limitations.append("mixed_adjustment_scopes_not_comparable")
    if negative_price_scopes:
        limitations.append("nonpositive_price_present")
    if ordered_keys:
        limitations.append("cross_pool_price_conflicts_present")
    limitations.append("audit_is_read_only_no_legacy_cache_rewrite")
    descriptive_consistency = bool(
        loaded
        and not unavailable
        and not schema3_scopes
        and not qfq_scopes
        and not unverified_source_scopes
        and not mixed_adjustment_pairs
        and not negative_price_scopes
        and not ordered_keys
    )
    limitations.append("legacy_cache_cannot_establish_unbiased_readiness")
    return {
        "schema_version": LEGACY_CACHE_AUDIT_SCHEMA_VERSION,
        "required_start": required_start.strftime("%Y-%m-%d"),
        "required_end": required_end.strftime("%Y-%m-%d"),
        "audit_available": bool(loaded),
        "conflict_free": not ordered_keys,
        "descriptive_return_consistency": descriptive_consistency,
        "ready_for_unbiased_return_research": False,
        "ready_for_unbiased_research": False,
        "checked_scopes": [
            {
                "scope_id": scope_id,
                "schema_version": item["schema_version"],
                "adjustment": item["adjustment"],
                "source_verified": item["source"]["verified"],
            }
            for scope_id, item in sorted(loaded.items())
        ],
        "unavailable_scopes": unavailable,
        "compared_scope_pair_count": compared_pairs,
        "isolated_source_pairs": isolated_source_pairs,
        "mixed_adjustment_pairs": mixed_adjustment_pairs,
        "legacy_schema3_scopes": schema3_scopes,
        "qfq_scopes": qfq_scopes,
        "unverified_source_scopes": unverified_source_scopes,
        "nonpositive_price_scopes": sorted(set(negative_price_scopes)),
        "conflict_identity_count": len(ordered_keys),
        "return_conflict_count": len(return_conflicts & set(ordered_keys)),
        "geometry_conflict_count": len(
            geometry_conflicts & set(ordered_keys)
        ),
        "conflicts": conflicts,
        "mixed_adjustment_identity_count": len(ordered_mixed_keys),
        "mixed_adjustment_return_conflict_count": len(
            mixed_adjustment_return_conflicts & set(ordered_mixed_keys)
        ),
        "mixed_adjustment_geometry_conflict_count": len(
            mixed_adjustment_geometry_conflicts & set(ordered_mixed_keys)
        ),
        "mixed_adjustment_examples": mixed_adjustment_examples,
        "truncated": (
            len(ordered_keys) > limit or len(ordered_mixed_keys) > limit
        ),
        "limitations": list(dict.fromkeys(limitations)),
    }
