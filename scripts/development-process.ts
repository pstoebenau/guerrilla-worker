import { spawn } from 'node:child_process';

export async function withDevelopmentShutdown<T>(run: (signal: AbortSignal) => Promise<T>): Promise<T> {
  const shutdown = new AbortController();
  const stop = () => shutdown.abort(new Error('Worker shutdown requested.'));
  const message = (value: unknown) => {
    if (value === 'local-shutdown' || (value as { type?: string })?.type === 'local-shutdown') stop();
  };
  process.on('SIGINT', stop); process.on('SIGTERM', stop); process.on('message', message);
  try { return await run(shutdown.signal); }
  finally { process.off('SIGINT', stop); process.off('SIGTERM', stop); process.off('message', message); }
}

export async function runDevelopmentCommand(command: string, args: string[], options: {
  env: NodeJS.ProcessEnv;
  cwd: string;
  ipc?: boolean;
  captureOutput?: boolean;
  containerName?: string;
  signal?: AbortSignal;
}): Promise<{ code: number; stdout: string }> {
  options.signal?.throwIfAborted();
  const child = spawn(command, args, {
    env: options.env, cwd: options.cwd, windowsHide: true,
    stdio: ['inherit', options.captureOutput ? 'pipe' : 'inherit', 'inherit', ...(options.ipc ? ['ipc' as const] : [])],
  });
  let stdout = '';
  child.stdout?.on('data', (data: Buffer) => { stdout += data.toString(); });
  let stopping: Promise<void> | undefined;
  const stop = () => {
    stopping ??= (async () => {
      if (options.containerName) {
        const signal = spawn('docker', ['kill', '--signal', 'SIGINT', options.containerName], {
          env: options.env, stdio: 'ignore', windowsHide: true,
        });
        await new Promise<void>(resolve => {
          signal.once('error', () => resolve()); signal.once('exit', () => resolve());
        });
      } else if (child.connected) {
        try { child.send({ type: 'local-shutdown' }, () => {}); }
        catch { child.kill('SIGINT'); }
      } else child.kill('SIGINT');
    })();
  };
  const message = (value: unknown) => {
    if (value === 'local-shutdown' || (value as { type?: string })?.type === 'local-shutdown') stop();
  };
  process.on('SIGINT', stop); process.on('SIGTERM', stop); process.on('message', message);
  options.signal?.addEventListener('abort', stop, { once: true });
  if (options.signal?.aborted) stop();
  try {
    const code = await new Promise<number>((resolve, reject) => {
      child.once('error', reject); child.once('close', code => resolve(code ?? 1));
    });
    await stopping;
    return { code, stdout };
  } finally {
    process.off('SIGINT', stop); process.off('SIGTERM', stop); process.off('message', message);
    options.signal?.removeEventListener('abort', stop);
  }
}
