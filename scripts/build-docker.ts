import { mkdir, mkdtemp, readFile, rm } from 'node:fs/promises';
import { homedir } from 'node:os';
import path from 'node:path';
import { runDevelopmentCommand, withDevelopmentShutdown } from './development-process';

const root = path.resolve(import.meta.dir, '..');

async function run(args: string[], env = process.env, signal?: AbortSignal): Promise<void> {
  if ((await runDevelopmentCommand(args[0]!, args.slice(1), { cwd: root, env, signal })).code !== 0) throw new Error('Docker build failed; inspect the build output above');
}

type ImageBuild = {
  runtimeImage: string;
  workerImage: string;
  version: string;
  push: boolean;
  metadataFile?: string;
};

// Keep registry publication in the same builder as local builds, including the
// verified engine context. Published workers consume the exact runtime digest.
export async function buildImages(options: ImageBuild, context: string, execute = run): Promise<void> {
  const repository = (tag: string) => tag.replace(/:[^/:]+$/, '');
  const build = (tag: string) => options.push
    ? ['docker', 'buildx', 'build', '--push', '--cache-from', `type=registry,ref=${repository(tag)}:buildcache`,
      '--cache-to', `type=registry,ref=${repository(tag)}:buildcache,mode=max`]
    : ['docker', 'build'];
  const runtimeMetadata = path.join(context, 'runtime-build.json');
  const workerMetadata = options.metadataFile ?? path.join(context, 'worker-build.json');
  const labels = ['--label', `org.opencontainers.image.version=${options.version}`];
  if (process.env.GITHUB_SHA) labels.push('--label', `org.opencontainers.image.revision=${process.env.GITHUB_SHA}`);
  if (process.env.GITHUB_REPOSITORY) labels.push('--label', `org.opencontainers.image.source=https://github.com/${process.env.GITHUB_REPOSITORY}`);
  await execute([...build(options.runtimeImage), '--platform', 'linux/amd64', '--file', 'Dockerfile.runtime', '--target', 'spirula-runtime',
    ...labels, ...(options.push ? ['--metadata-file', runtimeMetadata] : []), '--tag', options.runtimeImage, '.']);
  let runtime = options.runtimeImage;
  if (options.push) {
    const metadata = JSON.parse(await readFile(runtimeMetadata, 'utf8')) as Record<string, unknown>;
    const digest = metadata['containerimage.digest'];
    if (typeof digest !== 'string' || !/^sha256:[a-f0-9]{64}$/.test(digest)) throw new Error('Missing published runtime digest');
    runtime = `${repository(options.runtimeImage)}@${digest}`;
  }
  await execute([...build(options.workerImage), '--platform', 'linux/amd64', '--target', 'worker',
    '--build-arg', `WORKER_RUNTIME_IMAGE=${runtime}`, '--build-arg', `WORKER_VERSION=${options.version}`,
    ...labels, ...(options.push ? ['--metadata-file', workerMetadata] : []), '--tag', options.workerImage, '.']);
}

export async function buildDocker(args: string[], env = process.env): Promise<void> {
  await withDevelopmentShutdown(signal => buildDockerWithShutdown(args, env, signal));
}

async function buildDockerWithShutdown(args: string[], env: NodeJS.ProcessEnv, signal: AbortSignal): Promise<void> {
  let runtimeImage = 'guerrilla-runtime:local';
  let workerImage = 'guerrilla-worker:local';
  let push = false;
  let metadataFile: string | undefined;
  for (let index = 0; index < args.length; index++) {
    const flag = args[index];
    if (flag === '--help') {
      console.log('Build the Linux amd64 worker with Spirula Vulkan and native dense reconstruction.\nUsage: bun run build:docker [--runtime-image TAG] [--worker-image TAG] [--push] [--metadata-file PATH]\nDocker/BuildKit is required. No engine installation or host Python is required. Builds stay local unless --push is supplied. Registry builds use Buildx and registry caches.');
      return;
    }
    if (flag === '--push') { push = true; continue; }
    if (flag === '--metadata-file') {
      const value = args[++index];
      if (!value || value.startsWith('--')) throw new Error('A path is required after --metadata-file');
      metadataFile = path.resolve(value);
      continue;
    }
    if (flag !== '--runtime-image' && flag !== '--worker-image') throw new Error(`Unknown Docker build argument: ${flag}`);
    const value = args[++index];
    if (!value || !/^[a-zA-Z0-9][a-zA-Z0-9_.:/-]*$/.test(value)) throw new Error(`A valid image tag is required after ${flag}`);
    if (flag === '--runtime-image') runtimeImage = value;
    else workerImage = value;
  }
  if (runtimeImage === workerImage) throw new Error('Runtime and worker image tags must differ');
  if (metadataFile && !push) throw new Error('--metadata-file requires --push');
  if (push && (!runtimeImage.includes('/') || !workerImage.includes('/'))) throw new Error('--push requires explicit registry image names');
  const version = (JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')) as { version: string }).version;
  if (!/^\d+\.\d+\.\d+$/.test(version)) throw new Error('Invalid worker release version');
  const cache = path.resolve(env.WORKER_BUILD_CACHE ?? path.join(homedir(), '.cache/guerrilla-worker/build'));
  await mkdir(cache, { recursive: true });
  const context = await mkdtemp(path.join(cache, '.image-build-'));
  try {
    await buildImages({ runtimeImage, workerImage, version, push, metadataFile }, context, async args => {
      signal.throwIfAborted();
      await run(args, env, signal);
      signal.throwIfAborted();
    });
    console.log(`Self-contained Linux worker ready: ${workerImage}`);
  } finally {
    await rm(context, { recursive: true, force: true });
  }
}

if (import.meta.main) await buildDocker(process.argv.slice(2));
