import { setTimeout as delay } from 'node:timers/promises';

export class ApiError extends Error {
  constructor(public status: number) { super(`Worker API rejected request (${status})`); }
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
        if (!response.ok) throw new ApiError(response.status);
        return await response.json() as T;
      } catch (error) {
        if (attempt >= 2 || signal?.aborted || error instanceof ApiError && error.status < 500 && error.status !== 429) throw error;
        await delay(500 * 2 ** attempt, undefined, {signal});
      }
    }
  }
}
