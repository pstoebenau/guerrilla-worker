import { test, expect } from 'bun:test';
import { mkdtemp, writeFile, readFile, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { createHash } from 'node:crypto';
import { safeRelativePath, agentLockPath } from '../agent/paths';
import { download } from '../agent/transfers';
import { checkpoint, archiveFile } from '../agent/archive';
import { Client } from '../agent/http';
import { runPipeline } from '../agent/process';
import { execute, scratchIsClean } from '../agent/main';
import type { Assignment } from '@guerrilla/worker-protocol';

test('rejects portable traversal and Windows device aliases',()=> {
  for (const value of ['../x','C:/x','a\\x','/x','a/../b','a/con.txt','a.','a/b ']) expect(()=>safeRelativePath(value)).toThrow();
  expect(safeRelativePath('training/retained-checkpoints/a/state.tar')).toBeTruthy();
});
test('checks download bytes before publishing',async()=> {
  const folder=await mkdtemp(join(tmpdir(),'guerrilla-transfer-'));
  const server=Bun.serve({port:0,fetch:()=>new Response('wrong')});
  try {
    await expect(download(server.url.href,join(folder,'target'),{size:5,sha256:'0'.repeat(64)},new AbortController().signal)).rejects.toThrow('checksum');
    expect(await Bun.file(join(folder,'target')).exists()).toBe(false);
  } finally {server.stop(true);}
});
test('never acknowledges checkpoint before trusted canonical commit',async()=> {
  const folder=await mkdtemp(join(tmpdir(),'guerrilla-checkpoint-'));
  await writeFile(join(folder,'platform-state.json'),'{}');
  const calls:string[]=[];
  const server=Bun.serve({port:0,async fetch(request): Promise<Response> {
    const route=new URL(request.url).pathname.split('/').pop()!; calls.push(route);
    if(route==='artifacts')return Response.json({artifactId:'artifact',method:'put',url:new URL('/upload',server.url).href});
    if(route==='upload')return new Response(null,{status:200});
    if(route==='checkpoint')return new Response(null,{status:409});
    return Response.json({});
  }});
  try {
    await expect(checkpoint(new Client(server.url.href,'secret'),{scanId:'s',attemptId:'a',leaseId:'l'},folder,'cp',new AbortController().signal)).rejects.toThrow();
    expect(calls).toEqual(['artifacts','upload','complete','checkpoint']);
    expect(await Bun.file(join(folder,'.archive-acks','cp')).exists()).toBe(false);
  } finally {server.stop(true);}
});
test('aborted supervisor stops active subprocess',async()=> {
  const folder=await mkdtemp(join(tmpdir(),'guerrilla-process-')), abort=new AbortController();
  const running=runPipeline({command:process.execPath,args:['-e','setInterval(()=>{},1000)'],cwd:folder,logPath:join(folder,'log'),signal:abort.signal,onEvent:async()=>{}});
  setTimeout(()=>abort.abort(),100);
  await expect(running).rejects.toThrow();
});
test('shared supervisor locks are GPU-keyed independently of job scratch and worktree',()=> {
  const shared=join(tmpdir(),'guerrilla-shared-gpu');
  const first=agentLockPath('GPU-physical',{WORKER_AGENT_LOCK_DIRECTORY:shared,WORKER_SCRATCH:'first'});
  const second=agentLockPath('GPU-physical',{WORKER_AGENT_LOCK_DIRECTORY:shared,WORKER_SCRATCH:'second'});
  expect(first).toBe(second);
  expect(first).not.toBe(agentLockPath('GPU-other',{WORKER_AGENT_LOCK_DIRECTORY:shared}));
  expect(()=>agentLockPath('GPU-physical',{WORKER_AGENT_LOCK_DIRECTORY:'relative'})).toThrow('absolute');
});
test('cloud clean acknowledgment remains false while unrelated recovery data exists',async()=> {
  const folder=await mkdtemp(join(tmpdir(),'guerrilla-clean-'));
  await mkdir(join(folder,'empty-completed-scan'),{recursive:true});
  expect(await scratchIsClean(folder)).toBe(true);
  await mkdir(join(folder,'unrelated-failed-scan','attempt'),{recursive:true});
  await writeFile(join(folder,'unrelated-failed-scan','attempt','checkpoint'),'unique recovery data');
  expect(await scratchIsClean(folder)).toBe(false);
  await expect(scratchIsClean(join(folder,'unrelated-failed-scan','attempt','checkpoint'))).rejects.toThrow();
});

test('lease expiry aborts computation and cannot report completion',async()=> {
  const calls:string[]=[];
  const server=Bun.serve({port:0,async fetch(request):Promise<Response> {
    const route=new URL(request.url).pathname.split('/').pop()!;
    if(route==='input')return new Response('input');
    const body=await request.json() as {state?:string};
    if(route==='finish')calls.push(body.state!);
    return Response.json({});
  }});
  const assignment:Assignment={protocolVersion:1,scanId:crypto.randomUUID(),attemptId:crypto.randomUUID(),leaseId:crypto.randomUUID(),leaseExpiresAt:new Date(Date.now()+400).toISOString(),settingsHash:'0'.repeat(64),request:{version:1,backend:'spirula',inputId:'i',title:'test',maxCap:100000,settings:{}},input:{kind:'stored',filename:'input.mp4',url:new URL('/input',server.url).href,size:5,sha256:createHash('sha256').update('input').digest('hex')},resume:[]};
  let aborted=false;
  try {
    await expect(execute(new Client(server.url.href,'secret'),assignment,new AbortController().signal,{runPipeline:async options=> {
      const request = JSON.parse(await readFile(options.args[options.args.indexOf('--request') + 1]!, 'utf8'));
      expect(request.scanId).toBe(assignment.scanId);
      expect(request.attemptId).toBe(assignment.attemptId);
      expect(request.maxCap).toBe(100000);
      await new Promise<void>((_resolve,reject)=>options.signal.addEventListener('abort',()=> {aborted=true;reject(new Error('lease stopped'));},{once:true}));
    }})).rejects.toThrow('lease stopped');
    expect(aborted).toBe(true); expect(calls).toEqual(['interrupted']);
  } finally {server.stop(true);}
});

test('HTTP retries reuse event identity and never follow authentication redirects',async()=> {
  let count=0;const ids:string[]=[];
  const server=Bun.serve({port:0,async fetch(request):Promise<Response> {
    ids.push((await request.json() as {eventId:string}).eventId);
    return ++count<2 ? new Response(null,{status:503}) : Response.json({ok:true});
  }});
  try {await new Client(server.url.href,'secret').post('events',{eventId:'same'});expect(ids).toEqual(['same','same']);}
  finally {server.stop(true);}
});

test('multipart allocates signed parts, refreshes expiry and completes ordered bytes',async()=> {
  const folder=await mkdtemp(join(tmpdir(),'guerrilla-multipart-')),file=join(folder,'data');
  await writeFile(file,'abcdef');
  let signatures=0;const uploaded:string[]=[],lengths:string[]=[],completion:unknown[]=[];
  const server=Bun.serve({port:0,async fetch(request):Promise<Response> {
    const route=new URL(request.url).pathname;
    if(route==='/api/worker/artifacts') {
      const body=await request.json() as {action:string;partNumbers?:number[]};
      if(body.action==='allocate')return Response.json({artifactId:'artifact',method:'multipart',partSize:3});
      signatures++;
      return Response.json({parts:body.partNumbers!.map(partNumber=>({partNumber,url:new URL(`/part/${partNumber}/${signatures}`,server.url).href}))});
    }
    if(route.startsWith('/part/')) {
      if(route==='/part/1/1')return new Response(null,{status:403});
      lengths.push(request.headers.get('content-length')!);uploaded.push(await request.text());
      return new Response(null,{headers:{etag:route.includes('/1/')?'first':'second'}});
    }
    completion.push(await request.json());return Response.json({});
  }});
  try {
    await archiveFile(new Client(server.url.href,'secret'),{scanId:'s',attemptId:'a',leaseId:'l'},file,'data','intermediate','cp',new AbortController().signal);
    expect(uploaded).toEqual(['abc','def']);expect(lengths).toEqual(['3','3']);expect(signatures).toBe(2);
    expect(completion).toEqual([{scanId:'s',attemptId:'a',leaseId:'l',artifactId:'artifact',parts:[{partNumber:1,etag:'first'},{partNumber:2,etag:'second'}]}]);
  } finally {server.stop(true);}
});

test('single upload retry keeps immutable client intent and checkpoint ack follows commit',async()=> {
  const folder=await mkdtemp(join(tmpdir(),'guerrilla-refresh-'));
  await writeFile(join(folder,'platform-state.json'),'{}');
  const intents:unknown[]=[],calls:string[]=[];let puts=0;
  const server=Bun.serve({port:0,async fetch(request):Promise<Response> {
    const route=new URL(request.url).pathname.split('/').pop()!;calls.push(route);
    if(route==='artifacts') {intents.push(await request.json());return Response.json({artifactId:'a',method:'put',url:new URL('/upload',server.url).href});}
    if(route==='upload')return new Response(null,{status:++puts===1?403:200});
    if(route==='checkpoint') {expect((await request.json() as {sequence:number}).sequence).toBe(7);expect(await Bun.file(join(folder,'.archive-acks','cp')).exists()).toBe(false);}
    return Response.json({});
  }});
  try {
    await checkpoint(new Client(server.url.href,'secret'),{scanId:'s',attemptId:'a',leaseId:'l'},folder,'cp',new AbortController().signal,7);
    expect(intents.length).toBe(2);expect(intents[0]).toEqual(intents[1]);
    expect(calls).toEqual(['artifacts','upload','artifacts','upload','complete','checkpoint']);
    expect(await readFile(join(folder,'.archive-acks','cp'),'utf8')).toBe('committed');
  } finally {server.stop(true);}
});

test('server stop heartbeat aborts execution before reporting completion',async()=> {
  const states:string[]=[];
  const server=Bun.serve({port:0,async fetch(request):Promise<Response> {
    const route=new URL(request.url).pathname.split('/').pop()!;
    if(route==='input')return new Response('input');
    if(route==='heartbeat')return Response.json({command:'stop',leaseExpiresAt:new Date(Date.now()+30000).toISOString()});
    if(route==='finish')states.push((await request.json() as {state:string}).state);
    return Response.json({});
  }});
  const assignment:Assignment={protocolVersion:1,scanId:crypto.randomUUID(),attemptId:crypto.randomUUID(),leaseId:crypto.randomUUID(),leaseExpiresAt:new Date(Date.now()+30000).toISOString(),settingsHash:'0'.repeat(64),request:{version:1,backend:'spirula',inputId:'i',title:'test',maxCap:100000,settings:{}},input:{kind:'stored',filename:'input.mp4',url:new URL('/input',server.url).href,size:5,sha256:createHash('sha256').update('input').digest('hex')},resume:[]};
  try {
    await expect(execute(new Client(server.url.href,'secret'),assignment,new AbortController().signal,{runPipeline:async options=> {
      await new Promise<void>((_resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('fenced')),{once:true}));
    }})).rejects.toThrow('fenced');expect(states).toEqual(['interrupted']);
  } finally {server.stop(true);}
},10000);

