import { expect, test } from 'bun:test';
import { PassThrough } from 'node:stream';
import { readEnrollmentToken } from '../agent/enrollment';

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
