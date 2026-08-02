"""Deterministic, fail-closed certification report for future live trading.

This module only inspects local declarations.  It never imports a broker SDK,
connects to an account, queries broker state, or submits an order.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
from pathlib import PurePath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.config import Settings, settings
from backend.execution.base import ExecutionAdapter
from backend.execution.registry import (
    ExecutionAdapterRegistry,
    get_execution_registry,
)

SCHEMA_VERSION = "live-readiness/v1"
CAPABILITY_VERSION = "2026-07-28.1"
CERTIFIED_ADAPTER_IDS: frozenset[str] = frozenset()
KNOWN_SCAFFOLD_ADAPTER_IDS = frozenset({"qmt", "ptrade"})


class CapabilityStatus(str, Enum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    MISSING = "missing"
    LOCKED = "locked"


class LiveCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_id: str
    label: str
    status: CapabilityStatus
    required: bool = True
    evidence: str
    source: str
    limitation: str | None = None


class ReadinessDomain(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain_id: str
    title: str
    status: Literal["available", "partial", "blocked"]
    capabilities: list[LiveCapability]


class ReadinessBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocker_id: str
    domain_id: str
    capability_id: str
    title: str
    evidence: str
    remediation: str


class AdapterReadinessEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter_id: str
    display_name: str
    recognized_scaffold: bool
    certified: bool = False
    health_status: str
    health_ready: bool = False
    health_message: str
    sdk_module: str | None = None
    sdk_available: bool = False
    missing_config: list[str] = Field(default_factory=list)
    declared_capabilities: dict[str, Any] = Field(default_factory=dict)
    fail_closed: bool
    blockers: list[str] = Field(default_factory=list)


class LiveReadinessReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    capability_version: str = CAPABILITY_VERSION
    ready: Literal[False] = False
    certification: Literal["not_certified"] = "not_certified"
    platform_scope: Literal["research_and_paper_trading_only"] = (
        "research_and_paper_trading_only"
    )
    summary: str
    blocker_count: int
    domains: list[ReadinessDomain]
    blockers: list[ReadinessBlocker]
    adapters: list[AdapterReadinessEvidence]
    limitations: list[str]


def _capability(
    capability_id: str,
    label: str,
    status: CapabilityStatus,
    evidence: str,
    source: str,
    limitation: str | None = None,
) -> LiveCapability:
    return LiveCapability(
        capability_id=capability_id,
        label=label,
        status=status,
        evidence=evidence,
        source=source,
        limitation=limitation,
    )


def _static_domains(config: Settings) -> list[ReadinessDomain]:
    live_db = str(PurePath(config.TRADING_LIVE_DB)).casefold()
    sim_db = str(PurePath(config.TRADING_SIM_DB)).casefold()
    db_is_separate = live_db != sim_db
    separate_db_status = (
        CapabilityStatus.PARTIAL if db_is_separate else CapabilityStatus.MISSING
    )
    separate_db_evidence = (
        "配置声明了与模拟盘不同的 TRADING_LIVE_DB 路径，但未启用实盘账本。"
        if db_is_separate
        else "TRADING_LIVE_DB 与 TRADING_SIM_DB 未隔离。"
    )

    domain_specs: list[
        tuple[str, str, list[LiveCapability]]
    ] = [
        (
            "data_integrity",
            "点时数据与公司行动",
            [
                _capability(
                    "point_in_time_universe",
                    "历史点时股票池",
                    CapabilityStatus.MISSING,
                    "当前缓存股票池不是可认证的历史成分股快照。",
                    "research data audit",
                    "存在幸存者偏差时，历史收益不能作为资金决策依据。",
                ),
                _capability(
                    "corporate_actions_adjustment",
                    "公司行动与复权审计",
                    CapabilityStatus.MISSING,
                    "未建立分红、拆并股、配股及复权因子的不可变审计链。",
                    "research data audit",
                ),
            ],
        ),
        (
            "market_rules",
            "A 股可交易性与市场规则",
            [
                _capability(
                    "suspension_st",
                    "停牌与 ST 状态",
                    CapabilityStatus.MISSING,
                    "执行链路没有经过认证的逐日停牌/ST 状态。",
                    "execution audit",
                ),
                _capability(
                    "ipo_delisting",
                    "IPO 与退市状态",
                    CapabilityStatus.MISSING,
                    "执行链路没有 IPO、退市整理期和摘牌状态机。",
                    "execution audit",
                ),
                _capability(
                    "price_limits",
                    "涨跌停与一字板约束",
                    CapabilityStatus.MISSING,
                    "尚未统一实现主板/创业板/科创板/北交所的价格限制和一字板拒单。",
                    "execution audit",
                ),
            ],
        ),
        (
            "execution_semantics",
            "统一撮合与交易成本",
            [
                _capability(
                    "unified_matching",
                    "回测、模拟、实盘统一撮合",
                    CapabilityStatus.MISSING,
                    "模拟盘与回测仍使用不同执行语义，实盘执行核心尚不存在。",
                    "execution audit",
                ),
                _capability(
                    "volume_partial_fill",
                    "成交量约束与部分成交",
                    CapabilityStatus.PARTIAL,
                    "回测引擎具备成交量参与率和部分成交，但模拟盘与未来实盘未共用。",
                    "backtest engine v2",
                ),
                _capability(
                    "impact_slippage",
                    "冲击成本与滑点压力",
                    CapabilityStatus.PARTIAL,
                    "研究层可做成本压力和容量分析，尚未绑定券商成交回报校准。",
                    "research robustness kernels",
                ),
            ],
        ),
        (
            "risk_controls",
            "实盘风控与故障处置",
            [
                _capability(
                    "kill_switch",
                    "独立停止交易开关",
                    CapabilityStatus.MISSING,
                    "不存在经过认证、与策略进程隔离的 kill switch。",
                    "live execution audit",
                ),
                _capability(
                    "pretrade_limits",
                    "订单、标的、账户限额",
                    CapabilityStatus.MISSING,
                    "没有实盘级资金、仓位、单笔、频率和价格偏离硬限额。",
                    "live execution audit",
                ),
                _capability(
                    "disconnect_state_machine",
                    "断线重连与订单状态机",
                    CapabilityStatus.MISSING,
                    "未实现断线重连、未知订单状态隔离和人工恢复流程。",
                    "live execution audit",
                ),
            ],
        ),
        (
            "security_operations",
            "凭证、传输与运行审计",
            [
                _capability(
                    "credential_tls_redaction",
                    "凭证托管、TLS 与日志脱敏",
                    CapabilityStatus.MISSING,
                    "券商凭证托管、双向认证、TLS 终止和字段级日志脱敏未验收。",
                    "security audit",
                ),
                _capability(
                    "separate_live_ledger",
                    "独立实盘账本",
                    separate_db_status,
                    separate_db_evidence,
                    "settings.TRADING_LIVE_DB",
                    "路径隔离不等于实盘数据库、事务和权限模型已验收。",
                ),
                _capability(
                    "backup_recovery",
                    "备份、恢复与灾难演练",
                    CapabilityStatus.MISSING,
                    "未提供实盘账本备份、恢复点目标和定期恢复演练证据。",
                    "operations audit",
                ),
            ],
        ),
        (
            "model_governance",
            "模型治理",
            [
                _capability(
                    "model_hash_approval",
                    "模型哈希与晋级审批",
                    CapabilityStatus.PARTIAL,
                    "研究产物具备哈希和晋级门禁，但尚未绑定到经过认证的实盘加载链。",
                    "research manifests and workflow",
                ),
                _capability(
                    "champion_rollback",
                    "Champion/Challenger 与回滚",
                    CapabilityStatus.PARTIAL,
                    "模拟部署已保留模型版本历史和失败保护，但没有实盘灰度、自动回滚和变更冻结验收。",
                    "deployment model version history",
                ),
                _capability(
                    "drift_monitoring",
                    "数据与模型漂移监控",
                    CapabilityStatus.MISSING,
                    "没有连接实盘数据的特征、预测和收益漂移告警。",
                    "model operations audit",
                ),
            ],
        ),
        (
            "time_calendar",
            "时钟与交易日历",
            [
                _capability(
                    "exchange_calendar",
                    "交易日历与临时休市",
                    CapabilityStatus.MISSING,
                    "未建立可审计的交易所日历、临时休市和集合竞价时段控制。",
                    "live execution audit",
                ),
                _capability(
                    "clock_sync",
                    "时钟同步与时间边界",
                    CapabilityStatus.MISSING,
                    "未验证 NTP 偏差、时区、交易截止时间和服务重启后的时钟安全。",
                    "operations audit",
                ),
            ],
        ),
    ]
    return [
        ReadinessDomain(
            domain_id=domain_id,
            title=title,
            status=_domain_status(capabilities),
            capabilities=capabilities,
        )
        for domain_id, title, capabilities in domain_specs
    ]


def _domain_status(capabilities: Iterable[LiveCapability]) -> str:
    statuses = {capability.status for capability in capabilities}
    if statuses <= {CapabilityStatus.AVAILABLE}:
        return "available"
    if CapabilityStatus.MISSING in statuses or CapabilityStatus.LOCKED in statuses:
        return "blocked"
    return "partial"


def _safe_adapter_evidence(adapter: ExecutionAdapter) -> AdapterReadinessEvidence:
    adapter_id = str(getattr(adapter, "adapter_id", "unknown") or "unknown")
    display_name = str(getattr(adapter, "display_name", adapter_id))
    recognized = adapter_id in KNOWN_SCAFFOLD_ADAPTER_IDS
    blockers: list[str] = []
    declared: dict[str, Any] = {}
    health_status = "unavailable"
    health_ready = False
    health_message = "适配器能力读取失败，按不可用处理。"
    sdk_module: str | None = None
    sdk_available = False
    missing_config: list[str] = []
    live_submission = False

    try:
        capabilities = adapter.capabilities
        declared = capabilities.model_dump(mode="json")
        live_submission = bool(capabilities.live_order_submission)
    except Exception as error:  # pragma: no cover - exact SDK adapter failures vary
        blockers.append(f"capabilities_error:{type(error).__name__}")

    try:
        health = adapter.health()
        health_status = str(
            health.status.value if hasattr(health.status, "value") else health.status
        )
        health_ready = bool(health.ready)
        health_message = health.message
        sdk_module_value = health.details.get("sdk_module")
        sdk_module = str(sdk_module_value) if sdk_module_value else None
        sdk_available = health.details.get("sdk_available") is True
        missing_config = sorted(
            str(item) for item in health.details.get("missing_config", [])
        )
    except Exception as error:  # pragma: no cover - defensive boundary
        blockers.append(f"health_error:{type(error).__name__}")

    if not recognized:
        blockers.append("unknown_adapter")
    if adapter_id not in CERTIFIED_ADAPTER_IDS:
        blockers.append("adapter_not_certified")
    if health_ready:
        blockers.append("health_declaration_not_live_certification")
    else:
        blockers.append("adapter_not_ready")
    if live_submission:
        blockers.append("uncertified_live_submission_declared")
    else:
        blockers.append("live_submission_locked")

    return AdapterReadinessEvidence(
        adapter_id=adapter_id,
        display_name=display_name,
        recognized_scaffold=recognized,
        health_status=health_status,
        health_ready=health_ready,
        health_message=health_message,
        sdk_module=sdk_module,
        sdk_available=sdk_available,
        missing_config=missing_config,
        declared_capabilities=declared,
        fail_closed=not live_submission and not health_ready,
        blockers=sorted(set(blockers)),
    )


def _adapter_domain(
    adapters: list[AdapterReadinessEvidence],
) -> ReadinessDomain:
    any_adapter = bool(adapters)
    all_locked = any_adapter and all(
        not item.declared_capabilities.get("live_order_submission", False)
        for item in adapters
    )
    account_declared = any(
        item.declared_capabilities.get("supports_account_query") is True
        for item in adapters
    )
    position_declared = any(
        item.declared_capabilities.get("supports_position_query") is True
        for item in adapters
    )
    local_validation = any(
        item.declared_capabilities.get("supports_order_validation") is True
        for item in adapters
    )
    cancel_declared = any(
        item.declared_capabilities.get("supports_order_cancel") is True
        for item in adapters
    )
    capabilities = [
        _capability(
            "adapter_certification",
            "券商适配器认证",
            CapabilityStatus.MISSING,
            (
                "已注册适配器均为未认证接入脚手架。"
                if any_adapter
                else "没有注册任何券商适配器。"
            ),
            "execution adapter registry",
        ),
        _capability(
            "account_position_query",
            "账户与持仓查询",
            (
                CapabilityStatus.PARTIAL
                if account_declared and position_declared
                else CapabilityStatus.MISSING
            ),
            "适配器声明查询接口，但当前健康检查不允许调用。"
            if account_declared and position_declared
            else "适配器未完整声明账户和持仓查询能力。",
            "adapter.capabilities + adapter.health",
        ),
        _capability(
            "local_order_validation",
            "本地订单协议预检",
            (
                CapabilityStatus.PARTIAL
                if local_validation
                else CapabilityStatus.MISSING
            ),
            "仅支持本地格式预检，不连接券商、不代表可提交。"
            if local_validation
            else "没有本地订单协议预检声明。",
            "adapter.capabilities",
        ),
        _capability(
            "live_order_submission",
            "真实订单提交",
            CapabilityStatus.LOCKED if all_locked else CapabilityStatus.MISSING,
            (
                "所有适配器 live_order_submission=false，真实提交保持锁定。"
                if all_locked
                else "出现未认证提交声明或没有可验证的适配器，必须阻断。"
            ),
            "adapter.capabilities.live_order_submission",
        ),
        _capability(
            "order_query_cancel",
            "订单查询与撤单",
            CapabilityStatus.PARTIAL if cancel_declared else CapabilityStatus.MISSING,
            "撤单仅有能力声明，订单查询和券商回报未认证。"
            if cancel_declared
            else "没有完整订单查询、撤单和状态确认能力。",
            "adapter.capabilities",
        ),
        _capability(
            "fill_stream_reconciliation",
            "成交回报与日终对账",
            CapabilityStatus.MISSING,
            "适配器协议没有成交推送、缺口回补和券商对账能力。",
            "execution adapter contract",
        ),
        _capability(
            "broker_idempotency",
            "券商级幂等键",
            CapabilityStatus.MISSING,
            "未建立 client_order_id 到券商订单的持久唯一映射和重放保护。",
            "execution adapter contract",
        ),
    ]
    return ReadinessDomain(
        domain_id="broker_lifecycle",
        title="券商订单全生命周期",
        status=_domain_status(capabilities),
        capabilities=capabilities,
    )


_REMEDIATION_BY_DOMAIN = {
    "data_integrity": "接入点时股票池和公司行动源，冻结数据快照并通过质量门禁。",
    "market_rules": "实现并回放验证 A 股逐日可交易性与交易所价格规则。",
    "execution_semantics": "让回测、模拟和实盘共用可认证撮合核心及成本模型。",
    "broker_lifecycle": "在测试账户完成订单全生命周期、幂等和逐笔对账验收。",
    "risk_controls": "实现独立硬风控、kill switch、断线状态机和人工恢复演练。",
    "security_operations": "完成凭证托管、TLS、脱敏、独立账本及备份恢复验收。",
    "model_governance": "绑定模型哈希、双人审批、灰度、回滚与漂移监控。",
    "time_calendar": "接入权威交易日历并验证时钟同步和所有交易时间边界。",
}


def build_live_readiness_report(
    *,
    registry: ExecutionAdapterRegistry | None = None,
    config: Settings = settings,
) -> LiveReadinessReport:
    """Build a side-effect-free, deterministic readiness report."""
    active_registry = registry or get_execution_registry()
    try:
        raw_adapters = active_registry.list()
    except Exception:
        raw_adapters = []
    adapters = sorted(
        (_safe_adapter_evidence(adapter) for adapter in raw_adapters),
        key=lambda item: item.adapter_id,
    )
    domains = _static_domains(config)
    domains.append(_adapter_domain(adapters))
    domains.sort(key=lambda item: item.domain_id)

    blockers = [
        ReadinessBlocker(
            blocker_id=f"{domain.domain_id}:{capability.capability_id}",
            domain_id=domain.domain_id,
            capability_id=capability.capability_id,
            title=capability.label,
            evidence=capability.evidence,
            remediation=_REMEDIATION_BY_DOMAIN[domain.domain_id],
        )
        for domain in domains
        for capability in domain.capabilities
        if capability.required and capability.status is not CapabilityStatus.AVAILABLE
    ]
    blockers.sort(key=lambda item: item.blocker_id)

    # No runtime declaration can self-certify this platform.  Certification is
    # a separately reviewed artifact and remains absent by design.
    return LiveReadinessReport(
        summary=(
            "实盘交易未认证且保持锁定。当前能力仅限研究、模拟盘和本地订单格式预检。"
        ),
        blocker_count=len(blockers),
        domains=domains,
        blockers=blockers,
        adapters=adapters,
        limitations=[
            "本报告不连接券商、不读取账户、不查询持仓、不访问网络。",
            "SDK 已安装或环境变量已配置不等于适配器通过实盘认证。",
            "模拟盘部署、模拟订单和模拟持仓不属于实盘能力。",
            "只有独立验收并发布新的 capability_version 后才能重新评估门禁。",
        ],
    )
