import { expect, test } from 'bun:test';
import { mkdir, mkdtemp, readFile, readdir, realpath, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { stageWindowsCrtResources, vendorWindowsCrt } from '../scripts/windows-crt';
import { sha256 } from '../scripts/vendor-runtime';

test('Windows bundle keeps separate matching app-root CRT copies and retires stale DLLs', async () => {
  const tauri = await mkdtemp(path.join(tmpdir(), 'guerrilla-crt-bundle-'));
  try {
    const runtime = path.join(tauri, 'resources/runtime/windows-crt');
    await mkdir(runtime, { recursive: true });
    const files = ['msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'concrt140.dll', 'vcomp140.dll', 'MSVCP140_ATOMIC_WAIT.dll'];
    for (const file of files) await writeFile(path.join(runtime, file), `signed fixture: ${file}`);
    await writeFile(path.join(runtime, 'manifest.json'), '{}');
    const resources = await stageWindowsCrtResources(tauri);
    expect(Object.keys(resources)).toHaveLength(files.length);
    for (const file of files) {
      const source = `target/windows-crt/${file}`;
      expect(resources[source]).toBe(file);
      const staged = path.join(tauri, source);
      expect(await realpath(staged)).not.toBe(await realpath(path.join(runtime, file)));
      expect(await readFile(staged)).toEqual(await readFile(path.join(runtime, file)));
    }
    await writeFile(path.join(runtime, 'msvcp140.dll'), 'replacement signed fixture');
    expect(await readFile(path.join(tauri, 'target/windows-crt/msvcp140.dll'), 'utf8')).toBe('signed fixture: msvcp140.dll');
    await rm(path.join(runtime, 'vcomp140.dll'));
    await writeFile(path.join(tauri, 'target/windows-crt/obsolete.dll'), 'obsolete');
    const replacement = await stageWindowsCrtResources(tauri);
    expect(Object.values(replacement).sort()).toEqual(files.filter(file => file !== 'vcomp140.dll').sort());
    expect((await readdir(path.join(tauri, 'target/windows-crt'))).sort()).toEqual(Object.values(replacement).sort());
    expect(await readFile(path.join(tauri, 'target/windows-crt/msvcp140.dll'), 'utf8')).toBe('replacement signed fixture');
    expect(await readFile(path.join(runtime, 'vcruntime140.dll'), 'utf8')).toBe('signed fixture: vcruntime140.dll');
  } finally { await rm(tauri, { recursive: true, force: true }); }
});

test('Windows CRT bundle staging requires all app-local runtime DLLs', async () => {
  const tauri = await mkdtemp(path.join(tmpdir(), 'guerrilla-crt-incomplete-'));
  try {
    await mkdir(path.join(tauri, 'resources/runtime/windows-crt'), { recursive: true });
    await expect(stageWindowsCrtResources(tauri)).rejects.toThrow('Missing app-local runtime msvcp140.dll');
  } finally { await rm(tauri, { recursive: true, force: true }); }
});

test.skipIf(process.platform !== 'win32')('licensed Windows CRT stages signed app-local dependencies with matching copies', async () => {
  const staging = await mkdtemp(path.join(tmpdir(), 'guerrilla-crt-test-'));
  try {
    await vendorWindowsCrt(staging);
    const manifest = JSON.parse(await readFile(path.join(staging, 'windows-crt', 'manifest.json'), 'utf8'));
    for (const required of ['msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll']) {
      expect(manifest.files.some((file: { fileName: string }) => file.fileName.toLowerCase() === required)).toBe(true);
    }
    for (const file of manifest.files) {
      expect(await sha256(path.join(staging, 'windows-crt', file.fileName))).toBe(file.sha256);
    }
  } finally { await rm(staging, { recursive: true, force: true }); }
}, 60_000);
