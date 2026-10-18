import { expect, test } from 'bun:test';
import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';

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
