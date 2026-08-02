import Modal from '../shared/Modal';
import Button from '../shared/Button';
import type { AiSignalExplanationResult } from '../../types/ai';
import { AiErrorNotice, AiResultMeta } from './AiShared';
import { useAiAction, type AiScopeKey } from './aiAction';

interface SignalExplanationDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onExplain: () => Promise<AiSignalExplanationResult>;
  initialResult?: AiSignalExplanationResult;
  signalLabel?: string;
  disabled?: boolean;
  scopeKey?: AiScopeKey;
}

export default function SignalExplanationDialog({
  isOpen,
  onClose,
  onExplain,
  initialResult,
  signalLabel,
  disabled = false,
  scopeKey,
}: SignalExplanationDialogProps) {
  const action = useAiAction(initialResult, scopeKey);

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="AI 信号解释" size="lg">
      <div className="space-y-4">
        {signalLabel && (
          <div className="rounded-lg bg-gray-50 px-3 py-2 text-sm text-gray-700">
            当前信号：{signalLabel}
          </div>
        )}
        <p className="text-xs text-gray-500">解释仅在点击后生成，不会触发交易或修改策略。</p>
        <AiErrorNotice message={action.error} />
        {action.result && (
          <div className="space-y-3">
            <AiResultMeta result={action.result} />
            <div className="whitespace-pre-wrap break-words text-sm leading-6 text-gray-700">
              {action.result.explanation || '模型未返回信号解释。'}
            </div>
          </div>
        )}
        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <Button variant="secondary" onClick={onClose}>关闭</Button>
          <Button
            loading={action.loading}
            disabled={disabled}
            onClick={() => void action.run(onExplain)}
          >
            {action.result ? '重新解释' : '生成解释'}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
