import {test, expect} from 'bun:test';
import {mkdtemp, writeFile, readFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {BackgroundArchive} from '../agent/background-archive';
import {Client} from '../agent/http';
import {removeScratchPath} from '../agent/retention-files';

test('cancellation stops retries without acknowledging or deleting recovery data',async()=> {
  const root = await mkdtemp(join(tmpdir(),'guerrilla-cancel-'));
  await writeFile(join(root,'platform-state.json'),'{}');
  const abort = new AbortController();
  const server = Bun.serve({port:0,fetch:()=>new Response('',{status:409})});
  const queue = new BackgroundArchive(new Client(server.url.href,''),{scanId:'s',attemptId:'a',leaseId:'l'},root,abort.signal,
    async status=>{if(status==='retrying')abort.abort();},1);
  try {
    await queue.enqueue('pending',1);
    await expect(queue.drain()).rejects.toThrow();
    expect(await Bun.file(join(root,'.archive-acks','pending')).exists()).toBe(false);
    expect(await readFile(join(root,'.worker','archives','pending','platform-state.json'),'utf8')).toBe('{}');
    expect(await readFile(join(root,'platform-state.json'),'utf8')).toBe('{}');
  } finally {abort.abort();server.stop(true);await removeScratchPath(tmpdir(),root.slice(tmpdir().length+1));}
});

test('slow archival freezes bytes, coalesces pending snapshots, retries, and gates completion', async()=> {
  const root = await mkdtemp(join(tmpdir(),'guerrilla-background-'));
  await writeFile(join(root,'platform-state.json'),'{"value":"first"}');
  let release!:()=>void;
  const held = new Promise<void>(resolve=>{release=resolve;});
  let started!:()=>void;
  const transferring = new Promise<void>(resolve=>{started=resolve;});
  const bytes:string[] = [], commits:string[] = [], statuses:string[] = [];
  let puts = 0;
  const server = Bun.serve({port:0,async fetch(request):Promise<Response> {
    const route = new URL(request.url).pathname;
    if (route === '/api/worker/artifacts') {
      const body = await request.json() as {checkpointId:string};
      return Response.json({artifactId:body.checkpointId,method:'put',url:new URL('/upload',server.url).href});
    }
    if (route === '/upload') {
      if (++puts === 1) {started();await held;return new Response('',{status:500});}
      bytes.push(await request.text());return new Response('');
    }
    if (route === '/api/worker/complete' && puts === 2) return new Response('',{status:409});
    if (route === '/api/worker/checkpoint') commits.push((await request.json() as {checkpointId:string}).checkpointId);
    return Response.json({});
  }});
  const abort = new AbortController();
  const queue = new BackgroundArchive(new Client(server.url.href,''),{scanId:'s',attemptId:'a',leaseId:'l'},root,abort.signal,async state=>{statuses.push(state);},1);
  try {
    await queue.enqueue('one',1);
    await transferring;
    expect(await readFile(join(root,'.archive-acks','one.ready'),'utf8')).toBe('snapshot ready');
    expect(await Bun.file(join(root,'.archive-acks','one')).exists()).toBe(false);
    await writeFile(join(root,'platform-state.json'),'{"value":"second"}');await queue.enqueue('two',2);
    await writeFile(join(root,'platform-state.json'),'{"value":"third"}');await queue.enqueue('three',3);
    let done = false;const draining = queue.drain().then(()=>{done=true;});
    expect(done).toBe(false);
    release();await draining;
    expect(bytes[0]).toBe('{"value":"first"}');
    expect(bytes.at(-1)).toBe('{"value":"third"}');
    expect(commits).toEqual(['one','three']);
    expect(statuses).toContain('retrying');
    expect(statuses.at(-1)).toBe('saved');
    expect(done).toBe(true);
  } finally {
    release();abort.abort();await queue.drain().catch(()=>{});server.stop(true);
    await removeScratchPath(tmpdir(),root.slice(tmpdir().length+1));
  }
});
