from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import ssl
import sys
import threading
import time
from types import SimpleNamespace

import pandas as pd
import pytest
from requests.exceptions import SSLError as RequestsSSLError

from backend.data.source_validation import (
    CrossSourceConflictError,
    DailyFetchResult,
    SourceEvidenceError,
    build_cache_source_provenance,
    build_daily_fetch_evidence,
    compare_independent_daily_frames,
    require_cross_source_acceptance,
    validate_cache_source_provenance,
    validate_daily_fetch_evidence,
)
from backend.data.sources.akshare_source import (
    AKShareCallError,
    AKShareSource,
    ProviderOutageError,
    _run_sync,
)
from backend.data.sources.baostock_source import (
    BaoStockSource,
    _rebuild_hfq,
    rebuild_hfq_panel,
)
from backend.data.sources.validated import (
    CrossValidatedDailySource,
    build_public_research_source,
)


def _frame(
    *,
    code: str = "000001",
    multiplier: float = 1.0,
    conflict_at: int | None = None,
) -> pd.DataFrame:
    index = pd.bdate_range("2024-01-02", periods=35, name="date")
    close = pd.Series(
        [10.0 * (1.01**position) * multiplier for position in range(35)],
        index=index,
    )
    if conflict_at is not None:
        close.iloc[conflict_at] *= 1.10
    columns = pd.MultiIndex.from_product(
        [[code], ["open", "high", "low", "close", "volume"]],
        names=["code", "field"],
    )
    frame = pd.DataFrame(index=index, columns=columns, dtype=float)
    frame[(code, "close")] = close
    frame[(code, "open")] = close * 0.99
    frame[(code, "high")] = close * 1.01
    frame[(code, "low")] = close * 0.98
    frame[(code, "volume")] = 1000.0
    return frame


def _evidence(frame: pd.DataFrame) -> dict:
    return build_daily_fetch_evidence(
        frame,
        requested_codes=["000001"],
        start="2024-01-02",
        end="2024-02-19",
        provider="feed-a",
        endpoint="feed-a/daily",
        adjustment="qfq",
        evidence_level="declared",
    )


def test_daily_fetch_evidence_records_missing_codes_and_detects_tampering() -> None:
    evidence = build_daily_fetch_evidence(
        _frame(),
        requested_codes=["000001", "000002"],
        start="2024-01-02",
        end="2024-02-19",
        provider="feed-a",
        endpoint="feed-a/daily",
        adjustment="qfq",
        evidence_level="declared",
    )

    assert evidence["complete_code_coverage"] is False
    assert evidence["response"]["failed_codes"] == {
        "000002": "no_observations"
    }
    tampered = deepcopy(evidence)
    tampered["provider"] = "feed-b"
    with pytest.raises(SourceEvidenceError, match="hash verification"):
        validate_daily_fetch_evidence(tampered)


def test_cross_source_validation_accepts_different_qfq_scale() -> None:
    evidence = compare_independent_daily_frames(
        _frame(),
        _frame(multiplier=100.0),
        primary_provider="feed-a",
        reference_provider="feed-b",
        requested_codes=["000001"],
        adjustment="qfq",
        min_overlap_returns=20,
        return_abs_tolerance=1e-10,
    )

    require_cross_source_acceptance(evidence)
    assert evidence["summary"]["acceptable"] is True
    assert evidence["summary"]["conflict_count"] == 0


def test_cross_source_validation_records_edge_only_coverage_difference() -> None:
    primary = _frame()
    edge = primary.iloc[[0]].copy()
    edge.index = pd.DatetimeIndex(
        [primary.index[0] - pd.Timedelta(days=7)],
        name="date",
    )
    reference = pd.concat([edge, primary]).sort_index()

    evidence = compare_independent_daily_frames(
        primary,
        reference,
        primary_provider="feed-a",
        reference_provider="feed-b",
        requested_codes=["000001"],
        adjustment="raw",
        min_overlap_returns=20,
        return_abs_tolerance=1e-10,
    )

    require_cross_source_acceptance(evidence)
    code = evidence["per_code"][0]
    assert code["status"] == "passed"
    assert code["overlap_returns"] == 34
    assert code["primary_only_dates"] == 0
    assert code["reference_only_dates"] == 1
    assert code["edge_date_mask_difference"] is True


