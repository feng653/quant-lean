"""策略注册中心 —— 自动扫描、发现、索引策略类."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
from pathlib import Path
import sys
from typing import Optional

from backend.strategies.base import (
    PortfolioSignalMode,
    StrategyCategory,
    StrategyMetadata,
    StrategyProtocol,
    SubStrategyRef,
    TrainableStrategy,
    split_platform_params,
)

logger = logging.getLogger(__name__)


class StrategyRegistry:
    """策略注册中心（单例）。

    V3 功能:
        - 自动扫描 strategies/ 目录，发现 StrategyProtocol 子类。
        - 实例化并索引 metadata()。
        - 按 category 分类查询。
        - 组合策略 ↔ 子策略交叉引用查询。
        - 参数校验代理。

    使用::

        registry = get_registry()
        registry.scan_directory(Path("backend/strategies"))
        strategy = registry.get_strategy("ma_cross_v1")
    """

    _instance: Optional["StrategyRegistry"] = None

    def __new__(cls) -> "StrategyRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        # strategy_id → 策略实例
        self._instances: dict[str, StrategyProtocol] = {}
        # strategy_id → 策略类（运行任务时创建隔离实例）
        self._classes: dict[str, type[StrategyProtocol]] = {}
        # strategy_id → 元数据缓存
        self._metadata: dict[str, StrategyMetadata] = {}
        # 反向索引：子策略 → 引用它的组合策略
        self._parent_index: dict[str, list[str]] = {}

    # ── 扫描 ──────────────────────────────────────────────────────────

    def scan_directory(self, base_path: str | Path) -> int:
        """递归扫描目录，发现 StrategyProtocol 子类并实例化。

        Args:
            base_path: strategies/ 包根目录路径。

        Returns:
            新注册的策略数量。
        """
        base_path = Path(base_path)
        if not base_path.exists():
            logger.warning(f"策略目录不存在: {base_path}")
            return 0

        new_count = 0
        for py_file in base_path.rglob("*.py"):
            if py_file.name.startswith("_") and py_file.name != "__init__.py":
                continue
            # 跳过非策略文件
            if py_file.name in ("base.py", "registry.py", "performance.py"):
                continue

            try:
                mod_name = self._path_to_module(py_file, base_path)
                spec = importlib.util.spec_from_file_location(mod_name, py_file)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                previous_module = sys.modules.get(mod_name)
                sys.modules[mod_name] = module
                try:
                    spec.loader.exec_module(module)
                except Exception:
                    if previous_module is None:
                        sys.modules.pop(mod_name, None)
                    else:
                        sys.modules[mod_name] = previous_module
                    raise
            except Exception:
                logger.exception(f"加载策略模块失败: {py_file}")
                continue

            # 用 inspect 发现 StrategyProtocol 子类
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if not issubclass(obj, StrategyProtocol):
                    continue
                if obj is StrategyProtocol:
                    continue
                # 跳过抽象中间类（如 CompositeStrategy）
                # 检查是否实现了所有抽象方法
                if inspect.isabstract(obj):
                    continue

                try:
                    instance = obj()
                    metadata = instance.metadata()
                    self._validate_metadata(obj, metadata)
                except Exception:
                    logger.exception(f"实例化策略失败: {obj.__name__}")
                    continue

                sid = metadata.strategy_id
                if sid in self._instances:
                    logger.debug(f"策略已存在，跳过: {sid}")
                    continue

                self._instances[sid] = instance
                self._classes[sid] = obj
                self._metadata[sid] = metadata

                # 调用生命周期钩子
                try:
                    instance.on_register()
                except Exception:
                    logger.exception(f"on_register 失败: {sid}")

                new_count += 1
                logger.info(f"注册策略: {sid} ({metadata.display_name})")

        # ── 构建反向索引 ──
        self._rebuild_parent_index()

        return new_count

    def _rebuild_parent_index(self) -> None:
        """重建子策略 → 组合策略的反向索引。"""
        self._parent_index.clear()
        for sid, meta in self._metadata.items():
            for sub in meta.sub_strategies:
                self._parent_index.setdefault(sub.strategy_id, []).append(sid)

    # ── 查询 ──────────────────────────────────────────────────────────

    def register_strategy_class(
        self,
        strategy_class: type[StrategyProtocol],
    ) -> None:
        """Register one trusted class, including data-defined safe factories."""
        instance = strategy_class()
        metadata = instance.metadata()
        self._validate_metadata(strategy_class, metadata)
        if metadata.strategy_id in self._instances:
            raise ValueError(f"策略已存在: {metadata.strategy_id}")
        self._instances[metadata.strategy_id] = instance
        self._classes[metadata.strategy_id] = strategy_class
        self._metadata[metadata.strategy_id] = metadata
        instance.on_register()
        self._rebuild_parent_index()

    def replace_strategy_class(
        self,
        strategy_class: type[StrategyProtocol],
    ) -> None:
        """Atomically replace one trusted strategy implementation in memory.

        Persistent version/evidence changes are committed before this method is
        called.  Validation and lifecycle hooks complete before the active maps
        are changed, so a failed replacement leaves the previous runtime usable.
        """
        instance = strategy_class()
        metadata = instance.metadata()
        self._validate_metadata(strategy_class, metadata)
        instance.on_register()
        strategy_id = metadata.strategy_id
        self._instances[strategy_id] = instance
        self._classes[strategy_id] = strategy_class
        self._metadata[strategy_id] = metadata
        self._rebuild_parent_index()

    def get_strategy(self, strategy_id: str) -> StrategyProtocol:
        """按 ID 获取策略实例。

        Raises:
            KeyError: 策略未注册。
        """
        if strategy_id not in self._instances:
            raise KeyError(f"策略未注册: {strategy_id}")
        return self._instances[strategy_id]

    def create_strategy(self, strategy_id: str) -> StrategyProtocol:
        """为一次运行创建隔离的策略实例。

        训练型策略会在实例上保存模型状态，因此后台任务不能共享注册阶段
        创建的全局实例，否则并发实验会相互污染。
        """
        if strategy_id not in self._classes:
            raise KeyError(f"策略未注册: {strategy_id}")
        return self._classes[strategy_id]()

    def get_metadata(self, strategy_id: str) -> StrategyMetadata:
        """按 ID 获取策略元数据。"""
        if strategy_id not in self._metadata:
            raise KeyError(f"策略元数据未找到: {strategy_id}")
        return self._metadata[strategy_id]

    def list_all(self) -> list[StrategyMetadata]:
        """返回所有已注册策略的元数据列表。"""
        return list(self._metadata.values())

    def list_by_category(
        self, category: StrategyCategory
    ) -> list[StrategyMetadata]:
        """按分类筛选策略。"""
        return [m for m in self._metadata.values() if m.category == category]

    def get_sub_strategies(self, strategy_id: str) -> list[SubStrategyRef]:
        """获取某组合策略的子策略列表。"""
        meta = self.get_metadata(strategy_id)
        return meta.sub_strategies

    def get_parent_strategies(self, strategy_id: str) -> list[str]:
        """获取引用了某策略的所有组合策略 ID。"""
        return self._parent_index.get(strategy_id, [])

    def validate_params(
        self, strategy_id: str, params: dict
    ) -> tuple[bool, str]:
        """为指定策略校验参数。

        Returns:
            (is_valid, error_message)
        """
        try:
            if not isinstance(params, dict):
                return False, "params 必须是对象"
            strategy_params, _execution_config = split_platform_params(params)
            metadata = self.get_metadata(strategy_id)
            definitions = {item.name: item for item in metadata.params}
            unknown = sorted(set(strategy_params) - set(definitions))
            if unknown:
                return False, f"未知参数: {', '.join(unknown)}"
            normalized: dict = {}
            for name, field in definitions.items():
                value = strategy_params.get(name, field.default)
                if value is None:
                    if field.required:
                        return False, f"缺少必填参数: {name}"
                    continue
                if field.type in {"int", "integer"}:
                    if isinstance(value, bool) or not isinstance(value, int):
                        return False, f"{name} 必须为整数"
                elif field.type in {"float", "number"}:
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        return False, f"{name} 必须为数字"
                elif field.type in {"bool", "boolean"} and not isinstance(value, bool):
                    return False, f"{name} 必须为布尔值"
                elif field.type in {"str", "string", "choice"} and not isinstance(value, str):
                    return False, f"{name} 必须为字符串"
                if field.choices is not None and value not in field.choices:
                    return False, f"{name} 必须是 {field.choices} 之一"
                if field.min is not None and value < field.min:
                    return False, f"{name} 不能小于 {field.min}"
                if field.max is not None and value > field.max:
                    return False, f"{name} 不能大于 {field.max}"
                normalized[name] = value
            if metadata.requires_training:
                frequency = (
                    metadata.retrain_frequency.value
                    if hasattr(metadata.retrain_frequency, "value")
                    else str(metadata.retrain_frequency)
                )
                train_once = frequency == "never"
                expected_modes = {"fixed"} if train_once else {"expanding", "rolling"}
                default_mode = "fixed" if train_once else "expanding"
                window_mode = normalized.get("window_mode", default_mode)
                if window_mode not in expected_modes:
                    allowed = ", ".join(sorted(expected_modes))
                    return False, f"window_mode 必须是: {allowed}"
                if window_mode == "rolling":
                    rolling_months = normalized.get("rolling_train_months")
                    minimum_months = normalized.get("min_train_months")
                    if (
                        rolling_months is not None
                        and minimum_months is not None
                        and rolling_months < minimum_months
                    ):
                        return (
                            False,
                            "rolling_train_months 不能小于 min_train_months",
                        )
            strategy = self.get_strategy(strategy_id)
            return strategy.validate_params(normalized)
        except KeyError:
            return False, f"策略不存在: {strategy_id}"
        except Exception as e:
            return False, str(e)

    # ── 内部 ──────────────────────────────────────────────────────────

    @staticmethod
    def _validate_metadata(
        strategy_class: type[StrategyProtocol],
        metadata: StrategyMetadata,
    ) -> None:
        try:
            mode = PortfolioSignalMode(metadata.portfolio_signal_mode)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "portfolio_signal_mode 必须是 event_orders 或 target_weights"
            ) from exc
        if (
            issubclass(strategy_class, TrainableStrategy)
            and mode != PortfolioSignalMode.TARGET_WEIGHTS
        ):
            raise ValueError(
                "TrainableStrategy 必须声明 portfolio_signal_mode=target_weights"
            )

    @staticmethod
    def _path_to_module(py_file: Path, base_path: Path) -> str:
        """将文件路径转换为 Python 模块名。"""
        rel = py_file.relative_to(base_path.parent)
        parts = list(rel.parts)
        # 去掉 .py
        parts[-1] = parts[-1].replace(".py", "")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)


# ═══════════════════════════════════════════════════════════════════════════
# 全局访问点
# ═══════════════════════════════════════════════════════════════════════════


_registry_instance: StrategyRegistry | None = None

def get_registry() -> StrategyRegistry:
    """获取策略注册中心全局单例。"""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = StrategyRegistry()
    return _registry_instance
