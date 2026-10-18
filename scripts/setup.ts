import { cp, mkdir, rm } from 'node:fs/promises';
import path from 'node:path';
import { vendorRuntime } from './vendor-runtime';

const root = path.resolve(import.meta.dir, '..');
const destination = path.join(root, 'dist');
const build = Bun.spawn(['cargo', 'build', '--release', '--locked', '--workspace', '--target-dir', path.join(root, 'target')], { cwd: root, stdout: 'inherit', stderr: 'inherit' });
if (await build.exited !== 0) throw new Error('Rust worker build failed');
await mkdir(destination, { recursive: true });
await rm(path.join(destination, 'pipeline'), { recursive: true, force: true });
const suffix = process.platform === 'win32' ? '.exe' : '';
for (const binary of ['guerrilla-worker', 'guerrilla-pipeline']) await cp(path.join(root, 'target/release', binary + suffix), path.join(destination, binary + suffix));
await vendorRuntime(path.join(destination, 'runtime'));
console.log('Native worker and converter runtimes ready. Spirula and its reconstruction model install automatically on first use.');
