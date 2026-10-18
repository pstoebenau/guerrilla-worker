import { createHash } from 'node:crypto';
import { access, chmod, mkdir, mkdtemp, readFile, rename, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';

/** Freeze both native executables so rebuilding another checkout cannot change a running scan. */
export async function snapshotDevelopmentBinaries(binaryDirectory: string, runtimeDirectory: string, suffix = process.platform === 'win32' ? '.exe' : ''): Promise<string> {
  const names = ['guerrilla-worker', 'guerrilla-pipeline'].map(name => name + suffix);
  const bytes = await Promise.all(names.map(name => readFile(path.join(binaryDirectory, name))));
  const hash = createHash('sha256').update('native-development-runtime-v1\0');
  names.forEach((name, index) => hash.update(name).update(bytes[index]!));
  const runtime = path.join(runtimeDirectory, `native-${hash.digest('hex')}`);
  try { await access(path.join(runtime, names[1]!)); return runtime; }
  catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
  await mkdir(runtimeDirectory, { recursive: true });
  const staging = await mkdtemp(path.join(runtimeDirectory, '.preparing-'));
  try {
    for (const [index, name] of names.entries()) {
      const executable = path.join(staging, name);
      await writeFile(executable, bytes[index]!);
      await chmod(executable, 0o755);
    }
    try { await rename(staging, runtime); }
    catch (error) {
      if (!['EEXIST', 'ENOTEMPTY'].includes((error as NodeJS.ErrnoException).code ?? '')) throw error;
      await access(path.join(runtime, names[1]!));
    }
  } finally { await rm(staging, { recursive: true, force: true }); }
  return runtime;
}

export async function prepareDevelopmentRuntime(sourceDirectory: string, runtimeDirectory: string, env: NodeJS.ProcessEnv = process.env): Promise<string> {
  const target = path.join(sourceDirectory, 'target');
  const build = Bun.spawn(['cargo', 'build', '--locked', '--release', '--workspace', '--target-dir', target], {
    cwd: sourceDirectory, env, stdin: 'ignore', stdout: 'inherit', stderr: 'inherit', windowsHide: true,
  });
  if (await build.exited !== 0) throw new Error('Native worker build failed. Check the independent worker checkout.');
  return snapshotDevelopmentBinaries(path.join(target, 'release'), runtimeDirectory);
}
