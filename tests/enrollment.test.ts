import { expect, test } from 'bun:test';
import { PassThrough } from 'node:stream';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { readEnrollmentToken } from '../agent/enrollment';

test('legacy enrollment skips unavailable Python and engine checks', async () => {
  const folder = await mkdtemp(path.join(tmpdir(), 'guerrilla-enrollment-'));
  let registration: unknown;
  const server = Bun.serve({ port: 0, async fetch(request) {
    expect(new URL(request.url).pathname).toBe('/api/worker/enroll');
    const body = await request.json() as { token: string; registration: unknown };
    expect(body.token).toBe('fixture-token');
    registration = body.registration;
    return Response.json({ workerId: 'fixture-worker', credential: 'fixture-credential' });
  } });
  try {
    const worker = Bun.spawn([process.execPath, path.resolve(import.meta.dir, '../agent/main.ts'), 'enroll', '--token-stdin'], {
      env: { ...process.env, WORKER_ENROLLMENT_TOKEN: '', WORKER_CONTROL_URL: server.url.origin, WORKER_CREDENTIAL_FILE: path.join(folder, 'credentials.json'), PIPELINE_ROOT: path.join(folder, 'absent-pipeline'), PYTHON: path.join(folder, 'absent-python') },
      stdin: 'pipe', stdout: 'pipe', stderr: 'pipe',
    });
    worker.stdin.write('fixture-token\n'); worker.stdin.end();
    const [exitCode, stdout, stderr] = await Promise.all([worker.exited, new Response(worker.stdout).text(), new Response(worker.stderr).text()]);
    expect(exitCode).toBe(0);
    expect(stdout + stderr).not.toContain('fixture-token');
    expect(registration).toMatchObject({ healthy: false, engines: {}, gpus: [] });
    expect(JSON.parse(await readFile(path.join(folder, 'credentials.json'), 'utf8')).workerId).toBe('fixture-worker');
  } finally {
    server.stop(true);
    await rm(folder, { recursive: true, force: true });
  }
});

test('Enter submits a token without waiting for EOF and releases stdin', async () => {
  const input = new PassThrough();
  const token = readEnrollmentToken(input);
  input.write('  example-token');
  input.write('\r\n');
  expect(await token).toBe('example-token');
  expect(input.readableEnded).toBe(false);
  expect(input.isPaused()).toBe(true);
  expect(input.listenerCount('data')).toBe(0);
});

test('piped token without newline submits at EOF', async () => {
  const input = new PassThrough();
  const token = readEnrollmentToken(input);
  input.end('example-token');
  expect(await token).toBe('example-token');
});

test('empty and oversized tokens fail without echoing the input', async () => {
  for (const value of ['', '  \n', 'x'.repeat(4097) + '\n']) {
    const input = new PassThrough();
    const token = readEnrollmentToken(input);
    input.end(value);
    await expect(token).rejects.toThrow(/token/i);
    expect(input.isPaused()).toBe(true);
  }
});
