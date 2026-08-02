"""RBAC 权限定义与检查逻辑."""

from __future__ import annotations

from enum import Enum
from typing import Any


class Permission(str, Enum):
    """模块级细粒度权限定义。"""

    # ── 实验 ──
    EXP_READ = "experiments:read"  # 查看实验
    EXP_CREATE = "experiments:create"  # 创建实验
    EXP_DELETE = "experiments:delete"  # 删除实验
    EXP_SWEEP = "experiments:sweep"  # 参数扫描
    EXP_PROMOTE = "experiments:promote"  # 审批研究晋升

    # ── 交易 ──
    TRADE_READ = "trading:read"  # 查看持仓/信号/订单
    TRADE_DEPLOY = "trading:deploy"  # 部署/修改部署
    TRADE_EXECUTE = "trading:execute"  # 执行模拟交易
    TRADE_REBALANCE = "trading:rebalance"  # 触发再平衡

    # ── 数据 ──
    DATA_READ = "data:read"  # 查看数据
    DATA_UPDATE = "data:update"  # 触发数据更新

    # ── 策略 ──
    STRATEGY_READ = "strategies:read"  # 查看策略
    STRATEGY_SCAN = "strategies:scan"  # 扫描/热加载策略

    # ── AI ──
    AI_USE = "ai:use"  # 使用 AI 分析

    # ── 管理 ──
    ADMIN_USERS = "admin:users"  # 管理用户和权限


# ═══════════════════════════════════════════════════════════════════════════
# 预定义角色
# ═══════════════════════════════════════════════════════════════════════════

ROLE_PERMISSIONS: dict[str, list[str]] = {
    "admin": [p.value for p in Permission],  # 所有权限
    "operator": [  # 操作员
        Permission.EXP_READ.value,
        Permission.EXP_CREATE.value,
        Permission.EXP_SWEEP.value,
        Permission.TRADE_READ.value,
        Permission.TRADE_DEPLOY.value,
        Permission.TRADE_EXECUTE.value,
        Permission.DATA_READ.value,
        Permission.DATA_UPDATE.value,
        Permission.STRATEGY_READ.value,
        Permission.STRATEGY_SCAN.value,
        Permission.AI_USE.value,
    ],
    "viewer": [  # 只读（新用户默认）
        Permission.EXP_READ.value,
        Permission.TRADE_READ.value,
        Permission.DATA_READ.value,
        Permission.STRATEGY_READ.value,
    ],
}

# 所有权限值的集合（快速查找）
_ALL_PERMISSIONS: set[str] = {p.value for p in Permission}


def get_role_permissions(role: str) -> list[str]:
    """获取预设角色对应的权限列表。

    Args:
        role: 角色名（"admin" | "operator" | "viewer"）。

    Returns:
        权限值列表；角色不存在返回空列表。
    """
    return ROLE_PERMISSIONS.get(role, [])


def is_valid_permission(perm: str) -> bool:
    """检查字符串是否为有效权限。"""
    return perm in _ALL_PERMISSIONS


def has_permission(user: dict[str, Any], permission: Permission | str) -> bool:
    """检查用户是否拥有指定权限。

    Args:
        user: 用户字典，必须包含 'permissions' 列表字段，
              或 'is_admin' 布尔字段（admin 拥有全部权限）。
        permission: 权限值（Permission 枚举或字符串）。

    Returns:
        True 如果用户拥有该权限。
    """
    perm_str = permission.value if isinstance(permission, Permission) else permission

    # Admin 全权限
    if user.get("is_admin"):
        return True

    user_perms: list[str] = user.get("permissions", [])
    return perm_str in user_perms


def merge_permissions(
    role_perms: list[str], extra_perms: list[str]
) -> list[str]:
    """合并角色预设权限和额外授予的权限（去重）。

    Args:
        role_perms: 角色预设权限列表。
        extra_perms: 额外单独授予的权限。

    Returns:
        去重合并后的权限列表。
    """
    return list(set(role_perms) | set(extra_perms))
