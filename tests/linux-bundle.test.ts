import { expect, test } from 'bun:test';
import { existsSync } from 'node:fs';
import { cp, mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { homedir, tmpdir } from 'node:os';
import path from 'node:path';
import { linuxBundleEnvironment, linuxRuntimeLibraryPath } from '../scripts/linux-bundle';

test.skipIf(process.platform !== 'linux')('the AppImage search path resolves a wheel-private dependency without its parent extension', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'guerrilla-private-library-'));
  try {
    const libraries = path.join(root, 'python/lib/python3.12/site-packages/example.libs');
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

// Tauri installs this pinned tool during the Linux app build, before CI tests.
const linuxdeploy = process.env.LINUXDEPLOY_BIN ?? path.join(process.env.XDG_CACHE_HOME ?? path.join(homedir(), '.cache'), 'tauri/linuxdeploy-07333c6-x86_64.AppImage');
test.skipIf(process.platform !== 'linux' || !existsSync(linuxdeploy))('AppImage leaves the NVIDIA driver external while requiring bundled runtime dependencies', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'guerrilla-driver-exclusion-'));
  try {
    const runtime = path.join(root, 'runtime');
    const libraries = path.join(runtime, 'example.libs');
    const stubs = path.join(runtime, 'stubs');
    await mkdir(libraries, { recursive: true });
    await mkdir(stubs);
    await writeFile(path.join(root, 'driver.c'), 'int fixture_driver(void) { return 1; }');
    await writeFile(path.join(root, 'runtime.c'), 'int fixture_runtime(void) { return 42; }');
    await writeFile(path.join(root, 'consumer.c'), 'extern int fixture_driver(void); extern int fixture_runtime(void); int fixture_consumer(void) { return fixture_driver() + fixture_runtime(); }');
    const dependency = path.join(libraries, 'libguerrilla-runtime-fixture.so.1');
    const consumer = path.join(root, 'consumer.so');
    for (const args of [
      ['cc', '-shared', '-fPIC', path.join(root, 'driver.c'), '-Wl,-soname,libcuda.so.1', '-o', path.join(stubs, 'libcuda.so.1')],
      ['cc', '-shared', '-fPIC', path.join(root, 'runtime.c'), '-Wl,-soname,libguerrilla-runtime-fixture.so.1', '-o', dependency],
      ['cc', '-shared', '-fPIC', path.join(root, 'consumer.c'), '-L', stubs, '-l:libcuda.so.1', '-L', libraries, '-l:libguerrilla-runtime-fixture.so.1', '-o', consumer],
    ]) {
      const compiled = Bun.spawnSync(args, { stdout: 'pipe', stderr: 'pipe' });
      expect(compiled.exitCode, compiled.stderr.toString()).toBe(0);
    }
    const environment = await linuxBundleEnvironment(runtime, { ...process.env, LD_LIBRARY_PATH: '', LINUXDEPLOY_EXCLUDED_LIBRARIES: undefined });
    async function deploy(name: string, env: NodeJS.ProcessEnv) {
      const appdir = path.join(root, name);
      const library = path.join(appdir, 'usr/lib/consumer.so');
      await mkdir(path.dirname(library), { recursive: true });
      await cp(consumer, library);
      const result = Bun.spawnSync([linuxdeploy, '--appimage-extract-and-run', '--appdir', appdir, '--deploy-deps-only', library], { env, stdout: 'pipe', stderr: 'pipe' });
      return { result, appdir, log: result.stdout.toString() + result.stderr.toString() };
    }
    const without = await deploy('without-exclusion', { ...environment, LINUXDEPLOY_EXCLUDED_LIBRARIES: '' });
    expect(without.result.exitCode).not.toBe(0);
    expect(without.log).toContain('Could not find dependency: libcuda.so.1');
    const bundled = await deploy('with-exclusion', environment);
    expect(bundled.result.exitCode, bundled.log).toBe(0);
    expect(existsSync(path.join(bundled.appdir, 'usr/lib/libcuda.so.1'))).toBe(false);
    expect(existsSync(path.join(bundled.appdir, 'usr/lib/libguerrilla-runtime-fixture.so.1'))).toBe(true);
    await rm(dependency);
    const missingRuntime = await deploy('missing-runtime', environment);
    expect(missingRuntime.result.exitCode).not.toBe(0);
    expect(missingRuntime.log).toContain('Could not find dependency: libguerrilla-runtime-fixture.so.1');
  } finally { await rm(root, { recursive: true, force: true }); }
}, 30_000);
