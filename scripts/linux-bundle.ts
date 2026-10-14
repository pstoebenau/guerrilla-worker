import { readdir } from 'node:fs/promises';
import path from 'node:path';

// Python wheels resolve private libraries through their importing extension.
// linuxdeploy scans each ELF file independently, so it also needs those paths.
export async function linuxRuntimeLibraryPath(runtime: string, inherited = ''): Promise<string> {
  const directories = new Set<string>();
  async function visit(directory: string): Promise<void> {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      if (entry.isDirectory() && entry.name !== 'stubs') await visit(path.join(directory, entry.name));
      else if (/\.so(?:\.\d+)*$/.test(entry.name)) directories.add(directory);
    }
  }
  await visit(runtime);
  return [...directories].sort().concat(inherited ? [inherited] : []).join(':');
}

export async function linuxBundleEnvironment(runtime: string, inherited: NodeJS.ProcessEnv = process.env): Promise<NodeJS.ProcessEnv> {
  return {
    ...inherited,
    LD_LIBRARY_PATH: await linuxRuntimeLibraryPath(runtime, inherited.LD_LIBRARY_PATH),
    // libcuda is the owner's NVIDIA driver, not a redistributable CUDA runtime.
    // linuxdeploy accepts semicolon-separated exclusions even when a driver is
    // absent on the build machine. All other missing dependencies still fail.
    LINUXDEPLOY_EXCLUDED_LIBRARIES: ['libcuda.so.1', inherited.LINUXDEPLOY_EXCLUDED_LIBRARIES].filter(Boolean).join(';'),
  };
}
