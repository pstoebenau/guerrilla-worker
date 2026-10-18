import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { chmod, cp, mkdir, mkdtemp, readFile, readdir, rename, rm, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import path from 'node:path';
import { vendorWindowsCrt } from './windows-crt';

type Asset = { url: string; sha256: string };
type RuntimeLock = {
  version: number;
  nodeVersion: string;
  ffmpegRelease: string;
  targets: Record<string, Record<'node' | 'ffmpegArchive' | 'ffmpegConfig' | 'ffmpegLicense' | 'ffmpegReadme', Asset>>;
};
const root = path.resolve(import.meta.dir, '..');

export async function sha256(file: string): Promise<string> {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(file)) hash.update(chunk);
  return hash.digest('hex');
}

export async function verifiedDownload(asset: Asset, cache: string, signal?: AbortSignal): Promise<string> {
  signal?.throwIfAborted();
  if (!/^[a-f0-9]{64}$/.test(asset.sha256) || new URL(asset.url).protocol !== 'https:') throw new Error('Invalid runtime asset identity');
  await mkdir(cache, { recursive: true });
  const file = path.join(cache, `${asset.sha256}-${decodeURIComponent(path.basename(new URL(asset.url).pathname))}`);
  if (await Bun.file(file).exists()) {
    if (await sha256(file) === asset.sha256) return file;
    throw new Error(`Cached runtime checksum mismatch: ${path.basename(file)}`);
  }
  const temporary = `${file}.${crypto.randomUUID()}.download`;
  try {
    console.log(`Downloading ${path.basename(new URL(asset.url).pathname)}…`);
    for (let attempt = 0; ; attempt++) {
      signal?.throwIfAborted();
      let response: Response;
      try { response = await fetch(asset.url, { signal: AbortSignal.any([AbortSignal.timeout(600_000), ...(signal ? [signal] : [])]) }); }
      catch (error) {
        signal?.throwIfAborted();
        if (attempt === 3) throw error;
        console.log(`Runtime download interrupted; retrying (${attempt + 1}/3)`);
        await Bun.sleep(1000 * 2 ** attempt);
        continue;
      }
      if (!response.ok) {
        await response.body?.cancel();
        if ((response.status === 429 || response.status >= 500) && attempt < 3) {
          console.log(`Runtime download returned HTTP ${response.status}; retrying (${attempt + 1}/3)`);
          await Bun.sleep(1000 * 2 ** attempt);
          continue;
        }
        throw new Error(`Runtime download failed: HTTP ${response.status}`);
      }
      await Bun.write(temporary, response);
      break;
    }
    if (await sha256(temporary) !== asset.sha256) throw new Error('Runtime checksum mismatch; download refused');
    await rename(temporary, file);
    return file;
  } finally { await rm(temporary, { force: true }); }
}

async function run(args: string[], cwd: string, env: NodeJS.ProcessEnv = process.env): Promise<void> {
  const child = Bun.spawn(args, { cwd, env, stdin: 'ignore', stdout: 'inherit', stderr: 'inherit', windowsHide: true });
  if (await child.exited !== 0) throw new Error(`Runtime preparation failed: ${path.basename(args[0]!)}`);
}

export async function extractRuntimeArchive(archive: string, destination: string, stripComponents = 0): Promise<void> {
  let tar = 'tar';
  if (process.platform === 'win32') {
    const systemRoot = process.env.SystemRoot ?? process.env.SYSTEMROOT;
    if (!systemRoot || !path.isAbsolute(systemRoot)) throw new Error('Windows SystemRoot is required for runtime archive extraction');
    // Git Bash's GNU tar interprets a drive-letter archive path as a remote host.
    // Windows' native bsdtar accepts those paths and handles both tarballs and ZIPs.
    tar = path.join(systemRoot, 'System32', 'tar.exe');
  }
  await run([tar, '-xf', archive, '-C', destination, ...(stripComponents ? [`--strip-components=${stripComponents}`] : [])], root);
}

