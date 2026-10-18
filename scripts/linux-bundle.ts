import { readdir } from 'node:fs/promises';
import path from 'node:path';

export async function linuxRuntimeLibraryPath(runtime: string, inherited = ''): Promise<string> {
  const directories = new Set<string>();
  async function visit(directory: string): Promise<void> {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      if (entry.isDirectory()) await visit(path.join(directory, entry.name));
      else if (/\.so(?:\.\d+)*$/.test(entry.name)) directories.add(directory);
    }
  }
  await visit(runtime);
  return [...directories].sort().concat(inherited ? [inherited] : []).join(':');
}
export async function linuxBundleEnvironment(runtime: string, inherited: NodeJS.ProcessEnv = process.env): Promise<NodeJS.ProcessEnv> {
  return { ...inherited, LD_LIBRARY_PATH: await linuxRuntimeLibraryPath(runtime, inherited.LD_LIBRARY_PATH) };
}
