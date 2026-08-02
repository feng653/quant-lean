import { useState } from 'react';
import Button from '../shared/Button';
import type { RemoteTrainingTaskCredential } from '../../types/remoteTraining';
import { buildRemoteTrainingClientCommand } from '../../services/remoteTraining';

export default function RemoteTrainingCredential({
  credential,
  onDismiss,
}: {
  credential: RemoteTrainingTaskCredential;
  onDismiss: () => void;
}) {
  const [copied, setCopied] = useState<'token' | 'command' | null>(null);
  const command = buildRemoteTrainingClientCommand(credential);

  const copy = async (value: string, target: 'token' | 'command') => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(target);
    } catch {
      setCopied(null);
    }
  };

  return (
    <section
      className="rounded-xl border border-amber-200 bg-amber-50 p-4"
      aria-labelledby="remote-training-credential-title"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h4
            id="remote-training-credential-title"
            className="text-sm font-semibold text-amber-900"
          >
            一次性客户端凭据
          </h4>
          <p className="mt-1 text-xs leading-5 text-amber-800">
            此信息只在本页内存中显示。关闭、刷新或离开页面后无法再次查看，请立即复制到可信的 Windows 终端。
            命令执行后，客户端会安全提示你单独粘贴令牌，令牌不会进入 PowerShell 历史记录。
          </p>
        </div>
        <Button size="sm" variant="ghost" onClick={onDismiss}>
          已保存，隐藏
        </Button>
      </div>

      <div className="mt-4 space-y-3">
        <div>
          <div className="mb-1 flex items-center justify-between gap-2">
            <label className="text-xs font-medium text-amber-900" htmlFor="remote-task-token">
              一次性令牌
            </label>
            {credential.task_token && (
              <Button
                size="sm"
                variant="secondary"
                onClick={() => void copy(credential.task_token as string, 'token')}
              >
                {copied === 'token' ? '已复制' : '复制令牌'}
              </Button>
            )}
          </div>
          <input
            id="remote-task-token"
            readOnly
            value={credential.task_token || '服务端未返回令牌，请重新创建任务'}
            className="w-full rounded-lg border border-amber-200 bg-white px-3 py-2 font-mono text-xs text-gray-800"
          />
          {credential.token_expires_at && (
            <p className="mt-1 text-[11px] text-amber-700">
              有效期至 {new Date(credential.token_expires_at).toLocaleString('zh-CN')}
            </p>
          )}
        </div>

        <div>
          <div className="mb-1 flex items-center justify-between gap-2">
            <label className="text-xs font-medium text-amber-900" htmlFor="remote-client-command">
              PowerShell 命令
            </label>
            {command && (
              <Button
                size="sm"
                variant="secondary"
                onClick={() => void copy(command, 'command')}
              >
                {copied === 'command' ? '已复制' : '复制命令'}
              </Button>
            )}
          </div>
          <textarea
            id="remote-client-command"
            readOnly
            rows={3}
            value={command || `任务 ${credential.task_uuid} 已创建，但服务端未返回可执行命令。`}
            className="w-full resize-none rounded-lg border border-amber-200 bg-gray-950 px-3 py-2 font-mono text-xs leading-5 text-gray-100"
          />
        </div>
      </div>

      <p className="sr-only" aria-live="polite">
        {copied ? '已复制到剪贴板' : ''}
      </p>
    </section>
  );
}
