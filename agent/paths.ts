import { isSafeRelativePath } from '@guerrilla/worker-protocol';
import path from 'node:path';
import { homedir } from 'node:os';
import { createHash } from 'node:crypto';
export function safeRelativePath(value: string) {
  if (!isSafeRelativePath(value)) throw new Error('Invalid artifact path');
  return value;
}

export function agentLockPath(gpuId: string, environment: NodeJS.ProcessEnv = process.env, platform: NodeJS.Platform = process.platform) {
  const directory = environment.WORKER_AGENT_LOCK_DIRECTORY ?? (platform === 'win32' ? path.join(environment.LOCALAPPDATA ?? homedir(), 'Guerrilla') : '/tmp');
  if (!path.isAbsolute(directory)) throw new Error('WORKER_AGENT_LOCK_DIRECTORY must be absolute');
  return path.join(directory, `guerrilla-agent-${createHash('sha256').update(gpuId).digest('hex')}.lock`);
}
