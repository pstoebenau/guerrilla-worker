export type UpdateStatus = 'idle' | 'checking' | 'downloading' | 'ready' | 'waitingForIdle' | 'installing' | 'error';
export interface UpdateSnapshot {
  revision: number;
  status: UpdateStatus;
  version: string | null;
  downloaded: number;
  total: number | null;
  installQueued: boolean;
  workerBusy: boolean;
  error: string | null;
  releaseUrl: string | null;
}

const statuses: readonly string[] = ['idle', 'checking', 'downloading', 'ready', 'waitingForIdle', 'installing', 'error'];
function counter(value: unknown): value is number { return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0; }
function nullableText(value: unknown): value is string | null { return value === null || typeof value === 'string'; }
export function parseUpdateSnapshot(value: unknown): UpdateSnapshot {
  const invalid = () => new Error('The app returned an invalid update status. Try checking for updates again.');
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw invalid();
  const raw = value as Record<string, unknown>;
  if (!counter(raw.revision) || typeof raw.status !== 'string' || !statuses.includes(raw.status)
    || !nullableText(raw.version) || !counter(raw.downloaded) || !(raw.total === null || counter(raw.total))
    || typeof raw.installQueued !== 'boolean' || typeof raw.workerBusy !== 'boolean' || !nullableText(raw.error)
    || !(raw.releaseUrl === undefined || nullableText(raw.releaseUrl))) throw invalid();
  return {
    revision: raw.revision, status: raw.status as UpdateStatus, version: raw.version,
    downloaded: raw.downloaded, total: raw.total, installQueued: raw.installQueued,
    workerBusy: raw.workerBusy, error: raw.error, releaseUrl: raw.releaseUrl ?? null,
  };
}

// A snapshot request may finish after an event with newer download progress.
export function latestUpdate(current: UpdateSnapshot | null, next: UpdateSnapshot): UpdateSnapshot {
  return current && current.revision > next.revision ? current : next;
}
export function updatePercent(update: UpdateSnapshot | null): number | null {
  return update?.total ? Math.min(100, Math.round(update.downloaded / update.total * 100)) : null;
}
export function downloadSize(bytes: number): string {
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
