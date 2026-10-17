import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readdir, readFile, rename, rm, writeFile, access } from 'node:fs/promises';
import path from 'node:path';

export async function prepareDevelopmentRuntime(sourceDirectory: string, runtimeDirectory: string): Promise<string> {
  const result = await Bun.build({
    entrypoints: [path.join(sourceDirectory, 'agent/main.ts')], target: 'node', format: 'esm',
    plugins: [{ name: 'pinned-protocol', setup(build) {
      build.onResolve({ filter: /^@guerrilla\/worker-protocol$/ }, () => ({ path: path.join(sourceDirectory, 'packages/protocol/src/index.ts') }));
    } }],
  });
  if (!result.success) throw new Error('Worker bundle failed. Check the independent worker checkout.', { cause: result.logs });
  const bytes = new Uint8Array(await result.outputs[0].arrayBuffer());
  const hash = createHash('sha256').update('development-runtime-v1\0').update(bytes);
  const files = new Map<string, Buffer>();
  for (const name of (await readdir(path.join(sourceDirectory, 'pipeline'))).sort()) {
    if (!name.endsWith('.py') && !name.endsWith('.json') && !name.endsWith('.pyi')) continue;
    const contents = await readFile(path.join(sourceDirectory, 'pipeline', name));
    files.set(name, contents);
    hash.update(name).update(contents);
  }
  const runtime = path.join(runtimeDirectory, `hybrid-${hash.digest('hex')}`);
  try { await access(runtime); return runtime; } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
  }
  await mkdir(runtimeDirectory, { recursive: true });
  const staging = await mkdtemp(path.join(runtimeDirectory, '.preparing-'));
  try {
    await mkdir(path.join(staging, 'pipeline'));
    await writeFile(path.join(staging, 'worker.mjs'), bytes);
    for (const [name, contents] of files) await writeFile(path.join(staging, 'pipeline', name), contents);
    try { await rename(staging, runtime); } catch (error) {
      if (!['EEXIST', 'ENOTEMPTY'].includes((error as NodeJS.ErrnoException).code ?? '')) throw error;
      await access(path.join(runtime, 'worker.mjs'));
    }
  } finally { await rm(staging, { recursive: true, force: true }); }
  return runtime;
}
