import { describe, expect, test } from 'bun:test';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { sha256, verifiedDownload } from '../scripts/vendor-runtime';

describe('vendored runtime integrity', () => {
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
