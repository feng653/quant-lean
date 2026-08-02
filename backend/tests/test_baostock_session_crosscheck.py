from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import collect_baostock_session_crosscheck as crosscheck_cli

from backend.data.provider_artifacts import ContentAddressedProviderArtifactStore
from backend.data.sources.baostock_session_crosscheck import (
    BAOSTOCK_CROSSCHECK_INPUT_SCHEMA,
    BAOSTOCK_CROSSCHECK_REPORT_SCHEMA,
    BaoStockCrosscheckError,
    BaoStockCrosscheckPlan,
    BaoStockSessionCrosscheckCollector,
    SessionBlockerPair,
    annotate_tushare_session_reconciliation,
    classify_baostock_session,
)


class FakeResult:
    def __init__(
        self,
        rows: list[list[str]],
        *,
        error_code: str = "0",
        fields: list[str] | None = None,
        error_msg: str = "",
    ) -> None:
        self.error_code = error_code
        self.error_msg = error_msg
        self.fields = fields or ["date", "code", "volume", "amount", "tradestatus"]
        self._rows = rows
        self._index = -1

    def next(self) -> bool:
        self._index += 1
        return self._index < len(self._rows)

    def get_row_data(self) -> list[str]:
        return self._rows[self._index]


class FakeSdk:
    def __init__(
        self,
        results: dict[tuple[str, str], FakeResult],
        *,
        login_code: str = "0",
        login_message: str = "",
        query_raises: Exception | None = None,
    ) -> None:
        self.results = results
        self.login_code = login_code
        self.login_message = login_message
        self.query_raises = query_raises
        self.login_calls = 0
        self.logout_calls = 0
        self.query_calls: list[tuple[str, str]] = []

    def login(self) -> Any:
        self.login_calls += 1
        return SimpleNamespace(
            error_code=self.login_code,
            error_msg=self.login_message,
        )

    def logout(self) -> Any:
        self.logout_calls += 1
        return SimpleNamespace(error_code="0")

    def query_history_k_data_plus(
        self,
        code: str,
        fields: str,
        *,
        start_date: str,
        end_date: str,
        frequency: str,
        adjustflag: str,
    ) -> FakeResult:
        assert fields == "date,code,volume,amount,tradestatus"
        assert start_date == end_date
        assert frequency == "d"
        assert adjustflag == "3"
        self.query_calls.append((code, start_date))
        if self.query_raises is not None:
            raise self.query_raises
        return self.results[(code, start_date)]


class NoisyResult(FakeResult):
    def next(self) -> bool:
        print("SDK result stdout token=must-not-leak")
        print("SDK result stderr password=must-not-leak", file=sys.stderr)
        return super().next()


class NoisySdk(FakeSdk):
    def login(self) -> Any:
        print("login failed! token=must-not-leak")
        print("login stderr password=must-not-leak", file=sys.stderr)
        return super().login()

    def logout(self) -> Any:
        print("logout stdout token=must-not-leak")
        print("logout stderr password=must-not-leak", file=sys.stderr)
        return super().logout()

    def query_history_k_data_plus(self, *args: Any, **kwargs: Any) -> FakeResult:
        print("query stdout token=must-not-leak")
        print("query stderr password=must-not-leak", file=sys.stderr)
        return super().query_history_k_data_plus(*args, **kwargs)


def _pair(
    code: str = "000002.SZ",
    trade_date: str = "2016-01-04",
    reason: str = "suspend_without_daily_semantics_ambiguous",
) -> SessionBlockerPair:
    return SessionBlockerPair(code, trade_date, reason)


def _plan(*pairs: SessionBlockerPair) -> BaoStockCrosscheckPlan:
    return BaoStockCrosscheckPlan(tuple(sorted(pairs)))


def _collector(
    tmp_path: Path,
    sdk: FakeSdk,
    plan: BaoStockCrosscheckPlan,
    *,
    max_calls: int = 8,
) -> BaoStockSessionCrosscheckCollector:
    return BaoStockSessionCrosscheckCollector(
        sdk=sdk,
        store=ContentAddressedProviderArtifactStore(tmp_path / "evidence"),
        plan=plan,
        max_calls=max_calls,
    )


