import type { Registration } from '@guerrilla/worker-protocol';

export function gpuInventory(report: { gpus?: unknown }): Registration['gpus'] {
  if (Number(process.env.WORKER_GPU_INDEX ?? 0) !== 0) throw new Error('The worker requires GPU index 0; isolate devices before starting');
  const devices = report.gpus as Registration['gpus'] | undefined;
  if (!Array.isArray(devices) || devices.length !== 1 ||
      !/^[\w.-]{1,128}$/.test(devices[0]?.id ?? '') ||
      typeof devices[0]?.name !== 'string' || !devices[0].name || devices[0].name.length > 200 ||
      !Number.isSafeInteger(devices[0]?.memoryBytes) || devices[0].memoryBytes < 1) {
    throw new Error('No usable hardware GPU. Check engine dependencies with --preflight.');
  }
  return devices;
}