def test_cross_source_validation_rejects_interior_date_mask_difference() -> None:
    primary = _frame()
    reference = primary.drop(index=primary.index[12])

    evidence = compare_independent_daily_frames(
        primary,
        reference,
        primary_provider="feed-a",
        reference_provider="feed-b",
        requested_codes=["000001"],
        adjustment="raw",
        min_overlap_returns=20,
    )

    with pytest.raises(CrossSourceConflictError, match="insufficient_codes"):
        require_cross_source_acceptance(evidence)
    code = evidence["per_code"][0]
    assert code["status"] == "date_mask_mismatch"
    assert code["interior_primary_only_dates"] == 1


def test_cross_source_conflict_and_insufficient_overlap_fail_closed() -> None:
    conflict = compare_independent_daily_frames(
        _frame(),
        _frame(conflict_at=25),
        primary_provider="feed-a",
        reference_provider="feed-b",
        requested_codes=["000001"],
        adjustment="qfq",
        min_overlap_returns=20,
        return_abs_tolerance=0.005,
    )
    with pytest.raises(CrossSourceConflictError, match="validation failed"):
        require_cross_source_acceptance(conflict)

    short = compare_independent_daily_frames(
        _frame().iloc[:5],
        _frame().iloc[:5],
        primary_provider="feed-a",
        reference_provider="feed-b",
        requested_codes=["000001"],
        adjustment="qfq",
        min_overlap_returns=20,
    )
    with pytest.raises(CrossSourceConflictError, match="insufficient_codes"):
        require_cross_source_acceptance(short)


def test_cross_source_failure_exposes_bounded_structured_diagnostics() -> None:
    reference = _frame(conflict_at=25)
    reference.loc[reference.index[25], ("000001", "open")] *= 1.02
    reference.loc[reference.index[26], ("000001", "volume")] *= 2
    evidence = compare_independent_daily_frames(
        _frame(),
        reference,
        primary_provider="feed-a",
        reference_provider="feed-b",
        requested_codes=["000001"],
        adjustment="hfq",
    )

    with pytest.raises(
        CrossSourceConflictError,
        match=r"conflicted_codes=000001\(r=2,o=",
    ) as captured:
        require_cross_source_acceptance(evidence)

    details = captured.value.evidence_summary
    assert details is not None
    assert details["schema_version"] == "cross-source-failure-summary/v1"
    assert details["summary"]["conflicted_code_count"] == 1
    code = details["conflicted_codes"][0]
    assert code["code"] == "000001"
    assert code["return_conflicts"] == 2
    assert code["geometry_conflicts"] >= 1
    assert code["volume_conflicts"] == 1
    assert code["conflicts"] >= 2
    assert all(
        set(example)
        == {
            "code",
            "date",
            "dimensions",
            "primary_return",
            "reference_return",
            "abs_return_delta",
        }
        for example in details["examples"]
    )
    assert all("/" not in str(example) for example in details["examples"])


def test_cache_provenance_binds_frame_and_rejects_mixed_providers() -> None:
    frame = _frame()
    first = _evidence(frame)
    provenance = build_cache_source_provenance(frame, [first])
    validate_cache_source_provenance(provenance, frame=frame)

    changed = frame.copy()
    changed.iloc[-1, changed.columns.get_loc(("000001", "close"))] += 1
    with pytest.raises(SourceEvidenceError, match="frame digest changed"):
        validate_cache_source_provenance(provenance, frame=changed)

    second = build_daily_fetch_evidence(
        frame,
        requested_codes=["000001"],
        start="2024-01-02",
        end="2024-02-19",
        provider="feed-b",
        endpoint="feed-b/daily",
        adjustment="qfq",
        evidence_level="declared",
    )
    mixed = build_cache_source_provenance(frame, [first, second])
    assert mixed["identity_consistent"] is False


