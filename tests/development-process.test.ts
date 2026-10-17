import { expect, spyOn, test } from 'bun:test';
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { runDevelopmentCommand, withDevelopmentShutdown } from '../scripts/development-process';

import { verifiedDownload } from '../scripts/vendor-runtime';

const processModule = path.resolve(import.meta.dir, '../scripts/development-process.ts');

for (const mode of ['signal', 'ipc']) test.skipIf(process.platform === 'win32' && mode === 'signal')(`native ${mode} shutdown waits for the enrolled agent to flush its checkpoint before exit`, async () => {
  const temporary = await mkdtemp(path.join(tmpdir(), 'guerrilla-shutdown-'));
  let child: ReturnType<typeof Bun.spawn> | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const agent = path.join(temporary, 'agent.mjs');
    const checkpoint = path.join(temporary, 'checkpoint');
    const ready = path.join(temporary, 'ready');
    await writeFile(agent, `import { writeFile } from 'node:fs/promises';
      process.on('message', async message => {
        if (message?.type === 'local-shutdown') {
          await writeFile(${JSON.stringify(checkpoint)}, 'durable checkpoint');
          process.disconnect(); process.exit(0);
        }
      });
      process.on('disconnect', () => process.exit(0));
      await writeFile(${JSON.stringify(ready)}, 'ready'); console.log('agent-ready');`);
    const launcher = path.join(temporary, 'launcher.ts');
    await writeFile(launcher, `import { runDevelopmentCommand, withDevelopmentShutdown } from ${JSON.stringify(processModule)};
      process.exitCode = (await runDevelopmentCommand(${JSON.stringify(Bun.which('node'))}, [${JSON.stringify(agent)}], { env: process.env, cwd: ${JSON.stringify(temporary)}, ipc: true })).code;
      if (process.connected) process.disconnect();`);
    child = Bun.spawn([process.execPath, launcher], { stdout: 'pipe', stderr: 'pipe', ...(mode === 'ipc' ? { ipc() {} } : {}) });
    const output = new Response(child.stdout as ReadableStream).text();
    // Wait for the actual agent before interrupting its launcher.
    const deadline = Date.now() + 5_000;
    while (!await Bun.file(ready).exists()) {
      if (Date.now() > deadline) throw new Error('Development agent did not start');
      await Bun.sleep(10);
    }
    if (mode === 'ipc') child.send({ type: 'local-shutdown' });
    else child.kill('SIGTERM');
    timer = setTimeout(() => child?.kill('SIGKILL'), 5_000);
    expect(await child.exited).toBe(0);
    expect(await output).toContain('agent-ready');
    expect(await readFile(checkpoint, 'utf8')).toBe('durable checkpoint');
  } finally {
    clearTimeout(timer);
    child?.kill('SIGKILL');
    await rm(temporary, { recursive: true, force: true });
  }
}, 15_000);

test('development process returns child failures without changing their exit code', async () => {
  const result = await runDevelopmentCommand(process.execPath, ['-e', 'console.log("report"); process.exit(7)'], { cwd: tmpdir(), env: {}, captureOutput: true });
  expect(result).toEqual({ code: 7, stdout: 'report\n' });
});


test('shutdown during an engine download cancels it without retrying or leaving partial bytes', async () => {
  const cache = await mkdtemp(path.join(tmpdir(), 'guerrilla-build-abort-'));
  const shutdown = new AbortController();
  let started!: () => void;
  const downloading = new Promise<void>(resolve => { started = resolve; });
  const request = spyOn(globalThis, 'fetch').mockImplementation(Object.assign(async (_url: Parameters<typeof fetch>[0], options?: Parameters<typeof fetch>[1]) => {
    started();
    return await new Promise<Response>((_resolve, reject) => {
      const signal = options!.signal!;
      signal.addEventListener('abort', () => reject(signal.reason), { once: true });
    });
  }, { preconnect: fetch.preconnect }));
  try {
    const pending = verifiedDownload({ url: 'https://example.invalid/engine.tar.gz', sha256: 'a'.repeat(64) }, cache, shutdown.signal);
    await downloading;
    shutdown.abort(new Error('requested shutdown'));
    await expect(pending).rejects.toThrow('requested shutdown');
    expect(request).toHaveBeenCalledTimes(1);
    expect(await readdir(cache)).toEqual([]);
  } finally { request.mockRestore(); await rm(cache, { recursive: true, force: true }); }
});

test('a shutdown request prevents subsequent build phases from spawning', async () => {
  await withDevelopmentShutdown(async signal => {
    process.emit('message', { type: 'local-shutdown' }, undefined);
    await expect(runDevelopmentCommand(process.execPath, ['-e', 'process.exit(0)'], { cwd: tmpdir(), env: {}, signal })).rejects.toThrow('Worker shutdown requested');
  });
});
