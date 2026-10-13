import { expect, test } from 'bun:test';
import { validateEngineArtifact, type EngineLock } from '../scripts/build-docker';

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
