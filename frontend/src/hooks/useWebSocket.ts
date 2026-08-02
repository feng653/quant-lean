import { useState, useEffect, useRef, useCallback } from 'react';
import type { Signal } from '../types/trading';
import type { AppNotification } from '../store/notificationStore';
import { useNotificationStore } from '../store/notificationStore';
import type { JobUpdateEvent } from '../types/job';
import { webSocketBaseUrl } from '../config/runtime';

const WS_BASE_URL = import.meta.env.VITE_WS_URL || webSocketBaseUrl();
const MAX_RECONNECT_ATTEMPTS = 10;
const RECONNECT_INTERVAL = 5000;

interface TrainingProgress {
  progress: number;
  message: string;
  epoch: number;
  loss: number;
}

export function websocketUrl(path: string): string {
  return `${WS_BASE_URL}${path}`;
}

export function websocketAuthenticationFrame(token: string): string {
  return JSON.stringify({ type: 'authenticate', token });
}

function useWebSocketBase(
  path: string,
  onMessage: (data: unknown) => void,
  enabled: boolean = true
) {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttempts = useRef(0);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = useCallback(() => {
    if (!enabled) return;
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const token = localStorage.getItem('auth_token');
    if (!token) return;

    const ws = new WebSocket(websocketUrl(path));
    wsRef.current = ws;

    ws.onopen = () => {
      ws.send(websocketAuthenticationFrame(token));
    };

    ws.onmessage = (event: MessageEvent) => {
      try {
        const data = JSON.parse(event.data as string);
        if (
          typeof data === 'object' &&
          data !== null &&
          (data as Record<string, unknown>).type === 'authenticated'
        ) {
          reconnectAttempts.current = 0;
          return;
        }
        onMessage(data);
      } catch {
        // ignore parse errors
      }
    };

    ws.onclose = () => {
      wsRef.current = null;
      if (reconnectAttempts.current < MAX_RECONNECT_ATTEMPTS) {
        reconnectTimer.current = setTimeout(() => {
          reconnectAttempts.current += 1;
          connect();
        }, RECONNECT_INTERVAL);
      }
    };

    ws.onerror = () => {
      ws.close();
    };
  }, [path, onMessage, enabled]);

  useEffect(() => {
    connect();
    return () => {
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current);
      }
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [connect]);

  return wsRef;
}

/**
 * 监听训练进度
 */
export function useTrainingProgress(
  experimentId: number | null
): TrainingProgress | null {
  const [progress, setProgress] = useState<TrainingProgress | null>(null);

  const handleMessage = useCallback((data: unknown) => {
    const msg = data as Record<string, unknown>;
    if (msg.type === 'training_progress') {
      setProgress({
        progress: (msg.progress as number) ?? 0,
        message: (msg.message as string) ?? '',
        epoch: (msg.epoch as number) ?? 0,
        loss: (msg.loss as number) ?? 0,
      });
    }
  }, []);

  useWebSocketBase(
    experimentId ? `/training/${experimentId}` : '',
    handleMessage,
    !!experimentId
  );

  return progress;
}

/**
 * 监听系统通知
 */
export function useNotifications(): AppNotification[] {
  const addNotification = useNotificationStore((s) => s.addNotification);
  const notifications = useNotificationStore((s) => s.notifications);

  const handleMessage = useCallback(
    (data: unknown) => {
      const msg = data as Record<string, unknown>;
      if (msg.type === 'notification') {
        addNotification({
          type: (msg.level as AppNotification['type']) ?? 'info',
          title: (msg.title as string) ?? '',
          message: (msg.message as string) ?? '',
        });
      }
    },
    [addNotification]
  );

  useWebSocketBase('/notifications', handleMessage, true);

  return notifications;
}

/**
 * 监听实时信号
 */
export function useRealtimeSignals(deploymentId: number | null): Signal[] {
  const [signals, setSignals] = useState<Signal[]>([]);

  const handleMessage = useCallback((data: unknown) => {
    const msg = data as Record<string, unknown>;
    if (msg.type === 'signal') {
      const signal: Signal = {
        code: (msg.code as string) ?? '',
        action: ((msg.action as string) ?? (msg.signal_type as string) ?? '').toUpperCase(),
        score: (msg.score as number) ?? 0,
        weight: (msg.weight as number) ?? (msg.target_weight as number) ?? 0,
        confidence: (msg.confidence as number) ?? 0,
        reasoning: (msg.reasoning as string) ?? '',
      };
      setSignals((prev) => [signal, ...prev].slice(0, 200));
    } else if (msg.type === 'signal_batch') {
      const batch = (msg.signals as Signal[]) ?? [];
      setSignals((prev) => [...batch, ...prev].slice(0, 500));
    }
  }, []);

  useWebSocketBase(
    deploymentId ? `/realtime/${deploymentId}` : '',
    handleMessage,
    !!deploymentId
  );

  return signals;
}

/**
 * 监听后台任务状态变化。REST 负责快照，WebSocket 只负责通知刷新。
 */
export function useJobEvents(
  onJobChange: (event: JobUpdateEvent) => void,
  enabled: boolean = true
): void {
  const handleMessage = useCallback(
    (data: unknown) => {
      const message = data as Record<string, unknown>;
      if (message.type === 'job_updated') {
        onJobChange(message as unknown as JobUpdateEvent);
      }
    },
    [onJobChange]
  );

  useWebSocketBase('/jobs', handleMessage, enabled);
}
