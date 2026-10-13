export interface Profile {
  controlUrl: string;
  scratchDirectory: string;
  pythonPath: string;
  ffmpegPath: string;
  nodePath: string;
  converterPath: string;
  lichtfeldPath: string;
  densificationPluginPath: string;
  spirulaPath: string;
  startAtLogin: boolean;
}

export type WorkerState = 'stopped' | 'starting' | 'idle' | 'busy' | 'draining' | 'error' | 'unenrolled';
export interface EngineStatus { name: string; available: boolean; message?: string; version?: string }
export interface GpuStatus { name: string; memoryMb?: number; index?: number; uuid?: string }
export interface WorkerStatus {
  state: WorkerState;
  running?: boolean;
  connected: boolean;
  workerId?: string;
  jobId?: string;
  backend?: string;
  stage?: string;
  progress?: number;
  message?: string;
  updatedAt?: string;
  gpus?: GpuStatus[];
  engines?: EngineStatus[];
}
export interface DesktopInfo {
  profile: Profile;
  profileDirectory: string;
  enrolled: boolean;
  runtimeAvailable: boolean;
  version: string;
}
export interface AvailableUpdate { version: string; releaseUrl: string }
export interface DependencyCheck { name: string; available: boolean; message?: string; version?: string }
export interface Preflight { ok: boolean; checks?: DependencyCheck[]; engines?: EngineStatus[]; gpus?: GpuStatus[]; message?: string }
export interface Logs { lines: string[]; truncated?: boolean }
export type WorkerAction = 'start' | 'stop' | 'drain' | 'disconnect';
export type BrowseField = 'scratchDirectory' | 'lichtfeldPath' | 'densificationPluginPath' | 'spirulaPath';

export function isRunning(status: WorkerStatus | null): boolean {
  if (status?.running !== undefined) return status.running;
  return status !== null && ['starting', 'idle', 'busy', 'draining'].includes(status.state);
}
export function progressPercent(value: number | undefined): number | null {
  if (value === undefined || !Number.isFinite(value)) return null;
  return Math.round(Math.min(1, Math.max(0, value)) * 100);
}
export function statusTitle(status: WorkerStatus | null, native: boolean): string {
  if (!native) return 'Desktop runtime required';
  if (!status) return 'Checking worker';
  return { stopped: 'Worker stopped', starting: 'Worker starting', idle: 'Ready for work', busy: 'Processing a job', draining: 'Finishing current job', error: 'Worker needs attention', unenrolled: 'Connect this worker' }[status.state];
}
