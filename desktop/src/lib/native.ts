import { invoke, isTauri } from '@tauri-apps/api/core';
import type { AvailableUpdate, BrowseField, DesktopInfo, Logs, Preflight, Profile, WorkerAction, WorkerStatus } from './types';
import { parsePreflight, parseStatus } from './parse';

export const nativeAvailable = isTauri();
export function native<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  if (!nativeAvailable) return Promise.reject(new Error('Open the installed Guerrilla Worker desktop app to manage this worker.'));
  return invoke<T>(command, args);
}
export const api = {
  info: () => native<DesktopInfo>('desktop_info'),
  status: async (): Promise<WorkerStatus> => parseStatus(await native<unknown>('worker_status')),
  preflight: async (): Promise<Preflight> => parsePreflight(await native<unknown>('worker_preflight')),
  installGpuRuntime: () => native<DesktopInfo>('install_gpu_runtime'),
  save: (profile: Profile) => native<DesktopInfo>('save_profile', { profile }),
  enroll: async (token: string, controlUrl: string): Promise<WorkerStatus> => parseStatus(await native<unknown>('enroll_worker', { token, controlUrl })),
  action: async (action: WorkerAction): Promise<WorkerStatus> => parseStatus(await native<unknown>('worker_action', { action })),
  logs: (source?: string) => native<Logs>('worker_logs', { source }),
  openLogFolder: (source: string) => native<void>('open_log_directory', { source }),
  browse: (field: BrowseField) => native<string | null>('browse_path', { field }),
  openServer: () => native<void>('open_server'),
  checkForUpdates: () => native<AvailableUpdate | null>('check_for_updates'),
  openRelease: (url: string) => native<void>('open_release', { url }),
  openData: () => native<void>('open_data_directory'),
  quit: () => native<void>('quit_interface'),
};
