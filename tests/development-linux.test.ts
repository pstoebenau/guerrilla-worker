import { expect, test } from 'bun:test';
import { linuxWorkerArguments } from '../scripts/development-linux';

const options = {
  credentialDirectory: '/state/worktree/worker-credentials', scratchDirectory: '/state/worktree/scratch',
  gpuDirectory: '/state/gpu', workerImage: 'worker@sha256:test', containerName: 'isolated-enrollment-command',
};

test('Linux enrollment shares dedicated credentials, scratch, loopback and GPU lock with scoped transfers', () => {
  const args = linuxWorkerArguments(options, ['enroll', '--token-stdin']);
  expect(args).toContain('host');
  expect(args).toContain('isolated-enrollment-command');
  expect(args).toContain('device=0');
  expect(args).toContain(`type=bind,source=${options.credentialDirectory},target=/credentials`);
  expect(args).toContain(`type=bind,source=${options.scratchDirectory},target=/scratch`);
  expect(args).toContain(`type=bind,source=${options.gpuDirectory},target=/gpu`);
  expect(args).toContain('WORKER_CREDENTIAL_FILE=/credentials/worker.json');
  expect(args).toContain('WORKER_AGENT_LOCK_DIRECTORY=/gpu');
  expect(args).toContain('WORKER_ENROLLMENT_TOKEN');
  expect(args.slice(-3)).toEqual([options.workerImage, 'enroll', '--token-stdin']);
  expect(args.join(' ')).not.toMatch(/DATABASE_URL|AWS_SECRET|RUNPOD_API|docker.sock/);
});

test('Linux GPU selection identifies exactly one GPU', () => {
  for (const gpuDevice of ['1', 'GPU-a1B2-3344']) expect(linuxWorkerArguments({ ...options, gpuDevice }, ['--preflight'])).toContain(`device=${gpuDevice}`);
  for (const gpuDevice of ['all', '0,1', 'device=0', '-1', 'GPU-nope']) expect(() => linuxWorkerArguments({ ...options, gpuDevice }, [])).toThrow('identify one GPU');
});
