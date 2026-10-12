import { setTimeout as delay } from 'node:timers/promises';
import { redactSecrets } from '@guerrilla/worker-protocol';

export class ApiError extends Error {
  constructor(public status: number, route?: string) { super(`Worker API${route ? ` ${route}` : ''} rejected request (${status})`); }
}
// Log transport codes, never cause messages, addresses or signed request URLs.
const transportCodes = new Set(['ENOTFOUND','EAI_AGAIN','ECONNREFUSED','ECONNRESET','ETIMEDOUT','EHOSTUNREACH','ENETUNREACH','EPIPE','ERR_TLS_CERT_ALTNAME_INVALID','CERT_HAS_EXPIRED','DEPTH_ZERO_SELF_SIGNED_CERT','UNABLE_TO_VERIFY_LEAF_SIGNATURE','UND_ERR_CONNECT_TIMEOUT','UND_ERR_HEADERS_TIMEOUT','UND_ERR_BODY_TIMEOUT','UND_ERR_SOCKET']);
export function describeWorkerError(error: unknown): string {
  const codes = new Set<string>();
  const seen = new Set<unknown>();
  function visit(value: unknown) {
    if (!value || typeof value !== 'object' || seen.has(value) || seen.size >= 20) return;
    seen.add(value);
    const cause = value as {code?: unknown; cause?: unknown; errors?: unknown[]; name?: string};
    if (typeof cause.code === 'string' && transportCodes.has(cause.code)) codes.add(cause.code);
    if (cause.name === 'TimeoutError' || cause.name === 'AbortError') codes.add(cause.name);
    visit(cause.cause);
    if (Array.isArray(cause.errors)) for (const child of cause.errors) visit(child);
  }
  visit(error);
  const message = error instanceof Error ? error.message : 'Worker request failed';
  const details = [...codes].filter(code=>!message.includes(code));
  return `${redactSecrets(message)}${details.length ? ` (${details.join(', ')})` : ''}`;
}
export class Client {
  constructor(public origin: string, public credential: string) {
    const url = new URL(origin);
    if (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname))) throw new Error('Control plane requires HTTPS');
    if (url.username || url.password || url.search || url.hash) throw new Error('Invalid control plane origin');
    this.origin = url.origin;
  }
  async post<T>(route: string, body: unknown, signal?: AbortSignal): Promise<T> {
    const timeout = ['complete','checkpoint'].includes(route) ? 600000 : 30000;
    for (let attempt = 0; ; attempt++) {
      try {
        const response = await fetch(`${this.origin}/api/worker/${route}`, {
          method: 'POST', headers: {'content-type': 'application/json', ...(this.credential ? {authorization: `Bearer ${this.credential}`} : {})},
          body: JSON.stringify(body), signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(timeout)]) : AbortSignal.timeout(timeout), redirect: 'error',
        });
        if (!response.ok) throw new ApiError(response.status,route);
        return await response.json() as T;
      } catch (error) {
        if (attempt >= 2 || signal?.aborted || error instanceof ApiError && error.status < 500 && error.status !== 429) {
          if (error instanceof ApiError) throw error;
          throw new Error(`Worker ${route} request failed: ${describeWorkerError(error)}`,{cause:error});
        }
        await delay(500 * 2 ** attempt, undefined, {signal});
      }
    }
  }
}