def test_plan_rejects_credentials_and_noncanonical_pairs() -> None:
    with pytest.raises(BaoStockCrosscheckError, match="unsupported fields"):
        BaoStockCrosscheckPlan.from_document(
            {
                "schema_version": BAOSTOCK_CROSSCHECK_INPUT_SCHEMA,
                "blocker_pairs": [],
                "token": "must-not-be-accepted",
            }
        )
    with pytest.raises(BaoStockCrosscheckError, match="only code, date, and reason"):
        BaoStockCrosscheckPlan.from_document(
            {
                "schema_version": BAOSTOCK_CROSSCHECK_INPUT_SCHEMA,
                "blocker_pairs": [
                    {
                        "ts_code": "000002.SZ",
                        "trade_date": "2016-01-04",
                        "tushare_reason": "daily_suspend_semantics_ambiguous",
                        "password": "must-not-be-accepted",
                    }
                ],
            }
        )
    with pytest.raises(BaoStockCrosscheckError, match="canonically sorted"):
        BaoStockCrosscheckPlan((_pair("000003.SZ"), _pair("000002.SZ")))


def test_bounded_collection_records_exact_rows_and_resumes(tmp_path: Path) -> None:
    first = _pair()
    second = _pair(
        "600000.SH", "2016-01-05", "daily_suspend_semantics_ambiguous"
    )
    sdk = FakeSdk(
        {
            (first.baostock_code, first.trade_date): FakeResult(
                [[first.trade_date, first.baostock_code, "0", "0", "0"]]
            ),
            (second.baostock_code, second.trade_date): FakeResult(
                [[second.trade_date, second.baostock_code, "100", "1000", "1"]]
            ),
        }
    )
    collector = _collector(tmp_path, sdk, _plan(first, second), max_calls=1)

    first_report = collector.run()
    second_report = collector.run()

    assert first_report["progress"] == {
        "calls_this_invocation": 1,
        "max_calls_per_invocation": 1,
        "planned_pairs": 2,
        "completed_pairs": 1,
        "pending_pairs": 1,
        "complete": False,
    }
    assert second_report["progress"]["complete"] is True
    assert sdk.login_calls == 2
    assert sdk.logout_calls == 2
    assert len(sdk.query_calls) == 2
    assert second_report["classification"] == "quarantine"
    assert second_report["production_pit_ready"] is False
    assert second_report["runtime_data_changed"] is False
    assert all(
        observation["tushare_blocker_resolved"] is False
        for observation in second_report["observations"]
    )
    first_observation = next(
        row
        for row in second_report["observations"]
        if row["pair"]["ts_code"] == first.ts_code
    )
    assert first_observation["comparison"] == (
        "candidate_supports_non_trading_interpretation"
    )
    receipt = second_report["observations"][0]["receipt"]
    manifest, payload = collector.store.read(receipt["manifest_sha256"])
    stored = json.loads(payload)
    assert manifest["provider"] == "baostock"
    assert manifest["classification"] == "quarantine"
    assert manifest["bitemporal"]["available_at"]["evidence"] == (
        "declared_ingestion_time"
    )
    assert manifest["bitemporal"]["revision"]["evidence"] == (
        "declared_observation"
    )
    assert stored["rows"] == [
        [first.trade_date, first.baostock_code, "0", "0", "0"]
    ]


def test_login_failure_is_sanitized_and_does_not_logout(tmp_path: Path) -> None:
    sdk = FakeSdk(
        {},
        login_code="10001001",
        login_message="password=do-not-leak token=do-not-leak",
    )
    with pytest.raises(BaoStockCrosscheckError) as caught:
        _collector(tmp_path, sdk, _plan(_pair())).run()
    assert str(caught.value) == "BaoStock login failed"
    assert caught.value.diagnostic() == {
        "code": "baostock_login_failed",
        "retryable": False,
    }
    assert "do-not-leak" not in str(caught.value)
    assert sdk.logout_calls == 0


def test_query_exception_still_logs_out(tmp_path: Path) -> None:
    sdk = FakeSdk({}, query_raises=RuntimeError("token=must-not-leak"))
    with pytest.raises(BaoStockCrosscheckError) as caught:
        _collector(tmp_path, sdk, _plan(_pair())).run()
    assert str(caught.value) == "BaoStock session query failed"
    assert "must-not-leak" not in str(caught.value)
    assert sdk.login_calls == 1
    assert sdk.logout_calls == 1


