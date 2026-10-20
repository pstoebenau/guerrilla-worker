import { describe, expect, spyOn, test } from 'bun:test';
import { mkdir, mkdtemp, readFile, readdir, rename, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { extractRuntimeArchive, renameRuntimeDirectory, sha256, verifiedDownload } from '../scripts/vendor-runtime';
import { windowsPowerShellEnvironment } from '../scripts/windows-powershell';

describe('vendored runtime integrity', () => {
  test.skipIf(process.platform !== 'win32')('runtime publication waits for a transient Windows file lock and rejects a persistent lock', async () => {
    const temporary = await mkdtemp(path.join(tmpdir(), 'guerrilla-runtime-lock-'));
    try {
      for (const releaseLock of [true, false]) {
        const source = path.join(temporary, `source-${releaseLock}`);
        const destination = path.join(temporary, `runtime-${releaseLock}`);
        const ready = path.join(temporary, `ready-${releaseLock}`);
        const release = path.join(temporary, `release-${releaseLock}`);
        await mkdir(source);
        const payload = path.join(source, 'payload.txt');
        await writeFile(payload, 'verified runtime');
        const child = Bun.spawn(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', `
          $ErrorActionPreference = 'Stop'
          $file = [IO.File]::Open($env:GUERRILLA_LOCK_FILE, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
          try {
            [IO.File]::WriteAllText($env:GUERRILLA_LOCK_READY, 'ready')
            while (![IO.File]::Exists($env:GUERRILLA_LOCK_RELEASE)) { [Threading.Thread]::Sleep(20) }
          } finally { $file.Dispose() }
        `], {
          env: windowsPowerShellEnvironment({ ...process.env, GUERRILLA_LOCK_FILE: payload, GUERRILLA_LOCK_READY: ready, GUERRILLA_LOCK_RELEASE: release }),
          stdin: 'ignore', stdout: 'ignore', stderr: 'pipe', windowsHide: true,
        });
        try {
          const deadline = Date.now() + 15_000;
          while (!(await Bun.file(ready).exists()) && Date.now() < deadline && child.exitCode === null) await Bun.sleep(20);
          expect(await Bun.file(ready).exists()).toBe(true);
          await expect(rename(source, destination)).rejects.toThrow();
          if (releaseLock) {
            const unlocked = Bun.sleep(200).then(() => writeFile(release, 'release'));
            await renameRuntimeDirectory(source, destination);
            await unlocked;
            expect(await readFile(path.join(destination, 'payload.txt'), 'utf8')).toBe('verified runtime');
          } else {
            await expect(renameRuntimeDirectory(source, destination)).rejects.toThrow();
            expect(await readFile(payload, 'utf8')).toBe('verified runtime');
            expect(await Bun.file(path.join(destination, 'payload.txt')).exists()).toBe(false);
          }
        } finally {
          await writeFile(release, 'release');
          const exited = await Promise.race([child.exited, Bun.sleep(5000).then(() => null)]);
          if (exited === null) child.kill();
          expect(await child.exited, await new Response(child.stderr).text()).toBe(0);
        }
      }
    } finally { await rm(temporary, { recursive: true, force: true }); }
  }, 60_000);

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
  test('a transient upstream error retries and still verifies downloaded bytes', async () => {
    const cache = await mkdtemp(path.join(tmpdir(), 'guerrilla-runtime-retry-'));
    const source = path.join(cache, 'source');
    await writeFile(source, 'pinned bytes');
    const digest = await sha256(source);
    const request = spyOn(globalThis, 'fetch').mockResolvedValueOnce(new Response('temporary upstream failure', { status: 500 })).mockResolvedValueOnce(new Response('pinned bytes'));
    const sleep = spyOn(Bun, 'sleep').mockResolvedValue(undefined);
    try {
      const file = await verifiedDownload({ url: 'https://example.invalid/runtime.zip', sha256: digest }, cache);
      expect(request).toHaveBeenCalledTimes(2);
      expect(await readFile(file, 'utf8')).toBe('pinned bytes');
    } finally { request.mockRestore(); sleep.mockRestore(); await rm(cache, { recursive: true, force: true }); }
  });
  test('download retries are bounded and a checksum failure never becomes a cached runtime', async () => {
    const cache = await mkdtemp(path.join(tmpdir(), 'guerrilla-runtime-retry-'));
    const request = spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 503 }));
    const sleep = spyOn(Bun, 'sleep').mockResolvedValue(undefined);
    const asset = { url: 'https://example.invalid/runtime.zip', sha256: 'a'.repeat(64) };
    try {
      await expect(verifiedDownload(asset, cache)).rejects.toThrow('HTTP 503');
      expect(request).toHaveBeenCalledTimes(4);
      request.mockReset().mockResolvedValue(new Response('corrupt payload'));
      await expect(verifiedDownload(asset, cache)).rejects.toThrow('checksum mismatch');
      expect(request).toHaveBeenCalledTimes(1);
      expect(await Bun.file(path.join(cache, `${asset.sha256}-runtime.zip`)).exists()).toBe(false);
    } finally { request.mockRestore(); sleep.mockRestore(); await rm(cache, { recursive: true, force: true }); }
  });
  test('a response body disconnect retries from scratch before caching', async () => {
    const cache = await mkdtemp(path.join(tmpdir(), 'guerrilla-runtime-stream-'));
    const bytes = 'verified replacement';
    const digest = new Bun.CryptoHasher('sha256').update(bytes).digest('hex');
    let pulls = 0;
    const broken = new Response(new ReadableStream({
      pull(controller) {
        if (pulls++ === 0) controller.enqueue(new TextEncoder().encode('partial bytes longer than the replacement'));
        else controller.error(new Error('connection reset'));
      },
    }));
    const request = spyOn(globalThis, 'fetch').mockResolvedValueOnce(broken).mockResolvedValueOnce(new Response(bytes));
    const sleep = spyOn(Bun, 'sleep').mockResolvedValue(undefined);
    try {
      const file = await verifiedDownload({ url: 'https://example.invalid/runtime.zip', sha256: digest }, cache);
      expect(request).toHaveBeenCalledTimes(2);
      expect(await readFile(file, 'utf8')).toBe(bytes);
      expect(await readdir(cache)).toEqual([path.basename(file)]);
    } finally { request.mockRestore(); sleep.mockRestore(); await rm(cache, { recursive: true, force: true }); }
  });
  test('a missing asset reports its URL without retrying or leaving partial files', async () => {
    const cache = await mkdtemp(path.join(tmpdir(), 'guerrilla-runtime-missing-'));
    const url = 'https://example.invalid/missing.zip';
    const request = spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 404 }));
    try {
      await expect(verifiedDownload({ url, sha256: 'a'.repeat(64) }, cache)).rejects.toThrow(`HTTP 404: ${url}`);
      expect(request).toHaveBeenCalledTimes(1);
      expect(await readdir(cache)).toEqual([]);
    } finally { request.mockRestore(); await rm(cache, { recursive: true, force: true }); }
  });
});
