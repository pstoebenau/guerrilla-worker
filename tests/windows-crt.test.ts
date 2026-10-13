import { expect, test } from 'bun:test';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { vendorWindowsCrt } from '../scripts/windows-crt';
import { sha256 } from '../scripts/vendor-runtime';

test.skipIf(process.platform !== 'win32')('licensed Windows CRT stages signed app-local dependencies with matching copies', async () => {
  const staging = await mkdtemp(path.join(tmpdir(), 'guerrilla-crt-test-'));
  try {
    await mkdir(path.join(staging, 'python'));
    // CRT staging checks the layout but does not execute the Python interpreter.
    await writeFile(path.join(staging, 'python', 'python.exe'), 'staging placeholder');
    await vendorWindowsCrt(staging);
    const manifest = JSON.parse(await readFile(path.join(staging, 'windows-crt', 'manifest.json'), 'utf8'));
    for (const required of ['msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll']) {
      expect(manifest.files.some((file: { fileName: string }) => file.fileName.toLowerCase() === required)).toBe(true);
    }
    for (const file of manifest.files) {
      expect(await sha256(path.join(staging, 'windows-crt', file.fileName))).toBe(file.sha256);
      expect(await sha256(path.join(staging, 'python', file.fileName))).toBe(file.sha256);
    }
  } finally { await rm(staging, { recursive: true, force: true }); }
}, 60_000);
