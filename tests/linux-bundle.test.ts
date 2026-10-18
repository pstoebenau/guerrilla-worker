import { expect, test } from 'bun:test';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { linuxRuntimeLibraryPath } from '../scripts/linux-bundle';

test.skipIf(process.platform !== 'linux')('the AppImage search path resolves a bundled native library dependency', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'guerrilla-private-library-'));
  try {
    const libraries = path.join(root, 'engine/lib');
    await mkdir(libraries, { recursive: true });
    const dependency = path.join(root, 'dependency.c');
    const consumer = path.join(root, 'consumer.c');
    await writeFile(dependency, 'int fixture_dependency(void) { return 42; }');
    await writeFile(consumer, 'extern int fixture_dependency(void); int fixture_consumer(void) { return fixture_dependency(); }');
    for (const args of [
      ['cc', '-shared', '-fPIC', dependency, '-Wl,-soname,libguerrilla-fixture.so.1', '-o', path.join(libraries, 'libguerrilla-fixture.so.1')],
      ['cc', '-shared', '-fPIC', consumer, '-L', libraries, '-l:libguerrilla-fixture.so.1', '-o', path.join(libraries, 'consumer.so')],
    ]) {
      const compiled = Bun.spawnSync(args, { stdout: 'pipe', stderr: 'pipe' });
      expect(compiled.exitCode, compiled.stderr.toString()).toBe(0);
    }
    const without = Bun.spawnSync(['ldd', path.join(libraries, 'consumer.so')], { env: { ...process.env, LD_LIBRARY_PATH: '' }, stdout: 'pipe', stderr: 'pipe' });
    expect(without.stdout.toString()).toContain('libguerrilla-fixture.so.1 => not found');
    const resolved = Bun.spawnSync(['ldd', path.join(libraries, 'consumer.so')], { env: { ...process.env, LD_LIBRARY_PATH: await linuxRuntimeLibraryPath(root) }, stdout: 'pipe', stderr: 'pipe' });
    expect(resolved.exitCode).toBe(0);
    expect(resolved.stdout.toString()).toContain(path.join(libraries, 'libguerrilla-fixture.so.1'));
    expect(resolved.stdout.toString()).not.toContain('not found');
  } finally { await rm(root, { recursive: true, force: true }); }
});
