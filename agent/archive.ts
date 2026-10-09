import { randomUUID } from 'node:crypto';
import { stat, mkdir, writeFile } from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { join } from 'node:path';
import type { Fence, UploadIntent } from '@guerrilla/worker-protocol';
import { Client } from './http';
import { hashFile, upload, transferUrl } from './transfers';
import { checkedPath, selectedFiles, retentionExclusions } from './retention-files';

export async function archiveFile(client: Client, fence: Fence, file: string, relativePath: string, role: string, checkpointId: string | undefined, signal: AbortSignal, clientId: string = randomUUID()) {
  const identity = await hashFile(file), before = await stat(file);
  const allocation = {...fence, action:'allocate', clientId, relativePath, role, ...identity, checkpointId, reuseVerified:true};
  let intent = await client.post<UploadIntent>('artifacts', allocation, signal);
  if (intent.method === 'reuse') return intent.artifactId;
  if (intent.method === 'verify') {
    await client.post('complete', {...fence,artifactId:intent.artifactId},signal);
    return intent.artifactId;
  }
  const parts: {partNumber:number;etag:string}[] = [];
  if (intent.method === 'multipart') {
    if (!intent.partSize) throw new Error('Incomplete multipart intent');
    const partNumbers = Array.from({length:Math.ceil(identity.size / intent.partSize)},(_,index)=>index+1);
    const signed = await client.post<{parts:{partNumber:number;url:string}[]}>('artifacts',{...fence,action:'parts',artifactId:intent.artifactId,partNumbers},signal);
    intent.parts = signed.parts;
    if (!intent.parts?.length) throw new Error('Missing multipart URLs');
    for (const part of intent.parts) {
      transferUrl(part.url);
      const start = (part.partNumber - 1) * intent.partSize!;
      const send = (url:string) => fetch(url, {method:'PUT', headers:{'content-length':String(Math.min(intent.partSize!,identity.size-start))}, body:createReadStream(file, {start,end:Math.min(start + intent.partSize!,identity.size)-1}) as never, duplex:'half',signal,redirect:'error'} as RequestInit);
      let response = await send(part.url);
      if (response.status === 403) {
        const refreshedIntent = await client.post<{parts:{partNumber:number;url:string}[]}>('artifacts',{...fence,action:'parts',artifactId:intent.artifactId,partNumbers:[part.partNumber]},signal);
        const refreshed = refreshedIntent.parts?.find(candidate=>candidate.partNumber === part.partNumber);
        if (!refreshed) throw new Error('Missing refreshed multipart URL');
        response = await send(refreshed.url);
      }
      const etag = response.headers.get('etag');
      if (!response.ok || !etag) throw new Error(`Multipart upload failed (${response.status})`);
      parts.push({partNumber:part.partNumber,etag});
    }
  } else {
    if (!intent.url) throw new Error('Missing upload URL');
    try { await upload(intent.url,file,signal); }
    catch(error) {
      signal.throwIfAborted();
      intent = await client.post<UploadIntent>('artifacts',allocation,signal);
      if (!intent.url) throw error;
      await upload(intent.url,file,signal);
    }
  }
  const after = await stat(file);
  if (before.size !== after.size || before.mtimeMs !== after.mtimeMs || (await hashFile(file)).sha256 !== identity.sha256) throw new Error('Artifact changed during upload');
  await client.post('complete', {...fence,artifactId:intent.artifactId,...(parts.length ? {parts} : {})},signal);
  return intent.artifactId;
}
export async function checkpoint(client: Client, fence: Fence, root: string, checkpointId: string, signal: AbortSignal, sequence = 1, cache = new Map<string, {clientId:string; artifactId?:string}>()) {
  if (!/^[a-zA-Z0-9_-]{1,100}$/.test(checkpointId)) throw new Error('Invalid checkpoint ID');
  const artifactIds: string[] = [];
  for await (const relative of selectedFiles(root,await retentionExclusions(root))) {
    const role = relative === 'platform-state.json' ? 'checkpoint' : /\.(sog|spz)$/.test(relative) ? 'export' : /\.(log|jsonl)$/.test(relative) ? 'log' : 'intermediate';
    const entry = cache.get(relative) ?? {clientId:randomUUID()};
    cache.set(relative, entry);
    entry.artifactId ??= await archiveFile(client,fence,await checkedPath(root,relative),relative,role,checkpointId,signal,entry.clientId);
    artifactIds.push(entry.artifactId);
  }
  await client.post('checkpoint',{...fence,checkpointId,artifactIds,sequence},signal);
  const directory = join(root,'.archive-acks'); await mkdir(directory,{recursive:true});
  await writeFile(join(directory,checkpointId),'committed',{mode:0o600});
}