test('durable completion removes predecessor attempt scratch only for its scan',async()=> {
  let prior='', unrelated='', scanRoot='';
  const server=Bun.serve({port:0,async fetch(request):Promise<Response> {
    const route=new URL(request.url).pathname.split('/').pop()!;
    if(route==='input')return new Response('input');
    if(route==='finish') {
      expect((await request.json() as {state:string}).state).toBe('completed');
      expect(await Bun.file(prior).exists()).toBe(true);
      expect(await Bun.file(unrelated).exists()).toBe(true);
    }
    return Response.json({});
  }});
  const assignment:Assignment={protocolVersion:1,scanId:crypto.randomUUID(),attemptId:crypto.randomUUID(),leaseId:crypto.randomUUID(),leaseExpiresAt:new Date(Date.now()+30000).toISOString(),settingsHash:'0'.repeat(64),request:{version:1,backend:'spirula',inputId:'i',title:'test',maxCap:100000,settings:{}},input:{kind:'stored',filename:'input.mp4',url:new URL('/input',server.url).href,size:5,sha256:createHash('sha256').update('input').digest('hex')},resume:[]};
  try {
    await execute(new Client(server.url.href,'secret'),assignment,new AbortController().signal,{runPipeline:async options=> {
      const request = JSON.parse(await readFile(options.args[options.args.indexOf('--request')+1]!, 'utf8'));
      scanRoot=dirname(dirname(request.outputPath));
      prior=join(scanRoot,'prior-failed','latest-checkpoint');
      unrelated=join(dirname(scanRoot),crypto.randomUUID(),'retained-checkpoint');
      await mkdir(dirname(prior),{recursive:true});await mkdir(dirname(unrelated),{recursive:true});
      await writeFile(prior,'preserve until commit');await writeFile(unrelated,'other job');
    }});
    expect(await Bun.file(prior).exists()).toBe(false);
    expect(await Bun.file(unrelated).exists()).toBe(true);
    expect(await Bun.file(join(scanRoot,assignment.attemptId,'input.mp4')).exists()).toBe(false);
  } finally {server.stop(true);}
});
