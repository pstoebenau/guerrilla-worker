import { expect, test } from 'bun:test';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { buildImages, validateEngineArtifact, type EngineLock } from '../scripts/build-docker';

const lock: EngineLock = {
  recipe: 'a'.repeat(64), releaseUrl: 'https://example.invalid/releases/tag/recipe',
  archive: { url: 'https://example.invalid/engine.tar.gz', sha256: 'b'.repeat(64) },
  metadata: { url: 'https://example.invalid/engine-artifact.json', sha256: 'c'.repeat(64) },
  checksums: { url: 'https://example.invalid/SHA256SUMS.txt', sha256: 'd'.repeat(64) },
};
const metadata = { version: 1, recipe: lock.recipe, archive: 'lichtfeld-linux-amd64.tar.gz', sha256: lock.archive.sha256, platform: 'linux-amd64', cpuBaseline: 'x86-64-v3' };
const checksums = [
  `${lock.archive.sha256}  lichtfeld-linux-amd64.tar.gz`, `${lock.metadata.sha256}  engine-artifact.json`,
  `${'e'.repeat(64)}  build-source-inventory.json`, `${'f'.repeat(64)}  lichtfeld-source.tar.gz.part000`,
  `${'1'.repeat(64)}  lichtfeld-build-dependencies.tar.gz.part000`,
].join('\n');

test('a pinned archive must also match its recipe, platform, checksum manifest and matching source evidence', () => {
  expect(() => validateEngineArtifact(lock, metadata, checksums)).not.toThrow();
  expect(() => validateEngineArtifact(lock, { ...metadata, recipe: 'f'.repeat(64) }, checksums)).toThrow('recipe');
  expect(() => validateEngineArtifact(lock, { ...metadata, platform: 'windows-x64' }, checksums)).toThrow('platform');
  expect(() => validateEngineArtifact(lock, metadata, checksums.replace(lock.archive.sha256, '0'.repeat(64)))).toThrow('pinned assets');
  expect(() => validateEngineArtifact(lock, metadata, checksums.split('\n').slice(0, 3).join('\n'))).toThrow('source evidence');
  expect(() => validateEngineArtifact(lock, metadata, `${checksums}\n${lock.archive.sha256}  lichtfeld-linux-amd64.tar.gz`)).toThrow('manifest');
});

test('published workers use the verified runtime digest and the selected release version', async () => {
  const context = await mkdtemp(path.join(tmpdir(), 'docker-release-test-'));
  const digest = `sha256:${'a'.repeat(64)}`;
  const commands: string[][] = [];
  const metadataFile = path.join(context, 'published-worker.json');
  try {
    await buildImages({ runtimeImage: 'ghcr.io/owner/worker-runtime:v0.3.5', workerImage: 'ghcr.io/owner/worker:v0.3.5',
      version: '0.3.5', push: true, metadataFile }, lock, context, async args => {
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
    expect(commands[0]).toContain(`lichtfeld-artifact=${context}`);
    expect(commands[0]).toContain(`LICHTFELD_SHA256=${lock.archive.sha256}`);
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
      version: '1.0.0', push: true }, lock, context, async args => {
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
    lock, '/tmp/verified-engine-context', async args => { commands.push(args); });
  expect(commands).toHaveLength(2);
  expect(commands[1]).toContain('WORKER_RUNTIME_IMAGE=runtime:local');
  for (const command of commands) {
    expect(command.slice(0, 2)).toEqual(['docker', 'build']);
    expect(command).not.toContain('--push');
    expect(command).not.toContain('--cache-to');
  }
});
