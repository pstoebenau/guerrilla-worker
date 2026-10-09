import {test,expect} from 'bun:test';
import {mkdtemp,mkdir,readFile,writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {createHash} from 'node:crypto';
import type {ArtifactManifest,Assignment} from '@guerrilla/worker-protocol';
import {Client} from '../agent/http';
import {execute} from '../agent/runtime';
import {checkpoint} from '../agent/archive';
import {copyStageSnapshot,removeScratchPath} from '../agent/retention-files';

const sha=(bytes:string)=>createHash('sha256').update(bytes).digest('hex');
test('resume fetches only requested COLMAP inputs, no video or selection, and cancellation removes local work',async()=>{
  const gets:string[]=[],calls:string[]=[];
  let cancelling=false, job='',unrelated='';
  const payloads:Record<string,string>={'selection-a/frame.jpg':'frame','reconstruction-a/images/frame.jpg':'image','reconstruction-a/sparse/cameras.bin':'colmap','reconstruction-a/database.db':'unneeded'};
  const server=Bun.serve({port:0,async fetch(request):Promise<Response>{
    const route=new URL(request.url).pathname;
    if(route.startsWith('/files/')){const key=route.slice(7);gets.push(key);return new Response(payloads[key]);}
    calls.push(route);
    if(route.endsWith('/events') && cancelling)return new Response(null,{status:409});
    if(route.endsWith('/heartbeat'))return Response.json({command:'stop',reason:'cancelled',leaseExpiresAt:new Date(Date.now()+30000).toISOString()});
    if(route.endsWith('/cleanup')){
      expect(await Bun.file(join(job,'run/.worker/request.json')).exists()).toBe(false);
      expect(await Bun.file(unrelated).exists()).toBe(true);
    }
    return Response.json({});
  }});
  const files:ArtifactManifest[]=Object.entries(payloads).map(([relativePath,body])=>({artifactId:relativePath,relativePath,role:'intermediate',size:body.length,sha256:sha(body),url:new URL('/files/'+relativePath,server.url).href}));
  const a:Assignment={protocolVersion:2,scanId:crypto.randomUUID(),attemptId:crypto.randomUUID(),leaseId:crypto.randomUUID(),leaseExpiresAt:new Date(Date.now()+30000).toISOString(),settingsHash:'a'.repeat(64),request:{version:1,backend:'lichtfeld',inputId:'input',title:'test',maxCap:3,settings:{}},input:{kind:'stored',filename:'video',url:new URL('/video',server.url).href,size:5,sha256:sha('video')},resume:files,resumeState:{completed:{selection:{directory:'selection-a'}}}};
  try{
    await expect(execute(new Client(server.url.href,''),a,new AbortController().signal,{runPipeline:async options=>{
      const request=JSON.parse(await readFile(options.args[options.args.indexOf('--request')+1]!,'utf8'));
      job=join(request.outputPath,'..');
      unrelated=join(request.outputPath,'../../..',crypto.randomUUID(),'keep');
      await mkdir(join(unrelated,'..'),{recursive:true});await writeFile(unrelated,'other scan');
      expect(request.selectiveResume).toBe(true);
      await options.onEvent({type:'restore',stage:'densification',restoreId:'dense-inputs',paths:['reconstruction-a/images','reconstruction-a/sparse']});
      expect(await readFile(join(request.outputPath,'.worker/restored/dense-inputs'),'utf8')).toBe('verified');
      cancelling=true;
      await options.onEvent({type:'progress',stage:'densification',message:'running'});
    }})).rejects.toThrow('cancelled');
    expect(gets.sort()).toEqual(['reconstruction-a/images/frame.jpg','reconstruction-a/sparse/cameras.bin']);
    expect(calls).not.toContain('/video');
    expect(calls).toContain('/api/worker/cleanup');
  }finally{server.stop(true);}
});

test('stage snapshots exclude live engine scratch and retain remote stage references without uploading them',async()=>{
  const root=await mkdtemp(join(tmpdir(),'stages-')),snapshot=join(root,'.worker/snapshot');
  const remote:ArtifactManifest={artifactId:'saved-selection',relativePath:'selection-a/frame.jpg',role:'intermediate',size:5,sha256:sha('frame'),url:'http://localhost/unused'};
  const state={completed:{selection:{directory:'selection-a',files:[{path:remote.relativePath,size:5,sha256:remote.sha256}]}},stageCheckpoints:{}};
  await writeFile(join(root,'platform-state.json'),JSON.stringify(state));
  await mkdir(join(root,'densification-live'),{recursive:true});
  await writeFile(join(root,'densification-live/partial.npz'),'unsafe');
  await copyStageSnapshot(root,snapshot,new Set([remote.relativePath]));
  expect(await Bun.file(join(snapshot,'densification-live/partial.npz')).exists()).toBe(false);
  let commit:Record<string,unknown>|undefined;
  const allocations:string[]=[];
  const server=Bun.serve({port:0,async fetch(request):Promise<Response>{
    const route=new URL(request.url).pathname;
    if(route==='/upload'){await request.arrayBuffer();return new Response('');}
    const body=await request.json() as Record<string,unknown>;
    if(route.endsWith('/artifacts')){allocations.push(String(body.relativePath));return Response.json({artifactId:'state',method:'put',url:new URL('/upload',server.url).href});}
    if(route.endsWith('/checkpoint'))commit=body;
    return Response.json({});
  }});
  try{
    await checkpoint(new Client(server.url.href,''),{scanId:'s',attemptId:'a',leaseId:'l'},snapshot,'cp',new AbortController().signal,1,new Map(),undefined,[remote]);
    expect(allocations).toEqual(['platform-state.json']);
    expect(commit?.inheritedArtifactIds).toEqual(['saved-selection']);
    expect(commit?.artifactIds).toEqual(['state']);
  }finally{server.stop(true);await removeScratchPath(tmpdir(),root.slice(tmpdir().length+1));}
});
