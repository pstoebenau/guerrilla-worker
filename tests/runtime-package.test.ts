import { describe, expect, test } from 'bun:test';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { extractRuntimeArchive, sha256, verifiedDownload } from '../scripts/vendor-runtime';

describe('vendored runtime integrity', () => {
  test('runtime archives extract from absolute paths and preserve the required layout', async () => {
    const temporary = await mkdtemp(path.join(tmpdir(), 'guerrilla-runtime-archive-'));
    const tar = process.platform === 'win32' ? path.join(process.env.SystemRoot ?? process.env.SYSTEMROOT!, 'System32', 'tar.exe') : 'tar';
    try {
      const source = path.join(temporary, 'source');
      await mkdir(path.join(source, 'runtime'), { recursive: true });
      await writeFile(path.join(source, 'runtime', 'payload.txt'), 'runtime payload');
      for (const extension of process.platform === 'win32' ? ['tar.gz', 'zip'] : ['tar.gz']) {
        const archive = path.join(temporary, `runtime.${extension}`);
        const result = Bun.spawnSync([tar, '-a', '-cf', archive, '-C', source, 'runtime'], { stdout: 'pipe', stderr: 'pipe' });
        expect(result.exitCode, result.stderr.toString()).toBe(0);
        for (const strip of [0, 1]) {
          const destination = path.join(temporary, `extracted-${extension}-${strip}`);
          await mkdir(destination);
          await extractRuntimeArchive(archive, destination, strip);
          expect(await readFile(path.join(destination, ...(strip ? [] : ['runtime']), 'payload.txt'), 'utf8')).toBe('runtime payload');
        }
      }
    } finally { await rm(temporary, { recursive: true, force: true }); }
  }, 30_000);

  test('a tampered cached download is refused rather than reused', async () => {
    const cache = await mkdtemp(path.join(tmpdir(), 'guerrilla-runtime-test-'));
    const digest = 'a'.repeat(64);
    try {
      await writeFile(path.join(cache, `${digest}-python.tar.gz`), 'corrupt');
      await expect(verifiedDownload({ url: 'https://example.invalid/python.tar.gz', sha256: digest }, cache)).rejects.toThrow('checksum mismatch');
    } finally { await rm(cache, { recursive: true, force: true }); }
  });
  test('verified cached bytes work without a download', async () => {
    const cache = await mkdtemp(path.join(tmpdir(), 'guerrilla-runtime-test-'));
    try {
      const source = path.join(cache, 'source');
      await writeFile(source, 'known runtime bytes');
      const digest = await sha256(source);
      const file = path.join(cache, `${digest}-runtime.zip`);
      await writeFile(file, 'known runtime bytes');
      expect(await verifiedDownload({ url: 'https://example.invalid/runtime.zip', sha256: digest }, cache)).toBe(file);
    } finally { await rm(cache, { recursive: true, force: true }); }
  });
  test('unencrypted and unpinned assets fail before fetching', async () => {
    await expect(verifiedDownload({ url: 'http://example.invalid/runtime.zip', sha256: 'a'.repeat(64) }, tmpdir())).rejects.toThrow('identity');
    await expect(verifiedDownload({ url: 'https://example.invalid/runtime.zip', sha256: 'missing' }, tmpdir())).rejects.toThrow('identity');
  });
});
