import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  buildRemoteTrainingClientCommand,
  createRemoteTrainingTask,
  getRemoteTrainingTask,
  listRemoteTrainingTasks,
} from './remoteTraining';

const getMock = vi.hoisted(() => vi.fn());
const postMock = vi.hoisted(() => vi.fn());

vi.mock('./api', () => ({
  API_BASE_URL: 'https://quant.example.test/',
  default: {
    get: getMock,
    post: postMock,
  },
}));

describe('remote training service', () => {
  beforeEach(() => {
    getMock.mockReset();
    postMock.mockReset();
  });

  it('unwraps creation credentials without persisting the token', async () => {
    postMock.mockResolvedValueOnce({
      data: {
        data: {
          task_uuid: 'task-42',
          task_token: 'one-time-secret',
          token_expires_at: '2026-07-28T12:00:00Z',
        },
      },
    });

    const result = await createRemoteTrainingTask({ experiment_id: 42 });

    expect(postMock).toHaveBeenCalledWith(
      '/api/remote-training/tasks',
      { experiment_id: 42 },
      { timeout: 10 * 60 * 1000 },
    );
    expect(result.task_token).toBe('one-time-secret');
  });

  it('supports list and detail envelopes used by the API', async () => {
    getMock
      .mockResolvedValueOnce({
        data: {
          data: {
            items: [
              { task_uuid: 'task-42', experiment_id: 42, status: 'running' },
            ],
          },
        },
      })
      .mockResolvedValueOnce({
        data: {
          data: {
            task: {
              task_uuid: 'task-42',
              experiment_id: 42,
              status: 'completed',
            },
          },
        },
      });

    const tasks = await listRemoteTrainingTasks(42);
    const task = await getRemoteTrainingTask('task-42');

    expect(getMock).toHaveBeenNthCalledWith(
      1,
      '/api/remote-training/tasks',
      { params: { experiment_id: 42 } },
    );
    expect(tasks[0]?.status).toBe('running');
    expect(task.status).toBe('completed');
  });

  it('never puts the one-time token in a generated command or command history', () => {
    const command = buildRemoteTrainingClientCommand({
      task_uuid: 'task-42',
      task_token: 'one-time-secret',
      client_command:
        'python -m remote_worker --task "task-42" --token "one-time-secret"',
    });

    expect(command).toContain('--task-id "task-42"');
    expect(command).toContain('--server "https://quant.example.test"');
    expect(command).not.toContain('one-time-secret');
    expect(command).not.toContain('--token');
  });
});