def test_akshare_eastmoney_failure_does_not_silently_fallback_to_sina(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"eastmoney": 0, "sina": 0}

    def eastmoney(**kwargs):
        del kwargs
        calls["eastmoney"] += 1
        raise RuntimeError("primary failed")

    def sina(**kwargs):
        del kwargs
        calls["sina"] += 1
        return pd.DataFrame()

    async def no_retry(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(
            stock_zh_a_hist=eastmoney,
            stock_zh_a_daily=sina,
        ),
    )
    monkeypatch.setattr(
        "backend.data.sources.akshare_source._retry_call",
        no_retry,
    )

    result = asyncio.run(
        AKShareSource("eastmoney").fetch_daily_result(
            ["000001"],
            "2024-01-01",
            "2024-01-31",
        )
    )

    assert result.frame.empty
    assert calls == {"eastmoney": 1, "sina": 0}
    assert result.evidence["provider"] == "akshare:eastmoney"
    assert result.evidence["response"]["failed_codes"] == {
        "000001": "no_observations"
    }


def test_akshare_provider_name_is_explicitly_validated() -> None:
    with pytest.raises(ValueError, match="eastmoney, sina or tencent"):
        AKShareSource("automatic-fallback")


@pytest.mark.parametrize(
    ("price_adjustment", "request_adjustment"),
    [("raw", ""), ("qfq", "qfq"), ("hfq", "hfq")],
)
def test_akshare_tencent_request_normalization_and_evidence(
    monkeypatch: pytest.MonkeyPatch,
    price_adjustment: str,
    request_adjustment: str,
) -> None:
    calls: list[dict] = []

    def tencent_history(**kwargs):
        calls.append(dict(kwargs))
        return pd.DataFrame(
            {
                "date": ["2024-01-02", "2024-01-03"],
                "open": ["10.0", "10.2"],
                "close": ["10.1", "10.3"],
                "high": ["10.2", "10.4"],
                "low": ["9.9", "10.1"],
                "volume": ["1000", "1200"],
                "amount": ["10000", "12360"],
                "turnover": ["0.1", "0.12"],
            }
        )

    async def no_retry(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(stock_zh_a_hist_tx=tencent_history),
    )
    monkeypatch.setattr(
        "backend.data.sources.akshare_source._retry_call",
        no_retry,
    )

    result = asyncio.run(
        AKShareSource(
            "tencent",
            price_adjustment=price_adjustment,
        ).fetch_daily_result(
            ["600519"],
            "2024-01-02",
            "2024-01-03",
        )
    )

    assert calls == [
        {
            "symbol": "sh600519",
            "start_date": "20240102",
            "end_date": "20240103",
            "adjust": request_adjustment,
        }
    ]
    assert list(result.frame.columns.get_level_values("field").unique()) == [
        "amount",
        "close",
        "high",
        "low",
        "open",
        "volume",
    ]
    assert result.frame.loc[pd.Timestamp("2024-01-03"), ("600519", "close")] == 10.3
    assert result.evidence["provider"] == "akshare:tencent"
    assert (
        result.evidence["endpoint"]
        == "akshare.stock_zh_a_hist_tx/tencent"
    )
    assert result.evidence["adjustment"] == price_adjustment
    assert result.evidence["evidence_level"] == "public_aggregator"
    assert result.evidence["complete_code_coverage"] is True


def test_akshare_tencent_transport_outage_opens_provider_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def exhausted_transport(func, *args, **kwargs):
        del func, args
        calls.append(str(kwargs["symbol"]))
        raise AKShareCallError(
            "stock_zh_a_hist_tx",
            3,
            ConnectionError("Remote end closed connection without response"),
        )

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(stock_zh_a_hist_tx=lambda **kwargs: kwargs),
    )
    monkeypatch.setattr(
        "backend.data.sources.akshare_source._retry_call",
        exhausted_transport,
    )
    source = AKShareSource("tencent", price_adjustment="hfq")

    with pytest.raises(ProviderOutageError, match="akshare:tencent"):
        asyncio.run(
            source.fetch_daily_result(
                ["000001", "000002"],
                "2024-01-01",
                "2024-01-31",
            )
        )
    with pytest.raises(ProviderOutageError, match="akshare:tencent"):
        asyncio.run(
            source.fetch_daily_result(
                ["000003"],
                "2024-01-01",
                "2024-01-31",
            )
        )

    assert calls == ["sz000001"]


