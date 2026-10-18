import { cp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { vendorRuntime } from './vendor-runtime';
import { linuxBundleEnvironment } from './linux-bundle';
import { stageWindowsCrtResources } from './windows-crt';

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
const buildArguments = process.argv.slice(2);
if (!buildArguments.includes('--no-bundle') && !process.env.TAURI_SIGNING_PRIVATE_KEY) {
  throw new Error('Signed desktop packages require TAURI_SIGNING_PRIVATE_KEY (and TAURI_SIGNING_PRIVATE_KEY_PASSWORD for an encrypted key). Use --no-bundle for an unsigned executable.');
}
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
const nativeLinuxPackages = process.platform === 'linux' && !buildArguments.includes('--no-bundle') && !buildArguments.some(argument => argument === '--bundles' || argument.startsWith('--bundles='));
const nativeMacDmg = process.platform === 'darwin' && !buildArguments.includes('--no-bundle') && !buildArguments.some(argument => argument === '--bundles' || argument.startsWith('--bundles='));
const bundleEnvironment: NodeJS.ProcessEnv = { ...process.env, CARGO_TARGET_DIR: path.join(tauri, 'target') };
if (process.platform === 'linux') Object.assign(bundleEnvironment, await linuxBundleEnvironment(path.join(tauri, 'resources/runtime')));
const configurations = ['--config', 'src-tauri/tauri.bundle.conf.json'];
if (process.platform === 'win32') {
  // Tauri resource mappings keep app-local CRT beside all three executables.
  const resources: Record<string, string> = {
    'resources/runtime/': 'resources/runtime/', '../../LICENSE': 'LICENSE', '../../docs/windows-worker.md': 'README.md',
    ...await stageWindowsCrtResources(tauri),
  };
  await mkdir(path.join(tauri, 'target'), { recursive: true });
  const config = path.join(tauri, 'target/windows-resources.conf.json');
  await writeFile(config, JSON.stringify({ bundle: { resources } }));
  configurations.push('--config', config);
}
// Pull-request signing keys must never replace the production trust root.
if (process.env.GUERRILLA_UPDATER_TEST_PUBKEY) {
  if (process.env.GITHUB_REF === 'refs/heads/main') throw new Error('Public releases cannot use a test updater key');
  const config = path.join(tauri, 'target/test-updater.conf.json');
  await mkdir(path.dirname(config), { recursive: true });
  await writeFile(config, JSON.stringify({ plugins: { updater: { pubkey: process.env.GUERRILLA_UPDATER_TEST_PUBKEY } } }));
  configurations.push('--config', config);
}
await run([process.execPath, 'run', 'tauri', 'build', ...configurations, ...(nativeLinuxPackages ? ['--bundles', 'deb,rpm,appimage'] : nativeMacDmg ? ['--bundles', 'app'] : []), ...buildArguments], app, bundleEnvironment);
if (process.platform === 'darwin' && !buildArguments.includes('--no-bundle')) {
  const bundles = path.join(tauri, 'target/release/bundle/macos');
  const apps = (await readdir(bundles)).filter(name => name.endsWith('.app'));
  if (apps.length !== 1) throw new Error('Expected one complete macOS app bundle');
  await run(['codesign', '--verify', '--deep', '--strict', '--verbose=2', path.join(bundles, apps[0]!)]);
}
if (nativeMacDmg) {
  const version = (JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')) as {version: string}).version;
  await run(['python3', path.join(root, 'scripts/build_macos_dmg.py'), '--bundle', path.join(tauri, 'target/release/bundle'), '--version', version]);
}
// Tauri's pinned updater supports native package-manager updates. Sign the
// DEB and RPM with the official signer after their final bytes exist.
if (nativeLinuxPackages) {
  const version = (JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')) as { version: string }).version;
  for (const [folder, suffix] of [['deb', '.deb'], ['rpm', '.rpm']] as const) {
    const directory = path.join(tauri, 'target/release/bundle', folder);
    const packages = (await readdir(directory)).filter(file => file.endsWith(suffix));
    if (packages.length !== 1) throw new Error(`Expected exactly one ${suffix} updater package`);
    await run([process.execPath, 'run', 'tauri', 'signer', 'sign', '--app-version', version, path.join(directory, packages[0]!)], app, bundleEnvironment);
  }
}
