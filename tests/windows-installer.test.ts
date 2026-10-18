import { expect, test } from 'bun:test';
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { sha256 } from '../scripts/vendor-runtime';
import { windowsPowerShellEnvironment } from '../scripts/windows-powershell';

test.skipIf(process.platform !== 'win32')('pinned Inno compiler builds a single installer and verifies its checksum', async () => {
  const temporary = await mkdtemp(path.join(tmpdir(), 'guerrilla-installer-test-'));
  const source = path.join(temporary, 'app');
  const output = path.join(temporary, 'installer');
  try {
    // Compile the production script with tiny payloads; installing/executing the
    // real app belongs to the package job's installed-worker smoke test.
    for (const relative of [
      'guerrilla-worker-desktop.exe', 'guerrilla-worker.exe', 'guerrilla-pipeline.exe',
      'msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'LICENSE', 'README.md',
      'resources/runtime/manifest.json',
      'resources/runtime/node/node.exe', 'resources/runtime/ffmpeg/ffmpeg.exe',
      'resources/runtime/ffmpeg/ffprobe.exe',
      'resources/runtime/converter/node_modules/@playcanvas/splat-transform/bin/cli.mjs',
    ]) {
      const file = path.join(source, relative);
      await mkdir(path.dirname(file), { recursive: true });
      await writeFile(file, 'installer compilation fixture');
    }
    const child = Bun.spawn(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
      '-File', path.resolve(import.meta.dir, '../scripts/build-windows.ps1'),
      '-SourceDirectory', source, '-Version', '0.3.0', '-Destination', output], {
      env: windowsPowerShellEnvironment(), stdin: 'ignore', stdout: 'pipe', stderr: 'pipe', windowsHide: true,
    });
    const [stdout, stderr, code] = await Promise.all([
      new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited,
    ]);
    expect(code, `${stdout}\n${stderr}`).toBe(0);
    const base = 'Guerrilla-Worker-0.3.0-windows-x64-setup';
    const files = await readdir(output);
    expect(files).toContain(`${base}.exe`);
    expect(files.some(file => file.endsWith('.bin'))).toBe(false);
    const checksums = await readFile(path.join(output, `${base}-SHA256SUMS.txt`), 'utf8');
    for (const line of checksums.trim().split(/\r?\n/)) {
      const [digest, file] = line.split('  ');
      expect(await sha256(path.join(output, file!))).toBe(digest!);
    }
  } finally { await rm(temporary, { recursive: true, force: true }); }
}, 300_000);
