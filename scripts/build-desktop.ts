import { cp, mkdir, readFile, readdir, rm } from 'node:fs/promises';
import path from 'node:path';
import { vendorRuntime } from './vendor-runtime';
import { windowsPowerShellEnvironment } from './windows-powershell';
import { linuxBundleEnvironment } from './linux-bundle';

const root = path.resolve(import.meta.dir, '..');
const app = path.join(root, 'desktop');
const tauri = path.join(app, 'src-tauri');
if (process.argv.slice(2).some(argument => argument === '--target' || argument.startsWith('--target='))) {
  throw new Error('Desktop packages must be built natively on their target OS and architecture; the supporting runtimes are host-specific.');
}
async function run(args: string[], cwd = root, env: NodeJS.ProcessEnv = process.env): Promise<void> {
  const child = Bun.spawn(args, { cwd, env, stdin: 'inherit', stdout: 'inherit', stderr: 'inherit' });
  if (await child.exited !== 0) throw new Error(`Desktop build failed: ${args.join(' ')}`);
}
const version = Bun.spawnSync(['rustc', '-vV'], { stdout: 'pipe' });
const target = version.stdout.toString().match(/^host: (.+)$/m)?.[1];
if (!target) throw new Error('Install the Rust toolchain before building the desktop app');
const suffix = process.platform === 'win32' ? '.exe' : '';
await run([process.platform === 'win32' ? 'python' : 'python3', path.join(root, 'scripts/release_version.py'), 'check']);
await run(['cargo', 'build', '--release', '--locked', '--workspace', '--target-dir', path.join(root, 'target')]);
await mkdir(path.join(tauri, 'binaries'), { recursive: true });
for (const binary of ['guerrilla-worker', 'guerrilla-pipeline']) {
  await cp(path.join(root, 'target/release', binary + suffix), path.join(tauri, 'binaries', `${binary}-${target}${suffix}`));
}
await vendorRuntime(path.join(tauri, 'resources/runtime'));
await rm(path.join(tauri, 'resources/pipeline'), { recursive: true, force: true });
await run([process.execPath, 'install', '--frozen-lockfile'], app);
await run([process.execPath, 'run', 'check'], app);
const buildArguments = process.argv.slice(2);
const nativeLinuxRpm = process.platform === 'linux' && !buildArguments.includes('--no-bundle') && !buildArguments.some(argument => argument === '--bundles' || argument.startsWith('--bundles='));
const nativeMacDmg = process.platform === 'darwin' && !buildArguments.includes('--no-bundle') && !buildArguments.some(argument => argument === '--bundles' || argument.startsWith('--bundles='));
const bundleEnvironment: NodeJS.ProcessEnv = { ...process.env, CARGO_TARGET_DIR: path.join(tauri, 'target') };
if (process.platform === 'linux') Object.assign(bundleEnvironment, await linuxBundleEnvironment(path.join(tauri, 'resources/runtime')));
await run([process.execPath, 'run', 'tauri', 'build', '--config', 'src-tauri/tauri.bundle.conf.json', ...(process.platform === 'win32' && !buildArguments.includes('--no-bundle') ? ['--no-bundle'] : []), ...(nativeLinuxRpm ? ['--bundles', 'deb,appimage'] : nativeMacDmg ? ['--bundles', 'app'] : []), ...buildArguments], app, bundleEnvironment);
if (process.platform === 'darwin' && !buildArguments.includes('--no-bundle')) {
  const bundles = path.join(tauri, 'target/release/bundle/macos');
  const apps = (await readdir(bundles)).filter(name => name.endsWith('.app'));
  if (apps.length !== 1) throw new Error('Expected one complete macOS app bundle');
  await run(['codesign', '--verify', '--deep', '--strict', '--verbose=2', path.join(bundles, apps[0]!)]);
}
if (nativeLinuxRpm) {
  const version = (JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')) as {version: string}).version;
  await run(['python3', path.join(root, 'scripts/build_linux_rpm.py'), '--bundle', path.join(tauri, 'target/release/bundle'), '--version', version]);
}
if (nativeMacDmg) {
  const version = (JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')) as {version: string}).version;
  await run(['python3', path.join(root, 'scripts/build_macos_dmg.py'), '--bundle', path.join(tauri, 'target/release/bundle'), '--version', version]);
}
if (process.platform === 'win32') {
  const portable = path.join(root, 'dist/windows-app');
  await rm(portable, { recursive: true, force: true });
  await mkdir(portable, { recursive: true });
  await cp(path.join(tauri, 'target/release/guerrilla-worker-desktop.exe'), path.join(portable, 'guerrilla-worker-desktop.exe'));
  for (const binary of ['guerrilla-worker', 'guerrilla-pipeline']) await cp(path.join(root, 'target/release', binary + suffix), path.join(portable, binary + suffix));
  await mkdir(path.join(portable, 'resources'));
  await cp(path.join(tauri, 'resources/runtime'), path.join(portable, 'resources/runtime'), { recursive: true });
  await cp(path.join(root, 'LICENSE'), path.join(portable, 'LICENSE'));
  await cp(path.join(root, 'docs/windows-worker.md'), path.join(portable, 'README.md'));
  for (const file of await readdir(path.join(tauri, 'resources/runtime/windows-crt'))) {
    if (file.toLowerCase().endsWith('.dll')) await cp(path.join(tauri, 'resources/runtime/windows-crt', file), path.join(portable, file));
  }
  if (!buildArguments.includes('--no-bundle')) {
    const version = (JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')) as {version: string}).version;
    await run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', path.join(root, 'scripts/build-windows.ps1'), '-SourceDirectory', portable, '-Version', version, '-Destination', path.join(tauri, 'target/release/bundle/installer')], root, windowsPowerShellEnvironment());
  }
}
