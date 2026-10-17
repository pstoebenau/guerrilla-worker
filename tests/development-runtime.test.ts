import { expect, test } from 'bun:test';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { prepareDevelopmentRuntime } from '../scripts/development-runtime';

test('development snapshots pin agent, protocol and pipeline bytes across source changes and concurrent preparation', async () => {
  const temporary = await mkdtemp(path.join(tmpdir(), 'guerrilla-development-'));
  try {
    const source = path.join(temporary, 'source');
    const runtimes = path.join(temporary, 'runtimes');
    await mkdir(path.join(source, 'agent'), { recursive: true });
    await mkdir(path.join(source, 'packages/protocol/src'), { recursive: true });
    await mkdir(path.join(source, 'pipeline'));
    await writeFile(path.join(source, 'agent/main.ts'), "import { version } from '@guerrilla/worker-protocol'; console.log(version);");
    await writeFile(path.join(source, 'packages/protocol/src/index.ts'), "export const version = 'protocol-one';");
    await writeFile(path.join(source, 'pipeline/runner.py'), 'first pipeline');
    await writeFile(path.join(source, 'pipeline/settings.schema.json'), '{}');
    const [first, same] = await Promise.all([prepareDevelopmentRuntime(source, runtimes), prepareDevelopmentRuntime(source, runtimes)]);
    expect(same).toBe(first);
    expect(await readFile(path.join(first, 'worker.mjs'), 'utf8')).toContain('protocol-one');
    await writeFile(path.join(source, 'pipeline/runner.py'), 'second pipeline');
    const second = await prepareDevelopmentRuntime(source, runtimes);
    expect(second).not.toBe(first);
    expect(await readFile(path.join(first, 'pipeline/runner.py'), 'utf8')).toBe('first pipeline');
    expect(await readFile(path.join(second, 'pipeline/runner.py'), 'utf8')).toBe('second pipeline');
    await writeFile(path.join(source, 'packages/protocol/src/index.ts'), "export const version = 'protocol-two';");
    const third = await prepareDevelopmentRuntime(source, runtimes);
    expect(third).not.toBe(second);
    expect(await readFile(path.join(third, 'worker.mjs'), 'utf8')).toContain('protocol-two');
    expect(await readFile(path.join(first, 'worker.mjs'), 'utf8')).toContain('protocol-one');
  } finally { await rm(temporary, { recursive: true, force: true }); }
});
