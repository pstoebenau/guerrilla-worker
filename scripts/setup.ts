import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { access, chmod, mkdir, mkdtemp, rename, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spirulaAssets, spirulaDirectory, spirulaVersion } from './spirula-release';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const asset = spirulaAssets[`${process.platform}-${process.arch}` as keyof typeof spirulaAssets];
if (!asset && !process.env.SPIRULA_BIN) throw new Error('Automatic setup supports Windows/Linux x64 and macOS arm64. Other hosts can supply SPIRULA_BIN and PYTHON.');
const executable = path.join(spirulaDirectory, asset?.executable ?? (process.platform === 'win32' ? 'spirula.exe' : 'spirula'));

async function run(args: string[], env: NodeJS.ProcessEnv = process.env) {
  const child = Bun.spawn(args, { cwd: root, env, stdin: 'inherit', stdout: 'inherit', stderr: 'inherit', windowsHide: true });
  if (await child.exited !== 0) throw new Error(`Worker setup failed at ${path.basename(args[0]!)}; fix the error above and rerun.`);
}
async function hash(file: string) {
  const digest = createHash('sha256');
  for await (const chunk of createReadStream(file)) digest.update(chunk);
  return digest.digest('hex');
}
for (const command of ['node', 'npm', 'ffmpeg']) {
  if (!Bun.which(command)) throw new Error(`Install ${command} and add it to PATH, then rerun bun run setup.`);
}
const python = process.env.PYTHON ?? path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
try { await access(python); }
catch {
  if (process.env.PYTHON) throw new Error('PYTHON override does not exist.');
  if (Bun.which('uv')) await run(['uv', 'venv', '--python', '3.12', path.join(root, '.venv')]);
  else {
    const launcher = Bun.which('python3') ?? Bun.which('python') ?? Bun.which('py');
    if (!launcher) throw new Error('Install Python 3.12+ or uv, then rerun setup.');
    await run([launcher, '-m', 'venv', path.join(root, '.venv')]);
  }
}
if (!process.env.SPIRULA_BIN && asset) {
  if (await Bun.file(executable).exists()) {
    if (await hash(executable) !== asset.binarySha) throw new Error('Installed Spirula checksum mismatch; the existing runtime was preserved.');
  } else {
    const parent = path.dirname(spirulaDirectory);
    await mkdir(parent, { recursive: true, mode: 0o700 });
    const staging = await mkdtemp(path.join(parent, '.spirula-'));
    let mount: string | undefined;
    try {
      console.log(`Downloading verified Spirula ${spirulaVersion} for ${process.platform}-${process.arch}…`);
      const response = await fetch(`https://github.com/harry7557558/spirula-studio/releases/download/v${spirulaVersion}/${asset.name}`, { signal: AbortSignal.timeout(300_000) });
      if (!response.ok) throw new Error(`Spirula download failed: HTTP ${response.status}`);
      const archive = path.join(staging, asset.name);
      await Bun.write(archive, response);
      if (await hash(archive) !== asset.archiveSha) throw new Error('Spirula archive checksum mismatch; installation refused.');
      const install = path.join(staging, 'install');
      await mkdir(install);
      if (asset.name.endsWith('.dmg')) {
        mount = await mkdtemp(path.join(tmpdir(), 'guerrilla-spirula-'));
        await run(['hdiutil', 'attach', archive, '-readonly', '-nobrowse', '-mountpoint', mount]);
        await run(['ditto', path.join(mount, 'Spirula Studio.app'), path.join(install, 'Spirula Studio.app')]);
        await symlink('Spirula Studio.app/Contents/MacOS/spirula', path.join(install, asset.executable));
      } else {
        await run([python, '-m', 'zipfile', '-e', archive, install]);
      }
      const binary = path.join(install, asset.executable);
      if (await hash(binary) !== asset.binarySha) throw new Error('Spirula executable checksum mismatch.');
      await chmod(binary, 0o755);
      await rename(install, spirulaDirectory);
    } finally {
      if (mount) {
        // Never remove a mount directory until its disk image is detached.
        await run(['hdiutil', 'detach', mount]);
        await rm(mount, { recursive: true, force: true });
      }
      await rm(staging, { recursive: true, force: true });
    }
  }
}
if (Bun.which('uv')) await run(['uv', 'pip', 'install', '--python', python, '-r', 'pipeline/requirements.txt']);
else await run([python, '-m', 'pip', 'install', '-r', 'pipeline/requirements.txt']);
await run([process.execPath, 'install', '--frozen-lockfile']);
await run(['npm', 'ci', '--prefix', 'pipeline/docker/converter']);
await run([process.execPath, 'run', 'build']);
await run(['node', 'dist/worker.mjs', '--preflight'], { ...process.env, PYTHON: python });
console.log('Worker ready. Set WORKER_CONTROL_URL, run bun start enroll --token-stdin, then bun start.');
