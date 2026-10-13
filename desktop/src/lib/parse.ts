import type { DependencyCheck, EngineStatus, GpuStatus, Preflight, WorkerStatus, WorkerState } from './types';

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function text(value: unknown): string | undefined { return typeof value === 'string' ? value : undefined; }
function finite(value: unknown): number | undefined { return typeof value === 'number' && Number.isFinite(value) ? value : undefined; }
function engines(value: unknown): EngineStatus[] {
  return Object.entries(record(value)).map(([name, raw]) => {
    const item = record(raw);
    return { name, available: item.available === true, message: text(item.message) ?? text(item.error), version: text(item.version) };
  });
}
function gpus(value: unknown): GpuStatus[] {
  if (!Array.isArray(value)) return [];
  return value.map(raw => {
    const item = record(raw);
    const bytes = finite(item.memoryBytes);
    return { name: text(item.name) ?? 'GPU', index: finite(item.index) ?? finite(item.id), memoryMb: finite(item.memoryMb) ?? (bytes === undefined ? undefined : Math.round(bytes / 1024 / 1024)), uuid: text(item.uuid) };
  });
}
export function parseStatus(value: unknown): WorkerStatus {
  const raw = record(value);
  const states: WorkerState[] = ['stopped', 'starting', 'idle', 'busy', 'draining', 'error', 'unenrolled'];
  if (!states.includes(raw.state as WorkerState)) throw new Error('The worker returned an unknown state. Update the desktop app and worker together.');
  const detail = record(raw.progress);
  const current = finite(detail.current), total = finite(detail.total);
  const percent = finite(detail.percent);
  const progress = finite(raw.progress) ?? (current !== undefined && total !== undefined && total > 0 ? current / total : percent !== undefined ? percent / 100 : undefined);
  return {
    state: raw.state as WorkerState, running: typeof raw.running === 'boolean' ? raw.running : undefined, connected: raw.connected === true,
    workerId: text(raw.workerId), jobId: text(raw.jobId) ?? text(raw.scanId), backend: text(raw.backend),
    stage: text(raw.stage), progress, message: text(raw.message), updatedAt: text(raw.updatedAt),
    engines: engines(raw.engines), gpus: gpus(raw.gpus),
  };
}
export function parsePreflight(value: unknown): Preflight {
  const raw = record(value);
  const availableEngines = engines(raw.backends ?? raw.engines);
  const checks: DependencyCheck[] = [];
  if (Array.isArray(raw.checks)) {
    for (const item of raw.checks) {
      const check = record(item);
      checks.push({ name: text(check.name) ?? 'Dependency', available: check.available === true, message: text(check.message), version: text(check.version) });
    }
  }
  const inventory = gpus(raw.gpus);
  return { ok: raw.ok === true || (raw.ok === undefined && availableEngines.some(e => e.available) && inventory.length === 1), engines: availableEngines, gpus: inventory, checks, message: text(raw.message) };
}