def test_public_research_source_validates_raw_before_building_hfq() -> None:
    source = build_public_research_source()

    assert isinstance(source, CrossValidatedDailySource)
    assert isinstance(source.primary, BaoStockSource)
    assert isinstance(source.reference, AKShareSource)
    assert source.reference.preferred_provider == "sina"
    assert source.primary.price_adjustment == "raw"
    assert source.reference.price_adjustment == "raw"
    assert source.adjusted_reference is not None
    assert source.adjusted_reference.price_adjustment == "hfq"

    validation = compare_independent_daily_frames(
        _frame(),
        _frame(multiplier=127.0),
        primary_provider="baostock:official",
        reference_provider="akshare:sina",
        requested_codes=["000001"],
        adjustment="raw",
        min_overlap_returns=20,
        return_abs_tolerance=1e-10,
    )
    require_cross_source_acceptance(validation)
    assert validation["summary"]["acceptable"] is True
    assert validation["policy"]["comparison"] == "scale_invariant_ohlcv"


def test_hfq_factor_validation_ignores_only_unobserved_sparse_panel_cells() -> None:
    first = _frame()
    close = first[("000001", "close")]
    first[("000001", "preclose")] = close.shift(1).fillna(close.iloc[0])
    first[("000001", "amount")] = (
        first[("000001", "close")] * first[("000001", "volume")]
    )
    second = first.copy()
    second.columns = pd.MultiIndex.from_tuples(
        [("000002", field) for _, field in second.columns],
        names=["code", "field"],
    )
    second.iloc[:10] = float("nan")
    raw = pd.concat([first, second], axis=1).sort_index(axis=1)

    adjusted, evidence = rebuild_hfq_panel(raw)

    assert adjusted[("000002", "close")].iloc[:10].isna().all()
    assert evidence["recurrence_validated"] is True
    assert evidence["factors_finite_positive"] is True


def test_validated_raw_is_adjusted_only_after_strict_acceptance() -> None:
    raw = _frame()
    close = raw[("000001", "close")]
    raw[("000001", "preclose")] = close.shift(1).fillna(close.iloc[0])
    raw[("000001", "amount")] = (
        raw[("000001", "close")] * raw[("000001", "volume")]
    )
    raw.sort_index(axis=1, inplace=True)
    adjusted, _ = rebuild_hfq_panel(raw)
    differing_hfq = adjusted.copy()
    differing_hfq.loc[
        differing_hfq.index[25],
        ("000001", "close"),
    ] *= 1.1

    class Source:
        def __init__(
            self,
            provider: str,
            frame: pd.DataFrame,
            adjustment: str,
            *,
            adjuster: bool = False,
        ) -> None:
            self.provider = provider
            self.frame = frame
            self.adjustment = adjustment
            if adjuster:
                self.adjust_validated_raw = rebuild_hfq_panel

        async def fetch_daily_result(self, codes, start, end):
            return DailyFetchResult(
                self.frame,
                build_daily_fetch_evidence(
                    self.frame,
                    requested_codes=codes,
                    start=start,
                    end=end,
                    provider=self.provider,
                    endpoint=f"{self.provider}/daily",
                    adjustment=self.adjustment,
                    evidence_level="public_aggregator",
                ),
            )

    result = asyncio.run(
        CrossValidatedDailySource(
            Source(
                "baostock:official",
                raw,
                "raw",
                adjuster=True,
            ),
            Source("akshare:sina", raw, "raw"),
            adjusted_reference=Source(
                "akshare:sina",
                differing_hfq,
                "hfq",
            ),
            return_abs_tolerance=1e-12,
        ).fetch_daily_result(
            ["000001"],
            "2024-01-02",
            "2024-02-19",
        )
    )

    assert result.evidence["adjustment"] == "hfq"
    assert result.evidence["cross_validation"]["adjustment"] == "raw"
    assert result.evidence["cross_validation"]["summary"]["acceptable"] is True
    factor = result.evidence["adjustment_validation"]
    assert factor["recurrence_validated"] is True
    assert factor["factors_finite_positive"] is True
    assert (
        factor["informational_hfq_cross_source"]["summary"]["acceptable"]
        is False
    )
    assert result.evidence["raw_cross_validated"] is True
    assert result.evidence["adjusted_factor_validated"] is True


