import { useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router';
import { useAuthStore } from '../../store/authStore';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Input from '../../components/shared/Input';
import StatusTag from '../../components/shared/StatusTag';

export default function LoginPage() {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const login = useAuthStore((s) => s.login);
  const navigate = useNavigate();

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setError('');

    if (!username.trim()) {
      setError('请输入用户名');
      return;
    }
    if (!password) {
      setError('请输入密码');
      return;
    }

    setIsSubmitting(true);
    try {
      await login(username.trim(), password);
      navigate('/');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '登录失败，请重试');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-paper px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <h1 className="text-2xl font-bold text-ink-900">量化验证平台</h1>
          <p className="mt-1 text-sm text-ink-500">登录您的账户以继续</p>
          <div className="mt-3 flex justify-center">
            <StatusTag variant="paper">研究与模拟环境 · 未通过实盘认证</StatusTag>
          </div>
        </div>

        <form
          onSubmit={handleSubmit}
          className="rounded-md border border-ink-200 bg-surface p-6"
          noValidate
        >
          {error && (
            <Banner variant="danger" className="mb-4">
              {error}
            </Banner>
          )}

          <div className="space-y-4">
            <Input
              label="用户名"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder="请输入用户名"
              autoComplete="username"
              autoFocus
            />
            <Input
              label="密码"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="请输入密码"
              autoComplete="current-password"
            />
          </div>

          <Button type="submit" className="mt-6 w-full" loading={isSubmitting}>
            登录
          </Button>

          <p className="mt-4 text-center text-sm text-ink-500">
            还没有账号？
            <Link to="/register" className="ml-1 font-medium text-accent-700 hover:underline">
              去注册
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
