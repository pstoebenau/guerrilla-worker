import { expect, test } from 'bun:test';
import { parsePreflight, parseStatus } from '../src/lib/parse';

test('worker progress uses actual counters and maps server scan identity', () => {
  const status = parseStatus({ state: 'busy', running: true, connected: true, scanId: 'scan-1', progress: { current: 123, total: 500 }, gpus: [{ id: 'gpu-123', name: 'Hardware', memoryBytes: 8 * 1024 ** 3 }], engines: { spirula: { available: true } } });
  expect(status.jobId).toBe('scan-1');
  expect(status.progress).toBe(0.246);
  expect(status.gpus?.[0]?.memoryMb).toBe(8192);
  expect(status.engines?.[0]?.available).toBe(true);
  expect(parseStatus({ state: 'busy', progress: { current: 123 }, connected: true }).progress).toBeUndefined();
});
test('malformed status cannot silently show idle', () => {
  expect(() => parseStatus({ state: 'anything', connected: true })).toThrow('unknown state');
});
test('preflight reports missing engine reasons and requires one GPU', () => {
  const raw = { backends: { lichtfeld: { available: false, error: 'Engine not installed' }, spirula: { available: true } }, gpus: [{ name: 'GPU', memoryBytes: 8 * 1024 ** 3 }] };
  const report = parsePreflight(raw);
  expect(report.ok).toBe(true);
  expect(report.engines?.[0]?.message).toBe('Engine not installed');
  expect(parsePreflight({ ...raw, gpus: [] }).ok).toBe(false);
  expect(parsePreflight({ ...raw, gpus: [...raw.gpus, ...raw.gpus] }).ok).toBe(false);
});
