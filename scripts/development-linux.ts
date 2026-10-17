export type LinuxWorkerOptions = {
  credentialDirectory: string;
  scratchDirectory: string;
  gpuDirectory: string;
  workerImage: string;
  containerName: string;
  gpuDevice?: string;
};

/** One GPU, dedicated owner state and outbound protocol credentials only. */
export function linuxWorkerArguments(options: LinuxWorkerOptions, args: string[]): string[] {
  const gpu = options.gpuDevice ?? '0';
  if (!/^(\d+|GPU-[a-fA-F0-9-]+)$/.test(gpu)) throw new Error('WORKER_GPU_DEVICE must identify one GPU');
  return [
    'run', '--rm', '--name', options.containerName, '-i', '--network', 'host',
    '--gpus', `device=${gpu}`,
    '--mount', `type=bind,source=${options.credentialDirectory},target=/credentials`,
    '--mount', `type=bind,source=${options.scratchDirectory},target=/scratch`,
    '--mount', `type=bind,source=${options.gpuDirectory},target=/gpu`,
    '--env', 'WORKER_CONTROL_URL', '--env', 'WORKER_ENROLLMENT_TOKEN',
    '--env', 'WORKER_CREDENTIAL_FILE=/credentials/worker.json',
    '--env', 'WORKER_SCRATCH=/scratch', '--env', 'WORKER_AGENT_LOCK_DIRECTORY=/gpu',
    '--env', 'GPU_LOCK_PATH=/gpu/gpu-0.lock', '--env', 'GPU_LOCK_WAIT=1',
    options.workerImage, ...args,
  ];
}
