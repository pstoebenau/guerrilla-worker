import { expect, test } from 'bun:test';
import { gpuInventory } from '../agent/gpu';

test('registration accepts engine-discovered GPUs without vendor or OS assumptions', () => {
  for (const name of ['Apple M3 Max', 'NVIDIA GeForce RTX 4090', 'AMD Radeon RX 7900', 'Intel Arc']) {
    const gpus = [{ id: 'gpu-1234', name, memoryBytes: 8 * 1024 ** 3 }];
    expect(gpuInventory({ gpus })).toEqual(gpus);
  }
});

test('registration rejects missing, ambiguous, and invalid hardware reports', () => {
  const gpu = { id: 'gpu-1234', name: 'Apple M3 Max', memoryBytes: 8 * 1024 ** 3 };
  for (const gpus of [undefined, [], [gpu, gpu], [{ ...gpu, id: 'bad/id' }], [{ ...gpu, memoryBytes: 0 }], [{ ...gpu, memoryBytes: NaN }]]) {
    expect(() => gpuInventory({ gpus })).toThrow(/hardware GPU/);
  }
});
