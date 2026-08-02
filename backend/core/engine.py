"""Signal-driven T+1 backtest execution with explicit fill constraints."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from collections.abc import Mapping, Set
from typing import Literal

import pandas as pd

from .cost_model import CostModel
from .types import BacktestResult, PositionSnapshot, SignalDict, TradeRecord

logger = logging.getLogger(__name__)

RebalanceMode = Literal["signal_driven", "monthly_liquidate_compat"]
PortfolioSignalMode = Literal["event_orders", "target_weights"]
MembershipExitPolicy = Literal[
    "research_next_session_open",
    "raw_effective_close_auction",
]


@dataclass(frozen=True)
class ExecutionConstraints:
    """Observable execution constraints applied to every order.

    ``volume_participation=None`` preserves the old unlimited-liquidity
    assumption.  When volume exists, zero or invalid volume is always treated
    as a suspension.  Enabling participation requires volume data and caps a
    fill to the configured fraction of that session's reported volume.
    """

    volume_participation: float | None = None
    lot_size: int = 100

    def __post_init__(self) -> None:
        if self.lot_size <= 0:
            raise ValueError("lot_size must be positive")
        if self.volume_participation is not None and not (
            0 < self.volume_participation <= 1
        ):
            raise ValueError("volume_participation must be in (0, 1]")


@dataclass
class _PortfolioState:
    cash: float = 0.0
    holdings: dict[str, int] = field(default_factory=dict)
    avg_costs: dict[str, float] = field(default_factory=dict)
    last_closes: dict[str, float] = field(default_factory=dict)


class BacktestEngine:
    """Event-driven backtest engine.

    Signals produced on T execute at the next trading session's open.  The
    default ``signal_driven`` mode never changes a holding without an explicit
    BUY or SELL signal.  ``monthly_liquidate_compat`` exists only for callers
    that deliberately opt into the legacy first-session liquidation behavior.

    ``portfolio_signal_mode="event_orders"`` leaves positions absent from a
    BUY batch untouched.  The explicit ``target_weights`` mode interprets each
    non-empty BUY batch as the complete desired portfolio, liquidates omitted
    or overweight positions subject to fill constraints, then buys deficits.
    No calendar-date heuristic is used to choose between those semantics.
    """

    def __init__(
        self,
        initial_capital: float,
        cost_model: CostModel,
        start_date: str,
        end_date: str,
        max_positions: int = 20,
        *,
        rebalance_mode: RebalanceMode = "signal_driven",
        portfolio_signal_mode: PortfolioSignalMode = "event_orders",
        execution_constraints: ExecutionConstraints | None = None,
        eligible_codes_by_date: Mapping[str, Set[str]] | None = None,
        membership_exit_policy: MembershipExitPolicy = (
            "research_next_session_open"
        ),
    ) -> None:
        if initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if max_positions <= 0:
            raise ValueError("max_positions must be positive")
        if rebalance_mode not in {
            "signal_driven",
            "monthly_liquidate_compat",
        }:
            raise ValueError("unsupported rebalance_mode")
        if portfolio_signal_mode not in {"event_orders", "target_weights"}:
            raise ValueError("unsupported portfolio_signal_mode")
        if membership_exit_policy not in {
            "research_next_session_open",
            "raw_effective_close_auction",
        }:
            raise ValueError("unsupported membership_exit_policy")
        if membership_exit_policy == "raw_effective_close_auction":
            raise ValueError(
                "raw_close_auction_execution_not_supported"
            )
        self.initial_capital = float(initial_capital)
        self.cost_model = cost_model
        self.start_date = pd.Timestamp(start_date)
        self.end_date = pd.Timestamp(end_date)
        self.max_positions = max_positions
        self.rebalance_mode = rebalance_mode
        self.portfolio_signal_mode = portfolio_signal_mode
        self.execution_constraints = (
            execution_constraints or ExecutionConstraints()
        )
        self.membership_exit_policy = membership_exit_policy
        self.eligible_codes_by_date = (
            {
                pd.Timestamp(day).strftime("%Y-%m-%d"): frozenset(
                    str(code) for code in codes
                )
                for day, codes in eligible_codes_by_date.items()
            }
            if eligible_codes_by_date is not None
            else None
        )

    def run(
        self,
        signals: SignalDict,
        pivot: pd.DataFrame,
        strategy_id: str = "",
        *,
        execution_pivot: pd.DataFrame | None = None,
    ) -> BacktestResult:
        """Execute signals and return equity, fills and position snapshots.

        ``pivot`` remains the adjusted research tape used to define signal
        sessions.  When supplied, ``execution_pivot`` is a distinct raw tape
        used for fills, volume constraints and mark-to-market values.  The
        engine never fills from the adjusted tape when an explicit raw role is
        present.
        """
        trading_days = self._get_trading_days(pivot)
        if len(trading_days) == 0:
            logger.warning("No trading sessions in the requested backtest window")
            return BacktestResult(
                equity_curve=pd.DataFrame(),
                trade_log=[],
                position_snapshots=[],
                final_equity=self.initial_capital,
                signals_generated=0,
                trades_executed=0,
            )

        execution_prices = execution_pivot if execution_pivot is not None else pivot
        if execution_pivot is not None:
            execution_days = pd.DatetimeIndex(
                pd.to_datetime(execution_pivot.index)
            ).sort_values().unique()
            missing_execution_days = trading_days.difference(execution_days)
            if len(missing_execution_days):
                raise ValueError(
                    "raw execution price role is missing research sessions"
                )

        state = _PortfolioState(cash=self.initial_capital)
        trade_log: list[TradeRecord] = []
        snapshots: list[PositionSnapshot] = []
        baseline_date = trading_days[0].normalize() - pd.Timedelta(days=1)
        equity_records: list[dict[str, object]] = [
            {
                "date": baseline_date,
                "equity": self.initial_capital,
                "cash": self.initial_capital,
            }
        ]
        signals_count = 0
        previous_month: tuple[int, int] | None = None

        all_pivot_days = pd.DatetimeIndex(
            pd.to_datetime(pivot.index)
        ).sort_values().unique()
        for index, today in enumerate(trading_days):
            today_str = today.strftime("%Y-%m-%d")
            month = (today.year, today.month)
            first_session_of_month = month != previous_month
            previous_month = month

            open_prices = self._get_field(execution_prices, today, "open")
            close_prices = self._get_close(execution_prices, today)
            volumes = self._get_optional_field(
                execution_prices,
                today,
                "volume",
            )

            if index > 0:
                signal_day = trading_days[index - 1]
            else:
                prior_days = all_pivot_days[all_pivot_days < today]
                signal_day = prior_days[-1] if len(prior_days) else None
            signal_date = (
                signal_day.strftime("%Y-%m-%d")
                if signal_day is not None
                else ""
            )
            today_signals = signals.get(signal_date, [])
            signals_count += len(today_signals)

            # Official after-close reconstitutions are represented by new
            # membership on this session. The compatibility policy force-sells
            # at this session's open on the adjusted research tape. It is not
            # an index-tracking close-auction fill or raw execution evidence.
            if self.eligible_codes_by_date is not None:
                eligible_today = self.eligible_codes_by_date.get(today_str)
                if eligible_today is None:
                    raise ValueError(
                        "point-in-time execution eligibility date is missing"
                    )
                for code in sorted(set(state.holdings) - set(eligible_today)):
                    trade = self._execute_sell(
                        state=state,
                        code=code,
                        requested_shares=state.holdings.get(code, 0),
                        execution_price=open_prices.get(code),
                        volumes=volumes,
                        date=today_str,
                        signal_date=signal_date,
                        strategy_id="point_in_time_universe_exit",
                        signal_score=0.0,
                    )
                    if trade is not None:
                        trade_log.append(trade)

            if (
                self.rebalance_mode == "monthly_liquidate_compat"
                and first_session_of_month
            ):
                for code in list(state.holdings):
                    trade = self._execute_sell(
                        state=state,
                        code=code,
                        requested_shares=state.holdings.get(code, 0),
                        execution_price=open_prices.get(code),
                        volumes=volumes,
                        date=today_str,
                        signal_date=signal_date,
                        strategy_id="monthly_liquidate_compat",
                        signal_score=0.0,
                    )
                    if trade is not None:
                        trade_log.append(trade)

            sell_signals = [
                signal
                for signal in today_signals
                if signal.action.upper() == "SELL"
            ]
            for signal in sell_signals:
                trade = self._execute_sell(
                    state=state,
                    code=signal.code,
                    requested_shares=state.holdings.get(signal.code, 0),
                    execution_price=open_prices.get(signal.code),
                    volumes=volumes,
                    date=today_str,
                    signal_date=signal_date,
                    strategy_id=strategy_id,
                    signal_score=signal.score,
                )
                if trade is not None:
                    trade_log.append(trade)

            sorted_buy_signals = sorted(
                (
                    signal
                    for signal in today_signals
                    if signal.action.upper() == "BUY"
                    and self._buy_is_eligible(
                        signal.code,
                        signal_date=signal_date,
                        execution_date=today_str,
                    )
                ),
                key=lambda signal: signal.score,
                reverse=True,
            )
            buy_signals = []
            seen_buy_codes: set[str] = set()
            for signal in sorted_buy_signals:
                if signal.code in seen_buy_codes:
                    continue
                seen_buy_codes.add(signal.code)
                buy_signals.append(signal)
                if len(buy_signals) >= self.max_positions:
                    break
            target_weights = self._target_weights(buy_signals)
            if buy_signals:
                portfolio_equity = self._portfolio_equity_at_open(
                    state,
                    open_prices,
                )
                if self.portfolio_signal_mode == "target_weights":
                    weights_by_code = {
                        signal.code: weight
                        for signal, weight in zip(
                            buy_signals,
                            target_weights,
                        )
                    }
                    for code, held_shares in list(state.holdings.items()):
                        execution_price = open_prices.get(code)
                        if not self._valid_price(execution_price):
                            continue
                        assert execution_price is not None
                        target_value = (
                            portfolio_equity
                            * weights_by_code.get(code, 0.0)
                        )
                        target_shares = self._round_lot(
                            int(target_value / execution_price)
                        )
                        excess_shares = max(
                            held_shares - target_shares,
                            0,
                        )
                        if excess_shares <= 0:
                            continue
                        trade = self._execute_sell(
                            state=state,
                            code=code,
                            requested_shares=excess_shares,
                            execution_price=execution_price,
                            volumes=volumes,
                            date=today_str,
                            signal_date=signal_date,
                            strategy_id=strategy_id,
                            signal_score=0.0,
                        )
                        if trade is not None:
                            trade_log.append(trade)
                for signal, target_weight in zip(
                    buy_signals,
                    target_weights,
                ):
                    already_held = state.holdings.get(signal.code, 0) > 0
                    if (
                        not already_held
                        and len(state.holdings) >= self.max_positions
                    ):
                        continue
                    execution_price = open_prices.get(signal.code)
                    if not self._valid_price(execution_price):
                        continue
                    assert execution_price is not None
                    current_value = (
                        execution_price
                        * state.holdings.get(signal.code, 0)
                    )
                    allocated_cash = min(
                        state.cash,
                        max(
                            portfolio_equity * target_weight - current_value,
                            0.0,
                        ),
                    )
                    requested_shares = self.cost_model.calc_shares(
                        allocated_cash,
                        execution_price,
                    )
                    executable_shares = self._executable_shares(
                        signal.code,
                        requested_shares,
                        volumes,
                    )
                    if executable_shares <= 0:
                        continue
                    total_cost = self.cost_model.calc_buy_cost(
                        execution_price,
                        executable_shares,
                    )
                    while (
                        executable_shares > 0
                        and total_cost > state.cash
                    ):
                        executable_shares -= (
                            self.execution_constraints.lot_size
                        )
                        total_cost = (
                            self.cost_model.calc_buy_cost(
                                execution_price,
                                executable_shares,
                            )
                            if executable_shares > 0
                            else 0.0
                        )
                    if executable_shares <= 0:
                        continue
                    old_shares = state.holdings.get(signal.code, 0)
                    old_cost = state.avg_costs.get(signal.code, 0.0)
                    new_shares = old_shares + executable_shares
                    state.cash -= total_cost
                    state.holdings[signal.code] = new_shares
                    state.avg_costs[signal.code] = (
                        old_cost * old_shares + total_cost
                    ) / new_shares
                    trade_log.append(
                        TradeRecord(
                            date=today_str,
                            signal_date=signal_date,
                            code=signal.code,
                            action="BUY",
                            price=execution_price,
                            shares=executable_shares,
                            amount=execution_price * executable_shares,
                            cost=(
                                total_cost
                                - execution_price * executable_shares
                            ),
                            signal_strategy=strategy_id,
                            signal_score=signal.score,
                        )
                    )

            total_market_value = 0.0
            for code, shares in state.holdings.items():
                current_close = close_prices.get(code)
                if self._valid_price(current_close):
                    assert current_close is not None
                    state.last_closes[code] = current_close
                close_price = state.last_closes.get(code, 0.0)
                if close_price <= 0:
                    continue
                market_value = close_price * shares
                average_cost = state.avg_costs.get(code, close_price)
                total_market_value += market_value
                snapshots.append(
                    PositionSnapshot(
                        date=today_str,
                        code=code,
                        shares=shares,
                        avg_cost=average_cost,
                        close_price=close_price,
                        market_value=market_value,
                        unrealized_pnl=(
                            close_price - average_cost
                        )
                        * shares,
                    )
                )

            equity_records.append(
                {
                    "date": today,
                    "equity": state.cash + total_market_value,
                    "cash": state.cash,
                }
            )

        equity_curve = pd.DataFrame(equity_records).set_index("date")
        return BacktestResult(
            equity_curve=equity_curve,
            trade_log=trade_log,
            position_snapshots=snapshots,
            final_equity=float(equity_curve["equity"].iloc[-1]),
            signals_generated=signals_count,
            trades_executed=len(trade_log),
        )

    def _buy_is_eligible(
        self,
        code: str,
        *,
        signal_date: str,
        execution_date: str,
    ) -> bool:
        if self.eligible_codes_by_date is None:
            return True
        signal_members = self.eligible_codes_by_date.get(signal_date)
        execution_members = self.eligible_codes_by_date.get(execution_date)
        if signal_members is None or execution_members is None:
            return False
        return code in signal_members and code in execution_members

    def _execute_sell(
        self,
        *,
        state: _PortfolioState,
        code: str,
        requested_shares: int,
        execution_price: float | None,
        volumes: dict[str, float] | None,
        date: str,
        signal_date: str,
        strategy_id: str,
        signal_score: float,
    ) -> TradeRecord | None:
        held = state.holdings.get(code, 0)
        if held <= 0 or not self._valid_price(execution_price):
            return None
        assert execution_price is not None
        shares = self._executable_shares(
            code,
            min(requested_shares, held),
            volumes,
        )
        if shares <= 0:
            return None
        proceeds = self.cost_model.calc_sell_cost(execution_price, shares)
        state.cash += proceeds
        remaining = held - shares
        if remaining > 0:
            state.holdings[code] = remaining
        else:
            state.holdings.pop(code, None)
            state.avg_costs.pop(code, None)
            state.last_closes.pop(code, None)
        return TradeRecord(
            date=date,
            signal_date=signal_date,
            code=code,
            action="SELL",
            price=execution_price,
            shares=shares,
            amount=execution_price * shares,
            cost=execution_price * shares - proceeds,
            signal_strategy=strategy_id,
            signal_score=signal_score,
        )

    def _executable_shares(
        self,
        code: str,
        requested_shares: int,
        volumes: dict[str, float] | None,
    ) -> int:
        if requested_shares <= 0:
            return 0
        lot_size = self.execution_constraints.lot_size
        requested = requested_shares // lot_size * lot_size
        if requested <= 0:
            return 0

        if volumes is not None:
            volume = volumes.get(code)
            if (
                volume is None
                or not math.isfinite(volume)
                or volume <= 0
            ):
                return 0
        else:
            volume = None

        participation = (
            self.execution_constraints.volume_participation
        )
        if participation is None:
            return requested
        if volume is None:
            return 0
        volume_cap = int(volume * participation)
        volume_cap = volume_cap // lot_size * lot_size
        return min(requested, max(volume_cap, 0))

    def _round_lot(self, shares: int) -> int:
        lot_size = self.execution_constraints.lot_size
        return max(shares // lot_size * lot_size, 0)

    @staticmethod
    def _target_weights(signals: list) -> list[float]:
        if not signals:
            return []
        weights = [max(float(signal.weight), 0.0) for signal in signals]
        weight_sum = sum(weights)
        if weight_sum <= 0:
            return [1.0 / len(signals)] * len(signals)
        if weight_sum > 1.000001:
            return [weight / weight_sum for weight in weights]
        return weights

    @staticmethod
    def _portfolio_equity_at_open(
        state: _PortfolioState,
        open_prices: dict[str, float],
    ) -> float:
        market_value = 0.0
        for code, shares in state.holdings.items():
            price = open_prices.get(code, state.last_closes.get(code, 0.0))
            if price > 0 and math.isfinite(price):
                market_value += price * shares
        return state.cash + market_value

    @staticmethod
    def _valid_price(price: float | None) -> bool:
        return (
            price is not None
            and math.isfinite(float(price))
            and float(price) > 0
        )

    def _get_trading_days(self, pivot: pd.DataFrame) -> pd.DatetimeIndex:
        days = (
            pivot.index
            if isinstance(pivot.index, pd.DatetimeIndex)
            else pd.to_datetime(pivot.index)
        )
        unique_days = pd.DatetimeIndex(days).sort_values().unique()
        return unique_days[
            (unique_days >= self.start_date)
            & (unique_days <= self.end_date)
        ]

    @staticmethod
    def _get_field(
        pivot: pd.DataFrame,
        day: pd.Timestamp,
        field_name: str,
    ) -> dict[str, float]:
        values = BacktestEngine._get_optional_field(
            pivot,
            day,
            field_name,
        )
        return values or {}

    @staticmethod
    def _get_optional_field(
        pivot: pd.DataFrame,
        day: pd.Timestamp,
        field_name: str,
    ) -> dict[str, float] | None:
        if day not in pivot.index:
            return None
        row = pivot.loc[day]
        normalized_field = field_name.lower()
        if isinstance(row, pd.DataFrame):
            matching_columns = [
                column
                for column in row.columns
                if str(column).lower() == normalized_field
            ]
            if not matching_columns:
                return None
            series = row[matching_columns[0]]
            return {
                str(code): float(value)
                for code, value in series.items()
                if not pd.isna(value)
            }
        if isinstance(row.index, pd.MultiIndex):
            matching = [
                (str(code), value)
                for (code, field), value in row.items()
                if str(field).lower() == normalized_field
            ]
            if not matching:
                return None
            return {
                code: float(value)
                for code, value in matching
                if not pd.isna(value)
            }
        return None

    @staticmethod
    def _get_close(
        pivot: pd.DataFrame,
        day: pd.Timestamp,
    ) -> dict[str, float]:
        closes = BacktestEngine._get_optional_field(
            pivot,
            day,
            "close",
        )
        if closes is not None:
            return {
                code: value
                for code, value in closes.items()
                if value > 0 and math.isfinite(value)
            }
        if day not in pivot.index:
            return {}
        row = pivot.loc[day]
        if isinstance(row, pd.Series) and not isinstance(
            row.index,
            pd.MultiIndex,
        ):
            return {
                str(code): float(value)
                for code, value in row.items()
                if (
                    not pd.isna(value)
                    and float(value) > 0
                    and math.isfinite(float(value))
                )
            }
        return {}

    @staticmethod
    def _get_open(
        pivot: pd.DataFrame,
        day: pd.Timestamp,
    ) -> dict[str, float]:
        """Compatibility wrapper retained for focused contract tests."""
        return BacktestEngine._get_field(pivot, day, "open")
