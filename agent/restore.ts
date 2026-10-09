import type { ArtifactManifest, Fence } from '@guerrilla/worker-protocol';
import { Client } from './http';
import { checkedPath } from './retention-files';
import { safeRelativePath } from './paths';
import { download } from './transfers';

/** The runner requests dependencies; only the assignment's verified objects can be read. */
export async function restoreArtifacts(client:Client, fence:Fence, files:ArtifactManifest[], root:string,
  paths:string[], signal:AbortSignal, restored = new Set<string>()) {
  for (const path of paths) safeRelativePath(path);
  for (const file of files) {
    if (restored.has(file.artifactId) || !paths.some(path=>file.relativePath===path || file.relativePath.startsWith(path+'/'))) continue;
    const target = await checkedPath(root,file.relativePath);
    try { await download(file.url,target,file,signal); }
    catch {
      signal.throwIfAborted();
      const response = await client.post<{artifacts:ArtifactManifest[]}>('refresh',{...fence,artifactIds:[file.artifactId]},signal);
      const fresh = response.artifacts.find(item=>item.artifactId===file.artifactId);
      if (!fresh || fresh.relativePath!==file.relativePath || fresh.sha256!==file.sha256 || fresh.size!==file.size) throw new Error('Resume artifact identity changed');
      await download(fresh.url,target,file,signal);
    }
    restored.add(file.artifactId);
  }
}