def test_baostock_hfq_recurrence_filters_suspensions_and_preserves_returns() -> None:
    raw = pd.DataFrame(
        {
            "date": [
                "2024-06-10",
                "2024-06-11",
                "2024-06-12",
                "2024-06-13",
            ],
            "open": ["10", "10", "9.1", "9.3"],
            "high": ["10.2", "10", "9.3", "9.5"],
            "low": ["9.8", "10", "9.0", "9.2"],
            "close": ["10", "10", "9.2", "9.4"],
            "preclose": ["9.9", "10", "9.0", "9.2"],
            "volume": ["100", "0", "120", "130"],
            "amount": ["1000", "0", "1104", "1222"],
            "adjustflag": ["3", "3", "3", "3"],
            "tradestatus": ["1", "0", "1", "1"],
        }
    )

    rebuilt = _rebuild_hfq(raw, "000001").set_index("date")

    assert list(rebuilt.index) == [
        pd.Timestamp("2024-06-10"),
        pd.Timestamp("2024-06-12"),
        pd.Timestamp("2024-06-13"),
    ]
    # Corporate-action preclose=9 changes the factor from 1 to 10/9.
    assert rebuilt.loc[pd.Timestamp("2024-06-12"), "close"] == pytest.approx(
        9.2 * 10 / 9
    )
    assert rebuilt["close"].pct_change().iloc[1] == pytest.approx(9.2 / 9 - 1)
    assert rebuilt.loc[pd.Timestamp("2024-06-12"), "volume"] == 120


def test_baostock_adapter_requests_raw_data_and_emits_hfq_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str]] = []

    class Response:
        error_code = "0"
        error_msg = "success"

        def __init__(self) -> None:
            self.fields = [
                "date",
                "code",
                "open",
                "high",
                "low",
                "close",
                "preclose",
                "volume",
                "amount",
                "adjustflag",
                "tradestatus",
                "pctChg",
            ]
            self.rows = [
                [
                    "2024-01-02",
                    "sh.600519",
                    "10",
                    "10.2",
                    "9.9",
                    "10.1",
                    "10",
                    "100",
                    "1010",
                    "3",
                    "1",
                    "1",
                ],
                [
                    "2024-01-03",
                    "sh.600519",
                    "10.1",
                    "10.4",
                    "10",
                    "10.3",
                    "10.1",
                    "120",
                    "1236",
                    "3",
                    "1",
                    "1.98",
                ],
            ]
            self.position = -1

        def next(self) -> bool:
            self.position += 1
            return self.position < len(self.rows)

        def get_row_data(self) -> list[str]:
            return self.rows[self.position]

    def query(*args, **kwargs):
        calls.append({"symbol": args[0], **kwargs})
        return Response()

    success = SimpleNamespace(error_code="0", error_msg="success")
    monkeypatch.setitem(
        sys.modules,
        "baostock",
        SimpleNamespace(
            login=lambda: success,
            logout=lambda: success,
            query_history_k_data_plus=query,
        ),
    )

    result = asyncio.run(
        BaoStockSource().fetch_daily_result(
            ["600519"],
            "2024-01-02",
            "2024-01-03",
        )
    )

    assert calls[0]["symbol"] == "sh.600519"
    assert calls[0]["adjustflag"] == "3"
    assert result.evidence["provider"] == "baostock:official"
    assert result.evidence["adjustment"] == "hfq"
    assert "hfq_ohlc=raw_ohlc*hfq_factor" in result.evidence["transformations"]
    assert result.evidence["complete_code_coverage"] is True
    assert result.frame.loc[
        pd.Timestamp("2024-01-03"), ("600519", "close")
    ] == pytest.approx(10.3)

    raw_result = asyncio.run(
        BaoStockSource(price_adjustment="raw").fetch_daily_result(
            ["600519"],
            "2024-01-02",
            "2024-01-03",
        )
    )
    assert raw_result.evidence["adjustment"] == "raw"
    assert raw_result.frame.loc[
        pd.Timestamp("2024-01-03"), ("600519", "preclose")
    ] == pytest.approx(10.1)
    adjusted, factor_evidence = rebuild_hfq_panel(raw_result.frame)
    assert adjusted.loc[
        pd.Timestamp("2024-01-03"), ("600519", "close")
    ] == pytest.approx(10.3)
    assert factor_evidence["recurrence_validated"] is True
    assert factor_evidence["factors_finite_positive"] is True


