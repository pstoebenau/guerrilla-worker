import { createHash, randomUUID } from 'node:crypto';
import { createReadStream, createWriteStream } from 'node:fs';
import { mkdir, rename, unlink, stat } from 'node:fs/promises';
import { dirname } from 'node:path';
import { Readable, Transform } from 'node:stream';
import { pipeline } from 'node:stream/promises';

export async function hashFile(file: string, signal?:AbortSignal) {
  const hash = createHash('sha256'); let size = 0;
  for await (const chunk of createReadStream(file,{signal})) { hash.update(chunk); size += chunk.length; }
  return {size, sha256: hash.digest('hex')};
}
export function transferUrl(value: string) {
  const url = new URL(value);
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) throw new Error('Invalid transfer URL');
  return url;
}
export async function download(url: string, destination: string, expected: {size:number;sha256:string}, signal: AbortSignal) {
  transferUrl(url);
  const response = await fetch(url, {signal, redirect:'error'});
  if (!response.ok || !response.body) throw new Error(`Download failed (${response.status})`);
  await mkdir(dirname(destination), {recursive:true});
  const temporary = `${destination}.${randomUUID()}.part`; let size = 0;
  const hash = createHash('sha256');
  try {
    await pipeline(Readable.fromWeb(response.body as never), new Transform({transform(chunk, _encoding, callback) {
      size += chunk.length;
      if (size > expected.size) { callback(new Error('Download exceeded manifest size')); return; }
      hash.update(chunk); callback(null, chunk);
    }}), createWriteStream(temporary, {flags:'wx', mode:0o600}), {signal});
    if (size !== expected.size || hash.digest('hex') !== expected.sha256) throw new Error('Download checksum mismatch');
    await rename(temporary, destination);
  } finally { await unlink(temporary).catch(() => {}); }
}
export type TransferProgress = (bytes: number) => Promise<void>;

export function uploadBody(file: string, onProgress?: TransferProgress, range?: {start:number;end:number}) {
  return Readable.from((async function* () {
    let bytes = 0;
    for await (const chunk of createReadStream(file, range)) {
      bytes += chunk.length;
      await onProgress?.(bytes);
      yield chunk;
    }
  })());
}

export async function upload(url: string, file: string, signal: AbortSignal, onProgress?: TransferProgress) {
  transferUrl(url);
  const response = await fetch(url, {method:'PUT', headers:{'content-length':String((await stat(file)).size)}, body:uploadBody(file,onProgress) as never, duplex:'half', signal, redirect:'error'} as RequestInit);
  if (!response.ok) throw new Error(`Upload failed (${response.status})`);
}
