import path from 'node:path';
const root = path.resolve(import.meta.dir, '..');
const suffix = process.platform === 'win32' ? '.exe' : '';
const worker = path.join(root, 'dist', 'guerrilla-worker' + suffix);
if (!await Bun.file(worker).exists()) throw new Error('Run bun scripts/setup.ts to build the worker and bundle its supporting runtimes.');
const child = Bun.spawn([worker, ...process.argv.slice(2)], {
  cwd: root,
  env: { ...process.env, WORKER_RUNTIME_ROOT: process.env.WORKER_RUNTIME_ROOT ?? path.join(root, 'dist/runtime'), WORKER_PIPELINE_BIN: process.env.WORKER_PIPELINE_BIN ?? path.join(root, 'dist', 'guerrilla-pipeline' + suffix) },
  stdin: 'inherit', stdout: 'inherit', stderr: 'inherit',
});
process.exit(await child.exited);