def test_cross_source_allows_joint_suspension_gaps_but_not_partial_rows() -> None:
    primary = pd.concat([_frame(), _frame(code="000002")], axis=1)
    reference = primary.copy() * 17
    suspension_date = primary.index[10]
    primary.loc[suspension_date, "000002"] = float("nan")
    reference.loc[suspension_date, "000002"] = float("nan")

    accepted = compare_independent_daily_frames(
        primary,
        reference,
        primary_provider="feed-a",
        reference_provider="feed-b",
        requested_codes=["000001", "000002"],
        adjustment="hfq",
    )
    require_cross_source_acceptance(accepted)

    reference.loc[suspension_date, ("000002", "volume")] = 100
    rejected = compare_independent_daily_frames(
        primary,
        reference,
        primary_provider="feed-a",
        reference_provider="feed-b",
        requested_codes=["000001", "000002"],
        adjustment="hfq",
    )
    assert rejected["summary"]["acceptable"] is False
    assert rejected["per_code"][1]["status"] == "non_finite_or_nan_values"


@pytest.mark.parametrize(
    ("field", "multiplier", "diagnostic"),
    [
        ("high", 1.02, "geometry_conflicts"),
        ("volume", 2.0, "volume_conflicts"),
    ],
)
def test_cross_source_ohlcv_diagnostics_block_non_close_corruption(
    field: str,
    multiplier: float,
    diagnostic: str,
) -> None:
    reference = _frame(multiplier=100)
    reference.loc[reference.index[25], ("000001", field)] *= multiplier

    evidence = compare_independent_daily_frames(
        _frame(),
        reference,
        primary_provider="feed-a",
        reference_provider="feed-b",
        requested_codes=["000001"],
        adjustment="hfq",
    )

    assert evidence["summary"]["acceptable"] is False
    assert evidence["per_code"][0][diagnostic] >= 1


def test_akshare_transport_outage_opens_circuit_and_aborts_remaining_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def exhausted_transport(func, *args, **kwargs):
        del func, args
        calls.append(str(kwargs["symbol"]))
        raise AKShareCallError(
            "stock_zh_a_hist",
            3,
            ConnectionError("Remote end closed connection without response"),
        )

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(stock_zh_a_hist=lambda **kwargs: kwargs),
    )
    monkeypatch.setattr(
        "backend.data.sources.akshare_source._retry_call",
        exhausted_transport,
    )
    source = AKShareSource("eastmoney")

    with pytest.raises(ProviderOutageError, match="provider outage"):
        asyncio.run(
            source.fetch_daily_result(
                ["000001", "000002", "000003"],
                "2024-01-01",
                "2024-01-31",
            )
        )
    with pytest.raises(ProviderOutageError, match="provider outage"):
        asyncio.run(
            source.fetch_daily_result(
                ["000004"],
                "2024-01-01",
                "2024-01-31",
            )
        )

    assert calls == ["000001"]


