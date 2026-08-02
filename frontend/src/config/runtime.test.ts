import { describe, expect, it } from 'vitest';
import { apiBaseUrl, webSocketBaseUrl } from './runtime';

describe('runtime service URLs', () => {
  it('uses a safe local origin when window is unavailable', () => {
    expect(typeof window).toBe('undefined');
    expect(apiBaseUrl()).toBe('http://localhost:8000');
    expect(webSocketBaseUrl()).toBe('ws://localhost:8000/ws');
  });

  it('keeps the current host when selecting the backend port', () => {
    expect(apiBaseUrl('http://macmini.example.ts.net:5173')).toBe(
      'http://macmini.example.ts.net:8000',
    );
    expect(webSocketBaseUrl('http://macmini.example.ts.net:5173')).toBe(
      'ws://macmini.example.ts.net:8000/ws',
    );
  });

  it('keeps public HTTPS traffic on the reverse-proxy origin', () => {
    expect(apiBaseUrl('https://mac.feng37.top')).toBe(
      'https://mac.feng37.top',
    );
    expect(webSocketBaseUrl('https://mac.feng37.top')).toBe(
      'wss://mac.feng37.top/ws',
    );
  });

  it('preserves HTTPS and brackets IPv6 hosts', () => {
    expect(apiBaseUrl('https://[fd7a:115c:a1e0::1]:5173')).toBe(
      'https://[fd7a:115c:a1e0::1]:8000',
    );
    expect(webSocketBaseUrl('https://[fd7a:115c:a1e0::1]:5173')).toBe(
      'wss://[fd7a:115c:a1e0::1]:8000/ws',
    );
  });
});
