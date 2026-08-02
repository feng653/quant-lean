import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import AiAnalysisCard from './AiAnalysisCard';
import AiDiagnosisCard from './AiDiagnosisCard';
import {
  applySelectedSuggestions,
  suggestionValidation,
} from './paramSuggestionUtils';
import {
  aiErrorMessage,
  createAiRequestSequence,
} from './aiAction';
import { analyzeBacktest, diagnoseError, explainSignal } from '../../services/ai';
import type { StrategyMetadata } from '../../types/strategy';
import type { AiDiagnosisResult } from '../../types/ai';

const postMock = vi.hoisted(() => vi.fn());

vi.mock('../../services/api', () => ({
  default: { post: postMock },
}));

const strategy: Pick<StrategyMetadata, 'strategy_id' | 'display_name' | 'params'> = {
  strategy_id: 'momentum',
  display_name: '动量策略',
  params: [
    {
      name: 'window',
      type: 'int',
      default: 20,
      description: '窗口',
      required: true,
      min: 5,
      max: 120,
    },
    {
      name: 'threshold',
      type: 'float',
      default: 0.1,
      description: '阈值',
      required: true,
      min: 0,
      max: 1,
    },
    {
      name: 'mode',
      type: 'choice',
      default: 'fast',
      description: '模式',
      required: true,
      choices: ['fast', 'slow'],
    },
  ],
};

describe('AI components', () => {
  beforeEach(() => {
    postMock.mockReset();
  });

  it('invalidates superseded and out-of-scope AI requests', () => {
    const sequence = createAiRequestSequence();
    const first = sequence.next();
    expect(sequence.isCurrent(first)).toBe(true);

    const second = sequence.next();
    expect(sequence.isCurrent(first)).toBe(false);
    expect(sequence.isCurrent(second)).toBe(true);

    sequence.invalidate();
    expect(sequence.isCurrent(second)).toBe(false);
  });

  it('separates missing API key errors from generic 503 outages', () => {
    expect(aiErrorMessage(new Error('DEEPSEEK_API_KEY not configured')))
      .toContain('尚未配置 API Key');
    expect(aiErrorMessage(new Error('Request failed with status code 503')))
      .toBe('AI 服务暂时不可用，请稍后重试。');
  });

  it('keeps generic service outages distinct in the typed AI service', async () => {
    postMock.mockRejectedValueOnce(new Error('Request failed with status code 503'));
    await expect(analyzeBacktest(42)).rejects.toThrow('AI 服务暂时不可用，请稍后重试。');
  });

  it('rejects non-string choice suggestions without coercion', () => {
    expect(suggestionValidation(strategy, {
      param_name: 'mode',
      suggested_value: 1,
      reason: '错误类型',
    })).toEqual({
      valid: false,
      reason: '建议值不是文本',
    });
  });

  it('only applies explicitly selected and schema-valid parameter suggestions', () => {
    const result = applySelectedSuggestions(
      strategy,
      { window: 20, threshold: 0.1 },
      [
        {
          param_name: 'window',
          current_value: 20,
          suggested_value: 30,
          reason: '更平滑',
        },
        {
          param_name: 'threshold',
          current_value: 0.1,
          suggested_value: 2,
          reason: '越界建议',
        },
        {
          param_name: 'unknown',
          suggested_value: true,
          reason: '未知参数',
        },
      ],
      new Set(['window', 'threshold', 'unknown'])
    );

    expect(result.params).toEqual({ window: 30, threshold: 0.1 });
    expect(result.applied.map((item) => item.param_name)).toEqual(['window']);
  });

  it('marks a cached backtest analysis without triggering another request', () => {
    const onAnalyze = vi.fn(async () => ({
      cached: false,
      analysis: 'new',
    }));
    const html = renderToStaticMarkup(
      <AiAnalysisCard
        onAnalyze={onAnalyze}
        initialResult={{
          cached: true,
          model: 'deepseek-chat',
          usage: { total_tokens: 128, latency_ms: 320 },
          analysis: '缓存中的分析',
        }}
      />
    );

    expect(html).toContain('缓存结果');
    expect(html).toContain('缓存中的分析');
    expect(html).toContain('Token：128');
    expect(html).toContain('延迟：320 ms');
    expect(onAnalyze).not.toHaveBeenCalled();
  });

  it('sends the signal as an object matching the backend request contract', async () => {
    postMock.mockResolvedValueOnce({
      data: {
        data: {
          strategy_id: 'momentum',
          explanation: '价格突破且成交量放大',
        },
      },
    });

    const result = await explainSignal(
      'momentum',
      { direction: 'buy', strength: 0.82 },
      { symbol: '000001.SZ' }
    );

    expect(postMock).toHaveBeenCalledWith('/api/ai/explain-signal', {
      strategy_id: 'momentum',
      signal: { direction: 'buy', strength: 0.82 },
      context: { symbol: '000001.SZ' },
    });
    expect(result.explanation).toBe('价格突破且成交量放大');
  });

  it('normalizes the structured diagnosis returned by the backend', async () => {
    postMock.mockResolvedValueOnce({
      data: {
        data: {
          diagnosis: '策略数据不完整',
          structured: {
            category: 'data',
            root_cause: '缺少 close 列',
            evidence: 'KeyError: close',
            fix_suggestion: '修复字段映射',
            auto_fixable: false,
          },
        },
      },
    });

    const result = await diagnoseError(12, 'traceback');

    expect(result).toMatchObject({
      experiment_id: 12,
      category: 'data',
      root_cause: '缺少 close 列',
      evidence: ['KeyError: close'],
      fix_suggestion: '修复字段映射',
      fix_suggestions: ['修复字段映射'],
      auto_fixable: false,
    });
  });

  it('renders structured diagnosis as read-only guidance with no auto-fix control', () => {
    const diagnosis: AiDiagnosisResult = {
      cached: false,
      diagnosis: '数据列缺失',
      category: 'data',
      severity: '高',
      root_cause: '行情源字段映射缺少 close',
      evidence: ['日志包含 KeyError: close'],
      fix_suggestion: '检查数据源字段映射',
      fix_suggestions: ['检查数据源字段映射'],
      auto_fixable: true,
    };
    const html = renderToStaticMarkup(
      <AiDiagnosisCard onDiagnose={vi.fn(async () => diagnosis)} initialResult={diagnosis} />
    );

    expect(html).toContain('data');
    expect(html).toContain('行情源字段映射缺少 close');
    expect(html).toContain('日志包含 KeyError: close');
    expect(html).toContain('检查数据源字段映射');
    expect(html).toContain('具备自动处理条件');
    expect(html).not.toContain('自动修复');
    expect(html).not.toContain('应用修复');
  });
});