@pytest.mark.parametrize(
    "tls_error",
    [
        ssl.SSLEOFError(
            8,
            "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred",
        ),
        ssl.SSLCertVerificationError(
            1,
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed",
        ),
    ],
)
def test_akshare_tls_failures_open_provider_circuit(
    monkeypatch: pytest.MonkeyPatch,
    tls_error: ssl.SSLError,
) -> None:
    calls: list[str] = []

    async def exhausted_tls(func, *args, **kwargs):
        del func, args
        calls.append(str(kwargs["symbol"]))
        # Match the observed requests.exceptions.SSLError(SSLEOFError(...))
        # shape. Requests stores the TLS error as an argument, not a cause.
        outer = RequestsSSLError(tls_error)
        raise AKShareCallError("stock_zh_a_hist", 3, outer)

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(stock_zh_a_hist=lambda **kwargs: kwargs),
    )
    monkeypatch.setattr(
        "backend.data.sources.akshare_source._retry_call",
        exhausted_tls,
    )
    source = AKShareSource("eastmoney")

    with pytest.raises(ProviderOutageError, match="provider outage"):
        asyncio.run(
            source.fetch_daily_result(
                ["000001", "000002"],
                "2024-01-01",
                "2024-01-31",
            )
        )

    assert calls == ["000001"]


def test_empty_symbol_and_unsupported_cdr_do_not_open_provider_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def empty_history(**kwargs):
        calls.append(str(kwargs["symbol"]))
        return pd.DataFrame()

    async def no_retry(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(stock_zh_a_hist=empty_history),
    )
    monkeypatch.setattr(
        "backend.data.sources.akshare_source._retry_call",
        no_retry,
    )
    source = AKShareSource("eastmoney")

    first = asyncio.run(
        source.fetch_daily_result(
            ["000001", "689001"],
            "2024-01-01",
            "2024-01-31",
        )
    )
    second = asyncio.run(
        source.fetch_daily_result(
            ["000002"],
            "2024-01-01",
            "2024-01-31",
        )
    )

    assert first.frame.empty
    assert second.frame.empty
    assert calls == ["000001", "000002"]


def test_primary_outage_never_falls_through_to_reference_source() -> None:
    class OutageSource:
        async def fetch_daily_result(self, codes, start, end):
            del codes, start, end
            raise ProviderOutageError("primary unavailable")

    class ReferenceSource:
        def __init__(self) -> None:
            self.calls = 0

        async def fetch_daily_result(self, codes, start, end):
            del codes, start, end
            self.calls += 1
            raise AssertionError("reference must not be used as a fallback")

    reference = ReferenceSource()
    source = CrossValidatedDailySource(OutageSource(), reference)

    with pytest.raises(ProviderOutageError, match="primary unavailable"):
        asyncio.run(
            source.fetch_daily_result(
                ["000001"],
                "2024-01-01",
                "2024-01-31",
            )
        )
    assert reference.calls == 0


def test_akshare_native_calls_are_process_serialized() -> None:
    active = 0
    max_active = 0
    counter_lock = threading.Lock()

    def native_call(value: int) -> int:
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.01)
        with counter_lock:
            active -= 1
        return value

    async def run_calls() -> list[int]:
        return await asyncio.gather(
            *[_run_sync(native_call, value) for value in range(4)]
        )

    assert asyncio.run(run_calls()) == [0, 1, 2, 3]
    assert max_active == 1


def test_cross_validated_sources_are_fetched_sequentially() -> None:
    active = 0
    max_active = 0
    order: list[str] = []

    class Source:
        def __init__(self, provider: str) -> None:
            self.provider = provider

        async def fetch_daily_result(
            self,
            codes: list[str],
            start: str,
            end: str,
        ):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            order.append(f"{self.provider}:start")
            await asyncio.sleep(0)
            frame = _frame()
            evidence = build_daily_fetch_evidence(
                frame,
                requested_codes=codes,
                start=start,
                end=end,
                provider=self.provider,
                endpoint=f"{self.provider}/daily",
                adjustment="qfq",
                evidence_level="declared",
                transformations=(
                    ["test:scale-invariant"]
                    if self.provider == "feed-a"
                    else None
                ),
            )
            order.append(f"{self.provider}:end")
            active -= 1
            from backend.data.source_validation import DailyFetchResult

            return DailyFetchResult(frame, evidence)

    async def fetch():
        source = CrossValidatedDailySource(
            Source("feed-a"),
            Source("feed-b"),
        )
        return await source.fetch_daily_result(
            ["000001"],
            "2024-01-02",
            "2024-02-19",
        )

    result = asyncio.run(fetch())
    assert not result.frame.empty
    assert max_active == 1
    assert order == [
        "feed-a:start",
        "feed-a:end",
        "feed-b:start",
        "feed-b:end",
    ]
    assert result.evidence["transformations"] == ["test:scale-invariant"]