def test_all_sdk_stdout_and_stderr_are_discarded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pair = _pair()
    sdk = NoisySdk(
        {
            (pair.baostock_code, pair.trade_date): NoisyResult(
                [[pair.trade_date, pair.baostock_code, "0", "0", "0"]]
            )
        }
    )

    report = _collector(tmp_path, sdk, _plan(pair)).run()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert report["progress"]["complete"] is True
    artifact = next((tmp_path / "evidence" / "artifacts" / "sha256").glob("*/*"))
    assert b"must-not-leak" not in artifact.read_bytes()


def test_cli_login_noise_preserves_single_json_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps(
            {
                "schema_version": BAOSTOCK_CROSSCHECK_INPUT_SCHEMA,
                "blocker_pairs": [_pair().public_scope()],
            }
        ),
        encoding="utf-8",
    )
    sdk = NoisySdk({}, login_code="10001001")
    monkeypatch.setitem(sys.modules, "baostock", sdk)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "collect_baostock_session_crosscheck.py",
            "--input",
            str(input_path),
            "--evidence-root",
            str(tmp_path / "evidence"),
        ],
    )

    assert crosscheck_cli.main() == 2

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "status": "failed",
        "diagnostic": {"code": "baostock_login_failed", "retryable": False},
    }
    assert captured.out.count("\n") == 1
    assert "must-not-leak" not in captured.out


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (
            {"volume": "0", "amount": "0", "tradestatus": "0"},
            "provider_reports_not_trading_without_liquidity",
        ),
        (
            {"volume": "100", "amount": "1000", "tradestatus": "1"},
            "provider_reports_trading_with_liquidity",
        ),
        (
            {"volume": "100", "amount": "0", "tradestatus": "0"},
            "provider_state_liquidity_conflict",
        ),
        (
            {"volume": "0", "amount": "0", "tradestatus": "1"},
            "provider_trading_without_positive_liquidity_ambiguous",
        ),
    ],
)
def test_session_classification_is_conservative(
    row: dict[str, str], expected: str
) -> None:
    result = classify_baostock_session([row])
    assert result["status"] == expected
    assert result["governance_review_required"] is True
    assert result["production_tradability_proven"] is False


def test_disagreement_annotation_never_removes_tushare_blocker(tmp_path: Path) -> None:
    pair = _pair()
    sdk = FakeSdk(
        {
            (pair.baostock_code, pair.trade_date): FakeResult(
                [[pair.trade_date, pair.baostock_code, "100", "1000", "1"]]
            )
        }
    )
    report = _collector(tmp_path, sdk, _plan(pair)).run()
    assert report["schema_version"] == BAOSTOCK_CROSSCHECK_REPORT_SCHEMA
    assert report["observations"][0]["comparison"] == (
        "candidate_disagrees_with_missing_daily_interpretation"
    )
    session = {
        "trade_date": pair.trade_date,
        "valid": False,
        "blockers": [
            {"code": pair.ts_code, "reason": pair.tushare_reason},
            {"code": "600000.SH", "reason": "daily_basic_missing"},
        ],
    }

    annotated = annotate_tushare_session_reconciliation(session, report)

    assert annotated["blockers"] == session["blockers"]
    assert annotated["valid"] is False
    optional = annotated["optional_baostock_crosscheck"]
    assert optional["tushare_blockers_changed"] is False
    assert optional["observations"][0]["tushare_blocker_resolved"] is False
    assert optional["observations"][0]["official_governance_review_required"] is True


def test_tampered_crosscheck_report_is_rejected(tmp_path: Path) -> None:
    pair = _pair()
    sdk = FakeSdk(
        {
            (pair.baostock_code, pair.trade_date): FakeResult(
                [[pair.trade_date, pair.baostock_code, "0", "0", "0"]]
            )
        }
    )
    report = _collector(tmp_path, sdk, _plan(pair)).run()
    report["production_pit_ready"] = True
    with pytest.raises(BaoStockCrosscheckError, match="digest changed"):
        annotate_tushare_session_reconciliation(
            {
                "trade_date": pair.trade_date,
                "valid": False,
                "blockers": [
                    {"code": pair.ts_code, "reason": pair.tushare_reason}
                ],
            },
            report,
        )
