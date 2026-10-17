import { expect, test } from 'bun:test';
import { developmentEnvironment, workerEnvironment } from '../scripts/development-environment';

test('execution never inherits platform credentials or interpreter injection', () => {
  const source = {
    PATH: 'runtime', DATABASE_URL: 'secret', AWS_ACCESS_KEY_ID: 'secret', AWS_SECRET_ACCESS_KEY: 'secret',
    RUNPOD_API_KEY: 'secret', BETTER_AUTH_SECRET: 'secret', NODE_OPTIONS: '--require evil',
    PYTHONPATH: 'evil', LD_PRELOAD: 'evil', DYLD_INSERT_LIBRARIES: 'evil',
    WORKER_CONTROL_URL: 'https://example.com', WORKER_CREDENTIAL_FILE: 'credential.json',
    WORKER_ENROLLMENT_TOKEN: 'owner-scoped', WORKER_IMAGE: 'local-worker', DOCKER_HOST: 'unix:///socket',
    CARGO_HOME: '/build/cargo',
  };
  const env = workerEnvironment(source, '/worker');
  for (const key of ['DATABASE_URL', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'RUNPOD_API_KEY', 'BETTER_AUTH_SECRET',
    'NODE_OPTIONS', 'PYTHONPATH', 'LD_PRELOAD', 'DYLD_INSERT_LIBRARIES', 'WORKER_IMAGE', 'DOCKER_HOST', 'CARGO_HOME']) expect(env[key]).toBeUndefined();
  expect(env.PATH).toBe('runtime');
  expect(env.WORKER_CONTROL_URL).toBe('https://example.com');
  expect(env.WORKER_ENROLLMENT_TOKEN).toBe('owner-scoped');
});

test('development allows explicit state, image and build configuration before narrowing execution', () => {
  const approved = {
    WORKER_IMAGE: 'worker:worktree', WORKER_CONTAINER_NAME: 'isolated', WORKER_GPU_DEVICE: '1',
    WORKER_GPU_DIRECTORY: '/state/gpu', WORKER_CREDENTIAL_DIRECTORY: '/state/credentials',
    WORKER_SCRATCH: '/state/scratch', WORKER_RUNTIME_DIRECTORY: '/state/runtimes',
    WORKER_BUILD_CACHE: '/build/cache', DOCKER_CONFIG: '/docker/config', CARGO_HOME: '/build/cargo',
  };
  const env = developmentEnvironment({ ...approved, ORIGIN: 'http://127.0.0.1:6100', STRIPE_SECRET_KEY: 'secret', BUN_OPTIONS: 'injection' }, '/worker');
  for (const [key, value] of Object.entries(approved)) expect(env[key]).toBe(value);
  expect(env.WORKER_CONTROL_URL).toBe('http://127.0.0.1:6100');
  expect(env.STRIPE_SECRET_KEY).toBeUndefined();
  expect(env.BUN_OPTIONS).toBeUndefined();
});

test('Windows runtime configuration is case insensitive and normalizes PATH', () => {
  const env = developmentEnvironment({ path: 'executables', systemroot: 'windows', worker_control_url: 'https://owner', worker_gpu_device: '2', node_options: 'injection' }, '/worker', 'win32');
  expect(env.PATH).toBe('executables');
  expect(env.SystemRoot).toBe('windows');
  expect(env.WORKER_CONTROL_URL).toBe('https://owner');
  expect(env.WORKER_GPU_DEVICE).toBe('2');
  expect(env.WORKER_MODE).toBe('native-development');
  expect(env.NODE_OPTIONS).toBeUndefined();
});
