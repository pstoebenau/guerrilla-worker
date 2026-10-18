import { expect, test } from 'bun:test';
import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';

test.skipIf(process.platform !== 'win32')('generated NSIS script installs every CRT DLL at app root and inside the runtime', async () => {
  const tauri = path.resolve(import.meta.dir, '../desktop/src-tauri');
  const script = await readFile(path.join(tauri, 'target/release/nsis/x64/installer.nsi'), 'utf8');
  const normalize = (value: string) => value.replaceAll('\\', '/').toLowerCase();
  const resources = [...script.matchAll(/^\s*File \/a "\/oname=([^"]+)" "([^"]+)"\s*$/gm)]
    .map(match => ({ target: normalize(match[1]!), source: normalize(match[2]!) }));
  const runtime = path.join(tauri, 'resources/runtime/windows-crt');
  const files = (await readdir(runtime)).filter(file => file.toLowerCase().endsWith('.dll'));
  expect(files.length).toBeGreaterThanOrEqual(3);
  for (const file of files) {
    expect(resources).toContainEqual({ target: normalize(file), source: normalize(path.join(tauri, 'target/windows-crt', file)) });
    expect(resources).toContainEqual({ target: normalize(`resources/runtime/windows-crt/${file}`), source: normalize(path.join(runtime, file)) });
  }
});

test.skipIf(process.platform !== 'win32')('native Tauri NSIS build has one signed, self-contained installer', async () => {
  // Actual installation, legacy migration, upgrade and uninstall run in the
  // dedicated CI smoke script after this package integration check.
  const bundle = path.resolve(import.meta.dir, '../desktop/src-tauri/target/release/bundle/nsis');
  const files = await readdir(bundle);
  const installers = files.filter(file => file.endsWith('-setup.exe'));
  expect(installers).toHaveLength(1);
  expect(files.filter(file => file.endsWith('.bin'))).toHaveLength(0);
  const installer = installers[0]!;
  expect((await readFile(path.join(bundle, installer))).subarray(0, 2).toString()).toBe('MZ');
  const signature = (await readFile(path.join(bundle, installer + '.sig'), 'utf8')).trim();
  const minisign = Buffer.from(signature, 'base64').toString();
  expect(minisign).toContain('untrusted comment:');
  expect(minisign).toContain('trusted comment:');
});
