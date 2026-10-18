import { expect, test } from 'bun:test';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { snapshotDevelopmentBinaries } from '../scripts/development-runtime';

test('native development snapshots preserve running executable bytes across rebuilds and concurrent preparation', async () => {
  const temporary = await mkdtemp(path.join(tmpdir(), 'guerrilla-development-'));
  try {
    const source = path.join(temporary, 'bin');
    const runtimes = path.join(temporary, 'runtimes');
    await mkdir(source);
    await writeFile(path.join(source, 'guerrilla-worker'), 'first worker');
    await writeFile(path.join(source, 'guerrilla-pipeline'), 'first pipeline');
    const [first, same] = await Promise.all([snapshotDevelopmentBinaries(source, runtimes, ''), snapshotDevelopmentBinaries(source, runtimes, '')]);
    expect(same).toBe(first);
    await writeFile(path.join(source, 'guerrilla-pipeline'), 'second pipeline');
    const second = await snapshotDevelopmentBinaries(source, runtimes, '');
    expect(second).not.toBe(first);
    expect(await readFile(path.join(first, 'guerrilla-pipeline'), 'utf8')).toBe('first pipeline');
    expect(await readFile(path.join(second, 'guerrilla-pipeline'), 'utf8')).toBe('second pipeline');
    await writeFile(path.join(source, 'guerrilla-worker'), 'second worker');
    const third = await snapshotDevelopmentBinaries(source, runtimes, '');
    expect(third).not.toBe(second);
    expect(await readFile(path.join(third, 'guerrilla-worker'), 'utf8')).toBe('second worker');
    expect(await readFile(path.join(first, 'guerrilla-worker'), 'utf8')).toBe('first worker');
  } finally { await rm(temporary, { recursive: true, force: true }); }
});
