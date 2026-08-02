import { useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router';
import { useAuthStore } from '../../store/authStore';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Input from '../../components/shared/Input';
import StatusTag from '../../components/shared/StatusTag';

interface FieldErrors {
  username?: string;
  displayName?: string;
  password?: string;
  confirmPassword?: string;
}

export default function RegisterPage() {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [errors, setErrors] = useState<FieldErrors>({});
  const [serverError, setServerError] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const register = useAuthStore((s) => s.register);
  const navigate = useNavigate();

  const validate = (): FieldErrors => {
    const next: FieldErrors = {};
    if (!username.trim()) {
      next.username = '请输入用户名';
    } else if (username.trim().length < 3) {
      next.username = '用户名至少3个字符';
    } else if (!/^[A-Za-z0-9_.-]+$/.test(username.trim())) {
      next.username = '用户名只能包含字母、数字、点、下划线和连字符';
    }
    if (!displayName.trim()) {
      next.displayName = '请输入显示名';
    }
    if (!password) {
      next.password = '请输入密码';
    } else if (password.length < 8) {
      next.password = '密码长度至少8位';
    }
    if (!confirmPassword) {
      next.confirmPassword = '请再次输入密码';
    } else if (confirmPassword !== password) {
      next.confirmPassword = '两次密码输入不一致';
    }
    return next;
  };

  const clearFieldError = (field: keyof FieldErrors) => {
    setErrors((current) => ({ ...current, [field]: undefined }));
  };

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setServerError('');
    const nextErrors = validate();
    setErrors(nextErrors);
    if (Object.values(nextErrors).some(Boolean)) return;

    setIsSubmitting(true);
    try {
      await register(username.trim(), password, displayName.trim());
      navigate('/');
    } catch (err: unknown) {
      setServerError(err instanceof Error ? err.message : '注册失败，请重试');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-paper px-4 py-8">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <h1 className="text-2xl font-bold text-ink-900">创建账户</h1>
          <p className="mt-1 text-sm text-ink-500">注册以开始使用量化验证平台</p>
          <div className="mt-3 flex justify-center">
            <StatusTag variant="paper">研究与模拟环境 · 未通过实盘认证</StatusTag>
          </div>
        </div>

        <form
          onSubmit={handleSubmit}
          className="rounded-md border border-ink-200 bg-surface p-6"
          noValidate
        >
          {serverError && (
            <Banner variant="danger" className="mb-4">
              {serverError}
            </Banner>
          )}

          <div className="space-y-4">
            <Input
              label="用户名"
              value={username}
              onChange={(event) => { setUsername(event.target.value); clearFieldError('username'); }}
              error={errors.username}
              placeholder="字母数字组合，至少3位"
              autoComplete="username"
            />
            <Input
              label="显示名"
              value={displayName}
              onChange={(event) => { setDisplayName(event.target.value); clearFieldError('displayName'); }}
              error={errors.displayName}
              placeholder="您希望如何被称呼"
              autoComplete="nickname"
            />
            <Input
              label="密码"
              type="password"
              value={password}
              onChange={(event) => { setPassword(event.target.value); clearFieldError('password'); }}
              error={errors.password}
              placeholder="至少8位字符"
              autoComplete="new-password"
            />
            <Input
              label="确认密码"
              type="password"
              value={confirmPassword}
              onChange={(event) => { setConfirmPassword(event.target.value); clearFieldError('confirmPassword'); }}
              error={errors.confirmPassword}
              placeholder="再次输入密码"
              autoComplete="new-password"
            />
          </div>

          <Button type="submit" className="mt-6 w-full" loading={isSubmitting}>
            注册
          </Button>

          <p className="mt-4 text-center text-sm text-ink-500">
            已有账号？
            <Link to="/login" className="ml-1 font-medium text-accent-700 hover:underline">
              去登录
            </Link>
          </p>
        </form>

        <p className="mt-5 text-center text-xs leading-5 text-ink-400">
          本平台仅用于量化研究与模拟交易，不提供真实资金交易功能。
        </p>
      </div>
    </div>
  );
}
