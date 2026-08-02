import { describe, expect, it } from 'vitest';
import {
  websocketAuthenticationFrame,
  websocketUrl,
} from './useWebSocket';

describe('WebSocket authentication transport', () => {
  it('keeps bearer credentials out of the request URL', () => {
    const token = 'header.payload.signature';
    const url = websocketUrl('/notifications');

    expect(url).not.toContain(token);
    expect(url).not.toContain('token=');
    expect(url).toMatch(/\/ws\/notifications$/);
  });

  it('puts the bearer credential only in the first data frame', () => {
    const token = 'header.payload.signature';

    expect(JSON.parse(websocketAuthenticationFrame(token))).toEqual({
      type: 'authenticate',
      token,
    });
  });
});
