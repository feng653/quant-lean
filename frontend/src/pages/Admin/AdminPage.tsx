import { useCallback, useEffect, useMemo, useState } from 'react';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import Input from '../../components/shared/Input';
import Spinner from '../../components/shared/Spinner';
import {
  createAdminUser,
  deactivateAdminUser,
  listAdminUsers,
  listPermissions,
  updateAdminUserPermissions,
  updateAdminUserStatus,
} from '../../services/admin';
import type { AdminUser, PermissionDefinition } from '../../services/admin';
import { useAuthStore } from '../../store/authStore';

export default function AdminPage() {
  const currentUser = useAuthStore((state) => state.user);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [permissions, setPermissions] = useState<PermissionDefinition[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [selectedPermissions, setSelectedPermissions] = useState<Set<string>>(new Set());
  const [username, setUsername] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [isAdmin, setIsAdmin] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [userItems, permissionItems] = await Promise.all([
        listAdminUsers(),
        listPermissions(),
      ]);
      setUsers(userItems);
      setPermissions(permissionItems);
      setSelectedId((current) => current ?? userItems[0]?.id ?? null);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载管理员数据失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  const selected = users.find((item) => item.id === selectedId) ?? null;
  useEffect(() => {
    setSelectedPermissions(new Set(selected?.permissions ?? []));
  }, [selected]);

  const groupedPermissions = useMemo(() => {
    const groups = new Map<string, PermissionDefinition[]>();
    for (const permission of permissions) {
      groups.set(permission.group, [...(groups.get(permission.group) ?? []), permission]);
    }
    return [...groups.entries()];
  }, [permissions]);

  const createUser = async () => {
    setSaving(true);
    setError(null);
    try {
      await createAdminUser({
        username,
        password,
        display_name: displayName || undefined,
        email: email || undefined,
        is_admin: isAdmin,
      });
      setUsername('');
      setDisplayName('');
      setEmail('');
      setPassword('');
      setIsAdmin(false);
      setMessage('用户已创建');
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : '创建用户失败');
    } finally {
      setSaving(false);
    }
  };

  const savePermissions = async () => {
    if (!selected || selected.is_admin) return;
    setSaving(true);
    try {
      await updateAdminUserPermissions(selected.id, [...selectedPermissions].sort());
      setMessage(`已更新 ${selected.username} 的权限`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : '更新权限失败');
    } finally {
      setSaving(false);
    }
  };

  const toggleStatus = async (user: AdminUser) => {
    setSaving(true);
    try {
      await updateAdminUserStatus(user.id, !user.is_active);
      setMessage(`${user.username} 已${user.is_active ? '停用' : '启用'}`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : '更新状态失败');
    } finally {
      setSaving(false);
    }
  };

  const deactivate = async (user: AdminUser) => {
    if (!window.confirm(`确认停用用户 ${user.username} 并撤销其权限？历史数据会保留。`)) return;
    setSaving(true);
    try {
      await deactivateAdminUser(user.id);
      setMessage(`${user.username} 已停用`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : '停用用户失败');
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div className="flex justify-center py-20"><Spinner size="lg" /></div>;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-800">用户与权限</h1>
        <p className="mt-0.5 text-sm text-gray-500">创建账号、停用访问并按能力分配权限</p>
      </div>
      {error && <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>}
      {message && <div className="rounded-lg border border-green-200 bg-green-50 p-3 text-sm text-green-700">{message}</div>}

      <Card>
        <h2 className="mb-4 font-semibold">创建用户</h2>
        <div className="grid grid-cols-1 gap-3 md:grid-cols-5">
          <Input label="用户名" value={username} onChange={(event) => setUsername(event.target.value)} />
          <Input label="显示名称" value={displayName} onChange={(event) => setDisplayName(event.target.value)} />
          <Input label="邮箱" type="email" value={email} onChange={(event) => setEmail(event.target.value)} />
          <Input label="初始密码" type="password" value={password} onChange={(event) => setPassword(event.target.value)} />
          <div className="flex items-end gap-3 pb-2">
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={isAdmin} onChange={(event) => setIsAdmin(event.target.checked)} />管理员</label>
            <Button loading={saving} disabled={username.length < 2 || password.length < 8} onClick={() => void createUser()}>创建</Button>
          </div>
        </div>
      </Card>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1.2fr)_minmax(360px,0.8fr)]">
        <Card padding="none">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
              <thead className="border-b bg-gray-50 text-left text-xs text-gray-500"><tr><th className="px-4 py-3">用户</th><th className="px-4 py-3">角色</th><th className="px-4 py-3">状态</th><th className="px-4 py-3">最后登录</th><th className="px-4 py-3 text-right">操作</th></tr></thead>
              <tbody className="divide-y">
                {users.map((user) => (
                  <tr key={user.id} className={selectedId === user.id ? 'bg-primary-50' : ''} onClick={() => setSelectedId(user.id)}>
                    <td className="cursor-pointer px-4 py-3"><p className="font-medium">{user.display_name || user.username}</p><p className="text-xs text-gray-400">{user.username}{user.email ? ` · ${user.email}` : ''}</p></td>
                    <td className="px-4 py-3">{user.role}</td>
                    <td className="px-4 py-3"><span className={`rounded-full px-2 py-1 text-xs ${user.is_active ? 'bg-green-50 text-green-700' : 'bg-gray-100 text-gray-500'}`}>{user.is_active ? '启用' : '停用'}</span></td>
                    <td className="px-4 py-3">{user.last_login ? new Date(user.last_login).toLocaleString('zh-CN') : '-'}</td>
                    <td className="space-x-1 px-4 py-3 text-right">
                      <Button size="sm" variant="secondary" disabled={saving || user.id === currentUser?.id} onClick={(event) => { event.stopPropagation(); void toggleStatus(user); }}>{user.is_active ? '停用' : '启用'}</Button>
                      {user.is_active && <Button size="sm" variant="danger" disabled={saving || user.id === currentUser?.id} onClick={(event) => { event.stopPropagation(); void deactivate(user); }}>撤权停用</Button>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>

        <Card>
          <h2 className="font-semibold">{selected ? `${selected.username} 的权限` : '选择用户'}</h2>
          {selected?.is_admin ? (
            <p className="mt-4 text-sm text-gray-500">管理员自动拥有全部权限，无需逐项配置。</p>
          ) : selected ? (
            <>
              <div className="mt-4 space-y-4">
                {groupedPermissions.map(([group, items]) => (
                  <div key={group}>
                    <p className="mb-2 text-xs font-semibold uppercase text-gray-400">{group}</p>
                    <div className="grid grid-cols-2 gap-2">
                      {items.map((permission) => (
                        <label key={permission.key} className="flex items-center gap-2 text-sm">
                          <input
                            type="checkbox"
                            checked={selectedPermissions.has(permission.key)}
                            onChange={(event) => setSelectedPermissions((current) => {
                              const next = new Set(current);
                              if (event.target.checked) next.add(permission.key);
                              else next.delete(permission.key);
                              return next;
                            })}
                          />
                          {permission.name}
                        </label>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
              <Button className="mt-5" loading={saving} onClick={() => void savePermissions()}>保存权限</Button>
            </>
          ) : <p className="mt-4 text-sm text-gray-500">暂无用户。</p>}
        </Card>
      </div>
    </div>
  );
}
