const BACKEND_PORT = '8000';

function defaultOrigin(): string {
  return typeof window === 'undefined'
    ? 'http://localhost:5173'
    : window.location.origin;
}

export function apiBaseUrl(origin: string = defaultOrigin()): string {
  const url = new URL(origin);
  if (!url.port) {
    return url.origin;
  }
  url.port = BACKEND_PORT;
  return url.origin;
}

export function webSocketBaseUrl(
  origin: string = defaultOrigin(),
): string {
  const url = new URL('/ws', origin);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  if (url.port) {
    url.port = BACKEND_PORT;
  }
  return url.toString();
}