@pytest.mark.parametrize(
    ("primary", "reference", "requested", "status"),
    [
        (_frame(), _frame(code="000002"), ["000001"], "missing_reference_code"),
        (
            _frame().drop(columns=[("000001", "volume")]),
            _frame(),
            ["000001"],
            "missing_required_fields",
        ),
    ],
)
def test_cross_source_required_coverage_and_fields_fail_closed(
    primary: pd.DataFrame,
    reference: pd.DataFrame,
    requested: list[str],
    status: str,
) -> None:
    evidence = compare_independent_daily_frames(
        primary,
        reference,
        primary_provider="feed-a",
        reference_provider="feed-b",
        requested_codes=requested,
        adjustment="qfq",
    )
    assert evidence["summary"]["acceptable"] is False
    assert evidence["per_code"][0]["status"] == status


def test_cross_source_nan_cannot_hide_a_conflict_day() -> None:
    primary = _frame()
    reference = _frame()
    primary.loc[primary.index[20], ("000001", "close")] = float("nan")
    reference.loc[reference.index[20], ("000001", "close")] = float("nan")

    evidence = compare_independent_daily_frames(
        primary,
        reference,
        primary_provider="feed-a",
        reference_provider="feed-b",
        requested_codes=["000001"],
        adjustment="qfq",
    )

    assert evidence["summary"]["acceptable"] is False
    assert evidence["per_code"][0]["status"] == "non_finite_or_nan_values"


def test_reference_only_unrequested_code_fails_closed() -> None:
    reference = pd.concat([_frame(), _frame(code="000002")], axis=1)
    evidence = compare_independent_daily_frames(
        _frame(),
        reference,
        primary_provider="feed-a",
        reference_provider="feed-b",
        requested_codes=["000001"],
        adjustment="qfq",
    )
    assert evidence["summary"]["acceptable"] is False
    assert evidence["summary"]["unexpected_reference_codes"] == ["000002"]


def _rehash(payload: dict) -> dict:
    unsigned = dict(payload)
    unsigned.pop("content_sha256", None)
    payload["content_sha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return payload


def test_rehashed_authoritative_claim_is_rejected_by_adapter_policy() -> None:
    forged = deepcopy(_evidence(_frame()))
    forged["evidence_level"] = "exchange_authoritative"
    _rehash(forged)

    with pytest.raises(SourceEvidenceError, match="not permitted"):
        validate_daily_fetch_evidence(forged)


def test_rehashed_cache_derived_fields_are_recomputed() -> None:
    provenance = build_cache_source_provenance(
        _frame(),
        [_evidence(_frame())],
    )
    provenance["endpoints"] = ["forged/endpoint"]
    _rehash(provenance)

    with pytest.raises(SourceEvidenceError, match="endpoints conflict"):
        validate_cache_source_provenance(provenance)


def test_different_wrappers_for_same_upstream_are_not_independent() -> None:
    with pytest.raises(SourceEvidenceError, match="upstream identities"):
        compare_independent_daily_frames(
            _frame(),
            _frame(),
            primary_provider="akshare:eastmoney",
            reference_provider="akshare:eastmoney-legacy-wrapper",
            requested_codes=["000001"],
            adjustment="qfq",
        )


def test_cache_rejects_same_provider_with_different_endpoints() -> None:
    first = _evidence(_frame())
    second = build_daily_fetch_evidence(
        _frame(),
        requested_codes=["000001"],
        start="2024-01-02",
        end="2024-02-19",
        provider="feed-a",
        endpoint="feed-a/alternate",
        adjustment="qfq",
        evidence_level="declared",
    )
    provenance = build_cache_source_provenance(
        _frame(),
        [first, second],
    )
    assert provenance["identity_consistent"] is False
