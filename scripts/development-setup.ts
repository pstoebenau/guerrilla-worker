import path from 'node:path';
import { developmentEnvironment } from './development-environment';
import { runDevelopmentCommand } from './development-process';

const root = path.resolve(import.meta.dir, '..');

export async function setupDevelopmentWorker(env = process.env): Promise<void> {
  const scoped = developmentEnvironment(env, root);
  const worker = path.join(root, 'scripts/development-worker.ts');
  const command = process.platform === 'linux' ? [worker, '--docker-build'] : [path.join(root, 'scripts/setup.ts')];
  if ((await runDevelopmentCommand(process.execPath, command, { cwd: root, env: scoped, ipc: process.platform === 'linux' })).code !== 0) {
    throw new Error('Worker setup failed. Fix the error above and rerun worker setup.');
  }
  const preflight = await runDevelopmentCommand(process.execPath, [worker, '--preflight'], { cwd: root, env: scoped, ipc: true, captureOutput: true });
  if (preflight.code !== 0) throw new Error('Worker preflight failed. Check Docker, GPU drivers and engine installations.');
  const report = JSON.parse(preflight.stdout.trim().split(/\r?\n/).at(-1)!);
  if (!report.engines?.spirula) throw new Error('Spirula preflight failed. Check GPU drivers and engine dependencies, then rerun worker setup.');
  console.log('Spirula worker setup passed. Start the development worker alongside a running server.');
}

if (import.meta.main) {
  try { await setupDevelopmentWorker(); }
  finally { if (process.connected) process.disconnect(); }
}
