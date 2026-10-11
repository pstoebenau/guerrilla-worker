import { expect, test } from 'bun:test';
import { registrationFromReport } from '../agent/registration';

const gpus = [{ id: 'gpu-test', name: 'Test hardware', memoryBytes: 1024 }];

test('upgrading one engine preserves the other engine registration identity', () => {
  const before = registrationFromReport({ gpus, backends: {
    spirula: { available: true, runtimeVersions: { spirula: 'v1', converter: 'v1' } },
    lichtfeld: { available: true, runtimeVersions: { lichtfeld: 'v1', converter: 'v1' } },
  } });
  const after = registrationFromReport({ gpus, backends: {
    spirula: { available: true, runtimeVersions: { converter: 'v1', spirula: 'v1' } },
    lichtfeld: { available: true, runtimeVersions: { lichtfeld: 'v2', converter: 'v1' } },
  } });
  expect(before.engines.spirula).toBe(after.engines.spirula);
  expect(before.engines.lichtfeld).not.toBe(after.engines.lichtfeld);
  expect(after.healthy).toBe(true);
});

test('unavailable engines are omitted and available engines require a runtime identity', () => {
  const report = registrationFromReport({ gpus, backends: { spirula: { available: false } } });
  expect(report.engines).toEqual({});
  expect(report.healthy).toBe(false);
  expect(() => registrationFromReport({ gpus, backends: { spirula: { available: true } } })).toThrow('Missing runtime identity');
});
