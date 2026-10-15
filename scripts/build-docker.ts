import { cp, mkdir, mkdtemp, readFile, rm } from 'node:fs/promises';
import { homedir } from 'node:os';
import path from 'node:path';
import { sha256, verifiedDownload } from './vendor-runtime';

type Asset = { url: string; sha256: string };
export type EngineLock = {
  recipe: string;
  releaseUrl: string;
  archive: Asset;
  metadata: Asset;
  checksums: Asset;
};
const root = path.resolve(import.meta.dir, '..');
const archiveName = 'lichtfeld-linux-amd64.tar.gz';

export function validateEngineArtifact(lock: EngineLock, metadata: unknown, checksums: string): void {
  if (!/^[a-f0-9]{64}$/.test(lock.recipe) || new URL(lock.releaseUrl).protocol !== 'https:') throw new Error('Invalid Linux engine recipe');
  if (!metadata || typeof metadata !== 'object') throw new Error('Invalid Linux engine metadata');
  const identity = metadata as Record<string, unknown>;
  if (identity.version !== 1 || identity.recipe !== lock.recipe || identity.archive !== archiveName
    || identity.sha256 !== lock.archive.sha256 || identity.platform !== 'linux-amd64' || identity.cpuBaseline !== 'x86-64-v3') {
    throw new Error('Linux engine artifact does not match the pinned recipe, archive, or platform');
  }
  const records = new Map<string, string>();
  for (const line of checksums.trim().split(/\r?\n/)) {
    const match = /^([a-f0-9]{64})  ([A-Za-z0-9_.-]+)$/.exec(line);
    if (!match || match[2] === '.' || match[2] === '..' || records.has(match[2]!)) throw new Error('Invalid Linux engine checksum manifest');
    records.set(match[2]!, match[1]!);
  }
  if (records.get(archiveName) !== lock.archive.sha256 || records.get('engine-artifact.json') !== lock.metadata.sha256) {
    throw new Error('Linux engine checksum manifest does not match the pinned assets');
  }
  if (!records.has('build-source-inventory.json')
    || ![...records.keys()].some(name => /^lichtfeld-source\.tar\.gz\.part\d+$/.test(name))
    || ![...records.keys()].some(name => /^lichtfeld-build-dependencies\.tar\.gz\.part\d+$/.test(name))) {
    throw new Error('Linux engine artifact is missing its matching source evidence');
  }
}

async function run(args: string[]): Promise<void> {
  const child = Bun.spawn(args, { cwd: root, stdin: 'inherit', stdout: 'inherit', stderr: 'inherit' });
  if (await child.exited !== 0) throw new Error('Docker build failed; inspect the build output above');
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
export async function buildImages(options: ImageBuild, lock: EngineLock, context: string, execute = run): Promise<void> {
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
  await execute([...build(options.runtimeImage), '--platform', 'linux/amd64', '--file', 'Dockerfile.runtime', '--target', 'combined-runtime',
    '--build-context', `lichtfeld-artifact=${context}`, '--build-arg', `LICHTFELD_SHA256=${lock.archive.sha256}`,
    '--build-arg', `LICHTFELD_METADATA_SHA256=${lock.metadata.sha256}`, '--build-arg', `LICHTFELD_CHECKSUMS_SHA256=${lock.checksums.sha256}`,
    '--build-arg', `LICHTFELD_RECIPE=${lock.recipe}`, '--build-arg', `LICHTFELD_RELEASE_URL=${lock.releaseUrl}`,
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

export async function buildDocker(args: string[]): Promise<void> {
  let runtimeImage = 'guerrilla-runtime:local';
  let workerImage = 'guerrilla-worker:local';
  let push = false;
  let metadataFile: string | undefined;
  for (let index = 0; index < args.length; index++) {
    const flag = args[index];
    if (flag === '--help') {
      console.log('Build the Linux amd64 worker with bundled LichtFeld, Spirula, plugins and dependencies.\nUsage: bun run build:docker [--runtime-image TAG] [--worker-image TAG] [--push] [--metadata-file PATH]\nDocker/BuildKit is required. No engine installation or host Python is required. Builds stay local unless --push is supplied. Registry builds use Buildx and registry caches.');
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
  const decoded = JSON.parse(await readFile(path.join(root, 'packaging/docker-engines.lock.json'), 'utf8')) as { version: number; lichtfeld: EngineLock };
  if (decoded.version !== 1 || !decoded.lichtfeld) throw new Error('Unsupported Docker engine lock');
  const lock = decoded.lichtfeld;
  const cache = path.resolve(process.env.WORKER_BUILD_CACHE ?? path.join(homedir(), '.cache/guerrilla-worker/build'), 'linux-engines');
  // Downloads are pinned and individually hashed before any engine bytes enter
  // Docker. Reuse the existing public Linux build rather than recompiling it.
  const files = await Promise.all([lock.archive, lock.metadata, lock.checksums].map(asset => verifiedDownload(asset, cache)));
  validateEngineArtifact(lock, JSON.parse(await readFile(files[1]!, 'utf8')), await readFile(files[2]!, 'utf8'));
  await mkdir(cache, { recursive: true });
  const context = await mkdtemp(path.join(cache, '.lichtfeld-context-'));
  try {
    for (const [index, name] of [archiveName, 'engine-artifact.json', 'SHA256SUMS.txt'].entries()) {
      await cp(files[index]!, path.join(context, name));
      if (await sha256(path.join(context, name)) !== [lock.archive, lock.metadata, lock.checksums][index]!.sha256) throw new Error('Linux engine staging checksum mismatch');
    }
    await buildImages({ runtimeImage, workerImage, version, push, metadataFile }, lock, context);
    console.log(`Self-contained Linux worker ready: ${workerImage}`);
  } finally {
    await rm(context, { recursive: true, force: true });
  }
}

if (import.meta.main) await buildDocker(process.argv.slice(2));
