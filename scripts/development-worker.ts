import path from 'node:path';
import { homedir } from 'node:os';
import { mkdir } from 'node:fs/promises';
import { developmentEnvironment, workerEnvironment } from './development-environment';
import { linuxWorkerArguments } from './development-linux';
import { prepareDevelopmentRuntime } from './development-runtime';
import { runDevelopmentCommand } from './development-process';
import { buildDocker } from './build-docker';

const sourceDirectory = path.resolve(import.meta.dir, '..');

export type DevelopmentWorkerOptions = {
  args?: string[];
  env?: NodeJS.ProcessEnv;
  controlURL?: string;
  credentialDirectory?: string;
  scratchDirectory?: string;
  gpuDirectory?: string;
  workerImage?: string;
  containerName?: string;
};

export async function runDevelopmentWorker(options: DevelopmentWorkerOptions = {}): Promise<number> {
  if (!['win32', 'linux', 'darwin'].includes(process.platform)) throw new Error('Local workers require Windows/Linux with NVIDIA or an Apple Silicon Mac.');
  if (process.platform === 'darwin' && process.arch !== 'arm64') throw new Error('Mac workers require Apple Silicon (arm64).');
  const args = options.args ?? [];
  const env = developmentEnvironment(options.env ?? process.env, sourceDirectory);
  const state = path.join(env.LOCALAPPDATA ?? path.join(homedir(), '.local/state'), 'Guerrilla');
  const credentialDirectory = options.credentialDirectory ?? env.WORKER_CREDENTIAL_DIRECTORY
    ?? (env.WORKER_CREDENTIAL_FILE ? path.dirname(env.WORKER_CREDENTIAL_FILE) : path.join(state, 'worker/credentials'));
  const scratchDirectory = options.scratchDirectory ?? env.WORKER_SCRATCH ?? path.join(state, 'worker/scratch');
  const gpuDirectory = options.gpuDirectory ?? env.WORKER_GPU_DIRECTORY ?? path.join(state, 'gpu');
  const workerImage = options.workerImage ?? env.WORKER_IMAGE ?? 'guerrilla-worker:local';
  const containerName = options.containerName ?? env.WORKER_CONTAINER_NAME ?? `guerrilla-worker-${process.pid}`;
  env.WORKER_CONTROL_URL = options.controlURL ?? env.WORKER_CONTROL_URL;
  if (args.includes('--docker-build')) {
    await buildDocker(['--worker-image', workerImage], env);
    return 0;
  }
  if (process.platform === 'linux') {
    if (args.includes('--prepare-only')) return 0;
    for (const directory of [credentialDirectory, scratchDirectory, gpuDirectory]) await mkdir(directory, { recursive: true });
    return (await runDevelopmentCommand('docker', linuxWorkerArguments({
      credentialDirectory, scratchDirectory, gpuDirectory, workerImage, containerName, gpuDevice: env.WORKER_GPU_DEVICE,
    }, args), { env, cwd: sourceDirectory, containerName })).code;
  }
  const runtime = await prepareDevelopmentRuntime(sourceDirectory, env.WORKER_RUNTIME_DIRECTORY ?? path.join(state, 'runtimes'));
  if (args.includes('--prepare-only')) { console.log('Prepared independent worker runtime.'); return 0; }
  const execution = workerEnvironment({
    ...env,
    SPLAT_TRANSFORM_BIN: env.SPLAT_TRANSFORM_BIN ?? path.join(sourceDirectory, 'pipeline/docker/converter/node_modules/@playcanvas/splat-transform/bin/cli.mjs'),
    WORKER_CREDENTIAL_FILE: env.WORKER_CREDENTIAL_FILE ?? path.join(credentialDirectory, 'worker.json'),
    WORKER_SCRATCH: scratchDirectory,
    GPU_LOCK_PATH: env.GPU_LOCK_PATH ?? path.join(gpuDirectory, 'gpu-0.lock'),
    GPU_LOCK_WAIT: env.GPU_LOCK_WAIT ?? '1',
  }, sourceDirectory);
  execution.PIPELINE_ROOT = path.join(runtime, 'pipeline');
  execution.WORKER_BUNDLE = path.join(runtime, 'worker.mjs');
  const node = Bun.which('node');
  if (!node) throw new Error('Node.js is required for the worker.');
  return (await runDevelopmentCommand(node, [execution.WORKER_BUNDLE, ...args], { env: execution, cwd: runtime, ipc: true })).code;
}

if (import.meta.main) {
  try { process.exitCode = await runDevelopmentWorker({ args: process.argv.slice(2) }); }
  finally { if (process.connected) process.disconnect(); }
}
