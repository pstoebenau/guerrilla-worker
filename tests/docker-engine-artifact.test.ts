import { expect, test } from 'bun:test';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { buildImages } from '../scripts/build-docker';

test('published workers use the verified runtime digest and the selected release version', async () => {
  const context = await mkdtemp(path.join(tmpdir(), 'docker-release-test-'));
  const digest = `sha256:${'a'.repeat(64)}`;
  const commands: string[][] = [];
  const metadataFile = path.join(context, 'published-worker.json');
  try {
    await buildImages({ runtimeImage: 'ghcr.io/owner/worker-runtime:v0.3.5', workerImage: 'ghcr.io/owner/worker:v0.3.5',
      version: '0.3.5', push: true, metadataFile }, context, async args => {
      commands.push(args);
      if (commands.length === 1) {
        await writeFile(args[args.indexOf('--metadata-file') + 1]!, JSON.stringify({ 'containerimage.digest': digest }));
      }
    });
    expect(commands).toHaveLength(2);
    for (const command of commands) {
      expect(command.slice(0, 4)).toEqual(['docker', 'buildx', 'build', '--push']);
      expect(command).toContain('org.opencontainers.image.version=0.3.5');
      expect(command).toContain('linux/amd64');
    }
    expect(commands[0]).toContain('spirula-runtime');
    expect(commands[1]).toContain(`WORKER_RUNTIME_IMAGE=ghcr.io/owner/worker-runtime@${digest}`);
    expect(commands[1]).toContain('WORKER_VERSION=0.3.5');
    expect(commands[1]).toContain(metadataFile);
    expect(commands[1]).toContain('type=registry,ref=ghcr.io/owner/worker:buildcache,mode=max');
  } finally {
    await rm(context, { recursive: true, force: true });
  }
});

test('a missing runtime digest prevents worker publication', async () => {
  const context = await mkdtemp(path.join(tmpdir(), 'docker-release-test-'));
  let builds = 0;
  try {
    await expect(buildImages({ runtimeImage: 'ghcr.io/owner/runtime:v1.0.0', workerImage: 'ghcr.io/owner/worker:v1.0.0',
      version: '1.0.0', push: true }, context, async args => {
      builds++;
      await writeFile(args[args.indexOf('--metadata-file') + 1]!, '{}');
    })).rejects.toThrow('Missing published runtime digest');
    expect(builds).toBe(1);
  } finally {
    await rm(context, { recursive: true, force: true });
  }
});

test('default local builds keep both images local and share the local runtime', async () => {
  const commands: string[][] = [];
  await buildImages({ runtimeImage: 'runtime:local', workerImage: 'worker:local', version: '0.3.4', push: false },
    '/tmp/image-build-context', async args => { commands.push(args); });
  expect(commands).toHaveLength(2);
  expect(commands[1]).toContain('WORKER_RUNTIME_IMAGE=runtime:local');
  for (const command of commands) {
    expect(command.slice(0, 2)).toEqual(['docker', 'build']);
    expect(command).not.toContain('--push');
    expect(command).not.toContain('--cache-to');
  }
});