export async function vendorRuntime(destination: string): Promise<void> {
  const target = `${process.platform}-${process.arch}`;
  const lock = JSON.parse(await readFile(path.join(root, 'packaging/runtime-lock.json'), 'utf8')) as RuntimeLock;
  const assets = lock.targets[target];
  if (!assets) throw new Error(`No vendored runtime for ${target}; supported targets: ${Object.keys(lock.targets).join(', ')}`);
  const cache = process.env.WORKER_BUILD_CACHE ?? path.join(homedir(), '.cache', 'guerrilla-worker', 'build');
  await mkdir(path.dirname(destination), { recursive: true });
  const staging = await mkdtemp(path.join(path.dirname(destination), '.runtime-'));
  const windows = process.platform === 'win32';
  const suffix = windows ? '.exe' : '';
  try {
    for (const name of ['node'] as const) {
      const archive = await verifiedDownload(assets[name], cache);
      const install = path.join(staging, name);
      await mkdir(install);
      await extractRuntimeArchive(archive, install, 1);
    }
    await vendorWindowsCrt(staging);
    await mkdir(path.join(staging, 'ffmpeg'));
    await extractRuntimeArchive(await verifiedDownload(assets.ffmpegArchive, cache), path.join(staging, 'ffmpeg'));
    for (const name of ['ffmpeg', 'ffprobe'] as const) {
      const installed = path.join(staging, 'ffmpeg', name + suffix);
      await chmod(installed, 0o755);
    }
    await cp(await verifiedDownload(assets.ffmpegConfig, cache), path.join(staging, 'ffmpeg', 'configure.txt'));
    const ffmpegCheck = Bun.spawnSync([path.join(staging, 'ffmpeg', 'ffmpeg' + suffix), '-version'], { stdout: 'pipe', stderr: 'pipe' });
    const ffmpegConfiguration = ffmpegCheck.stdout.toString() + ffmpegCheck.stderr.toString();
    if (ffmpegCheck.exitCode !== 0 || !ffmpegConfiguration.includes('--disable-nonfree') || ffmpegConfiguration.includes('--enable-nonfree')) throw new Error('FFmpeg runtime failed the redistribution configuration check');
    for (const [name, installed] of [['ffmpegLicense', 'LICENSE'], ['ffmpegReadme', 'README']] as const) {
      await cp(await verifiedDownload(assets[name], cache), path.join(staging, 'ffmpeg', installed));
    }
    const node = path.join(staging, 'node', windows ? 'node.exe' : 'bin/node');
    const npm = path.join(staging, 'node', windows ? 'node_modules/npm/bin/npm-cli.js' : 'lib/node_modules/npm/bin/npm-cli.js');
    const converter = path.join(staging, 'converter');
    await mkdir(converter);
    for (const file of ['package.json', 'package-lock.json']) await cp(path.join(root, 'pipeline/docker/converter', file), path.join(converter, file));
    const runtimeEnv = { ...process.env, PATH: [path.dirname(node), path.join(staging, 'ffmpeg'), process.env.PATH ?? ''].join(path.delimiter) };
    const npmConfig = path.join(staging, '.npmrc');
    await writeFile(npmConfig, '');
    await run([node, npm, 'ci', '--prefix', converter, '--global=false', '--userconfig', npmConfig, '--omit=dev', '--ignore-scripts', '--no-audit', '--no-fund'], converter, runtimeEnv);
    await rm(npmConfig);
    await run([node, path.join(converter, 'node_modules/@playcanvas/splat-transform/bin/cli.mjs'), '--version'], root, runtimeEnv);
    await run([path.join(staging, 'ffmpeg', 'ffmpeg' + suffix), '-version'], root);
    await mkdir(path.join(staging, 'notices'));
    await cp(path.join(root, 'legal'), path.join(staging, 'notices/legal'), { recursive: true });
    await cp(path.join(root, 'LICENSE'), path.join(staging, 'notices/Worker-LICENSE'));
    await cp(path.join(root, 'THIRD_PARTY_NOTICES'), path.join(staging, 'notices/THIRD_PARTY_NOTICES'));
    const files = { node: await sha256(node), ffmpeg: await sha256(path.join(staging, 'ffmpeg', 'ffmpeg' + suffix)), ffprobe: await sha256(path.join(staging, 'ffmpeg', 'ffprobe' + suffix)) };
    const windowsCrtManifestSha256 = windows ? await sha256(path.join(staging, 'windows-crt/manifest.json')) : undefined;
    await writeFile(path.join(staging, 'manifest.json'), JSON.stringify({ version: 3, profile: 'native', target, nodeVersion: lock.nodeVersion, ffmpegRelease: lock.ffmpegRelease, windowsCrtManifestSha256, files }, null, 2) + '\n');
    const backup = `${destination}.previous-${crypto.randomUUID()}`;
    const exists = await Bun.file(path.join(destination, 'manifest.json')).exists();
    if (exists) await rename(destination, backup);
    try { await rename(staging, destination); }
    catch (error) { if (exists) await rename(backup, destination); throw error; }
    if (exists) await rm(backup, { recursive: true, force: true });
    console.log(`Vendored runtime ready: ${destination}`);
  } finally { await rm(staging, { recursive: true, force: true }); }
}

if (import.meta.main) await vendorRuntime(path.resolve(process.argv[2] ?? path.join(root, 'dist/runtime')));
