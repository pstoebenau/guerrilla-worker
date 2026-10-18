import path from 'node:path';
import { developmentEnvironment } from './development-environment';
import { runDevelopmentCommand } from './development-process';

const root = path.resolve(import.meta.dir, '..');
export async function setupDevelopmentWorker(env = process.env): Promise<void> {
  const scoped = developmentEnvironment(env, root);
  const worker = path.join(root, 'scripts/development-worker.ts');
  const command = process.platform === 'linux' ? [worker, '--docker-build'] : [path.join(root, 'scripts/setup.ts')];
  if ((await runDevelopmentCommand(process.execPath, command, { cwd: root, env: scoped })).code !== 0) throw new Error('Worker setup failed; inspect the error above.');
  if (process.platform !== 'linux' && (await runDevelopmentCommand(process.execPath, [worker, 'setup-runtime'], { cwd: root, env: scoped })).code !== 0) throw new Error('Spirula installation failed; inspect the runtime setup error above.');
  if ((await runDevelopmentCommand(process.execPath, [worker, '--preflight'], { cwd: root, env: scoped, captureOutput: true })).code !== 0) throw new Error('Spirula preflight failed; check GPU drivers and runtime installation.');
  console.log('Spirula worker setup passed. Start the development worker alongside a running server.');
}
if (import.meta.main) { try { await setupDevelopmentWorker(); } finally { if (process.connected) process.disconnect(); } }
