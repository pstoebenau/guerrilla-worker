import { mkdir, readFile, writeFile, chmod, readdir, copyFile, lstat } from 'node:fs/promises';
import { homedir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { existsSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { execFileSync, spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import { ENGINES, PROTOCOL_VERSION, redactSecrets, type Assignment, type EnrollmentResponse, type Registration, type PollResponse, type HeartbeatResponse } from '@guerrilla/worker-protocol';
import { ApiError, Client } from './http';
import { runPipeline } from './process';
import { removeScratchPath } from './retention-files';
import { restoreArtifacts } from './restore';
import { download } from './transfers';
import { archiveFile } from './archive';
import { BackgroundArchive } from './background-archive';
import { agentLockPath } from './paths';
import { readEnrollmentToken } from './enrollment';
import { registrationFromReport } from './registration';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const pipelineRoot = process.env.PIPELINE_ROOT ?? (process.platform === 'linux' && root === '/opt' ? '/opt/pipeline' : path.join(root,'pipeline'));
const runner = path.join(pipelineRoot,'platform_runner.py');
const localPython = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const python = process.env.PYTHON ?? (existsSync(localPython) ? localPython : process.platform === 'win32' ? 'python' : 'python3');
const credentialFile = process.env.WORKER_CREDENTIAL_FILE ?? path.join(homedir(),'.config','guerrilla-worker','credentials.json');
const scratch = path.resolve(process.env.WORKER_SCRATCH ?? path.join(homedir(),'.cache','guerrilla-worker'));

export async function scratchIsClean(directory = scratch) {
  const rootEntry = await lstat(directory).catch(error=> {if(error.code==='ENOENT')return null;throw error;});
  if (!rootEntry) return true;
  if (!rootEntry.isDirectory() || rootEntry.isSymbolicLink()) throw new Error('Scratch inventory requires a real directory');
  const entries = await readdir(directory,{recursive:true,withFileTypes:true}).catch(error=> {if(error.code==='ENOENT')return [];throw error;});
  // Links and special entries are not proof of an empty owner workspace.
  return entries.every(entry=>entry.isDirectory());
}

function registration(): Registration {
  const report = JSON.parse(execFileSync(python,[runner,'--preflight'],{encoding:'utf8',windowsHide:true,maxBuffer:4*1024*1024}));
  return registrationFromReport(report);
}

export async function execute(client: Client, assignment: Assignment, shutdown: AbortSignal, dependencies = {runPipeline}) {
  if (assignment.protocolVersion !== PROTOCOL_VERSION || !ENGINES.includes(assignment.request.backend) || !Number.isSafeInteger(assignment.request.maxCap) || assignment.request.maxCap < 1) throw new Error('Incompatible assignment');
  for (const id of [assignment.scanId,assignment.attemptId,assignment.leaseId]) if (!/^[a-zA-Z0-9_-]{1,100}$/.test(id)) throw new Error('Invalid assignment identity');
  const fence = {scanId:assignment.scanId,attemptId:assignment.attemptId,leaseId:assignment.leaseId};
  const abort = new AbortController(), signal = AbortSignal.any([shutdown,abort.signal]);
  let deadline = Date.parse(assignment.leaseExpiresAt), sequence = 0, checkpointSequence = 0, drain = false, cancelled = false, pipelineFailure: string | undefined;
  if (!Number.isFinite(deadline) || deadline <= Date.now()) throw new Error('Expired assignment');
  const watchdog = setInterval(()=> { if (Date.now() >= deadline) abort.abort(new Error('Lease expired')); },250);
  let beating = false;
  const heartbeats = setInterval(async ()=> {
    if (beating || signal.aborted) return; beating = true;
    try {
      const response = await client.post<HeartbeatResponse>('heartbeat',fence,signal);
      if (response.command === 'stop') {
        cancelled = response.reason === 'cancelled';
        abort.abort(new Error(cancelled ? 'Assignment cancelled by owner.' : 'Assignment fenced'));
      }
      drain ||= response.command === 'drain';
      const renewed = Date.parse(response.leaseExpiresAt);
      if (Number.isFinite(renewed)) deadline = renewed;
    } catch { abort.abort(new Error('Lease renewal failed')); }
    finally { beating = false; }
  },5000);
  const job = path.join(scratch,assignment.scanId,assignment.attemptId), output = path.join(job,'run');
  let events = Promise.resolve();
  const sendEvent = (kind:string, message:string, progress:Record<string,unknown> = {}, stage?:string, runtimeVersions?:unknown) => {
    const body = {...fence,eventId:randomUUID(),sequence:++sequence,kind,message,progress,...(stage ? {stage} : {}),...(runtimeVersions ? {runtimeVersions} : {})};
    const next = events.then(async()=> { await client.post('events',body,signal); });
    events = next.catch(()=>{});
    return next;
  };
  const archiveAbort = new AbortController();
  const latestUploadSequence = new Map<string,number>();
  const archives = new BackgroundArchive(client,fence,output,AbortSignal.any([signal,archiveAbort.signal]),async (status,detail)=> {
    const message = status === 'retrying' ? 'Upload delayed. Local files retained; cleanup pending while uploads retry.' : status === 'saved' ? 'Stage outputs saved.' : 'Saving stage outputs in the background.';
    const {stages,...progress} = detail;
    for (const stage of stages) {
      if ((latestUploadSequence.get(stage) ?? 0) > progress.checkpointSequence) continue;
      latestUploadSequence.set(stage,progress.checkpointSequence);
      await sendEvent('upload',message,{...progress,status,unit:'bytes'},stage).catch(()=>{});
    }
  },5000,assignment.resume);
  try {
    await mkdir(path.join(output,'.worker'),{recursive:true});
    const restored = new Set<string>();
    const restore = (paths:string[]) => restoreArtifacts(client,fence,assignment.resume,output,paths,signal,restored);
    const resumeState = assignment.resumeState ?? null;
    if (resumeState) await writeFile(path.join(output,'platform-state.json'),JSON.stringify(resumeState),{mode:0o600});
    const needsInput = !(resumeState?.completed as Record<string,unknown> | undefined)?.selection;
    const input = path.join(job,'input.mp4');
    if (needsInput) {
    await sendEvent('stage','Preparing video',{status:'running'},'download');
    if (assignment.input.kind === 'stored') {
      if (!assignment.input.sha256 || assignment.input.size === null) throw new Error('Missing input identity');
      const expected = {sha256:assignment.input.sha256,size:assignment.input.size};
      try { await download(assignment.input.url,input,expected,signal); }
      catch {signal.throwIfAborted();const refreshed=await client.post<{input:Assignment['input']}>('refresh',{...fence,input:true,artifactIds:[]},signal);await download(refreshed.input.url,input,expected,signal);}
    } else {
      await dependencies.runPipeline({command:python,args:[runner,'--download',assignment.input.url,'--public-source','--destination',input],parentGuard:path.join(pipelineRoot,'parent_guard.py'),cwd:pipelineRoot,logPath:path.join(job,'download.log'),signal,onEvent:async()=>{}});
      await archiveFile(client,fence,input,'input/source.mp4','input',undefined,signal);
    }
    await sendEvent('stage','Video ready',{status:'completed'},'download');
    }
    const requestPath = path.join(output,'.worker','request.json');
    await writeFile(requestPath,JSON.stringify({...assignment.request,scanId:assignment.scanId,attemptId:assignment.attemptId,inputPath:input,outputPath:output,resume:!!resumeState,selectiveResume:!!resumeState,inputSha256:assignment.input.sha256,archiveAck:true,backgroundArchive:true,...(assignment.runtimeVersions ? {runtimeVersions:assignment.runtimeVersions} : {})}),{mode:0o600});
    await dependencies.runPipeline({command:python,args:[runner,'--request',requestPath],parentGuard:path.join(pipelineRoot,'parent_guard.py'),cwd:pipelineRoot,logPath:path.join(job,'worker.log'),signal,onEvent:async event=> {
      if (event.type === 'restore') {
        if (typeof event.restoreId !== 'string' || !/^[a-zA-Z0-9_-]{1,100}$/.test(event.restoreId) || !Array.isArray(event.paths) || event.paths.some(p=>typeof p!=='string')) throw new Error('Invalid stage restore request');
        await sendEvent('progress','Restoring required stage inputs',{substep:'Downloading saved inputs'},event.stage);
        await restore(event.paths as string[]);
        await mkdir(path.join(output,'.worker','restored'),{recursive:true});
        await writeFile(path.join(output,'.worker','restored',event.restoreId),'verified',{mode:0o600});
        return;
      }
      if (event.type === 'checkpoint') {
        await mkdir(path.join(output,'logs'),{recursive:true});
        await copyFile(path.join(job,'worker.log'),path.join(output,'logs','worker.log'));
        await archives.enqueue(String(event.checkpointId),++checkpointSequence,event.stage); return;
      }
      if (event.type === 'failed' && typeof event.error === 'string') pipelineFailure = redactSecrets(event.error);
      const {type, runtimeVersions, message, ...progress} = event;
      if (type === 'completed') return;
      await sendEvent(type,redactSecrets(String(message ?? event.error ?? type)),progress,event.stage,runtimeVersions);
    }});
    await sendEvent('stage','Waiting for required uploads',{status:'running'},'finalizing');
    await archives.drain();
    await client.post('finish',{...fence,state:'completed'},signal);
    // All replacement outputs are canonical and completion is acknowledged.
    // Attempt history remains in the control plane; superseded local copies
    // from this scan no longer supply unique recovery data.
    await removeScratchPath(scratch,assignment.scanId);
  } catch (error) {
    // Cancellation can fence an event/upload before the next periodic heartbeat.
    // Confirm the stop command: other 409s (such as invalid artifacts) are failures.
    if (!signal.aborted && error instanceof ApiError && error.status === 409) {
      const heartbeat = await client.post<HeartbeatResponse>('heartbeat',fence,signal).catch(()=>null);
      if (heartbeat?.command === 'stop') {
        cancelled = heartbeat.reason === 'cancelled';
        error = new Error(cancelled ? 'Assignment cancelled by owner.' : 'Assignment fenced');
        abort.abort(error);
      }
    }
    archiveAbort.abort(error);
    await archives.drain().catch(()=>{});
    await events;
    let cleanupFailure: unknown;
    if (cancelled) {
      try {
        await removeScratchPath(scratch,assignment.scanId);
        await client.post('cleanup',fence);
      } catch (failure) { cleanupFailure = failure; }
    }
    if (!signal.aborted) await archiveFile(client,fence,path.join(job,'worker.log'),'logs/worker.log','log',undefined,signal).catch(()=>{});
    // A fenced worker cannot finalize: server rejects stale identities.
    await client.post('finish',{...fence,state:signal.aborted?'interrupted':'failed',error:cleanupFailure?'Cancelled; local cleanup pending.':cancelled?'Cancelled by owner.':pipelineFailure ?? redactSecrets(error instanceof Error?error.message:'Pipeline failed')}).catch(()=>{});
    throw error;
  } finally { clearInterval(watchdog); clearInterval(heartbeats); }
  return drain;
}

export async function main(shutdown = new AbortController()) {
  const enrollmentToken = process.env.WORKER_ENROLLMENT_TOKEN;
  delete process.env.WORKER_ENROLLMENT_TOKEN;
  const explicitEnrollment = process.argv.includes('enroll');
  if (explicitEnrollment) console.error('Checking worker GPU and engine dependencies...');
  const info = registration();
  if (process.argv.includes('--preflight')) { console.log(JSON.stringify(info)); return; }
  let credentialExists = true;
  try {await readFile(credentialFile);} catch(error) {if((error as NodeJS.ErrnoException).code==='ENOENT')credentialExists=false;else throw error;}
  if (explicitEnrollment || !credentialExists && enrollmentToken) {
    if (credentialExists) throw new Error('This worker already has saved credentials. Start the worker normally; use a different WORKER_CREDENTIAL_FILE to enroll a separate identity.');
    const origin = process.env.WORKER_CONTROL_URL;
    if (!origin) throw new Error('Set WORKER_CONTROL_URL to the running Guerrilla server before enrolling.');
    let token = enrollmentToken;
    if (!token && process.argv.includes('--token-stdin')) {
      console.error('Paste the enrollment token from Workers and press Enter:');
      token = await readEnrollmentToken();
    }
    if (!token) throw new Error('Run enroll --token-stdin and paste a token from Workers.');
    console.error('Enrolling worker with the server...');
    const enrolled = await new Client(origin,'').post<EnrollmentResponse>('enroll',{token,registration:info});
    await mkdir(path.dirname(credentialFile),{recursive:true,mode:0o700});
    await writeFile(credentialFile,JSON.stringify({origin,...enrolled}),{flag:'wx',mode:0o600});
    await chmod(credentialFile,0o600); delete process.env.WORKER_ENROLLMENT_TOKEN;
    console.log('Worker enrolled; credential saved outside checkout.'); if(explicitEnrollment)return;
  }
  process.once('SIGINT',()=>shutdown.abort()); process.once('SIGTERM',()=>shutdown.abort());
  process.on('message',message=> {if (message === 'local-shutdown' || (message as {type?:string})?.type === 'local-shutdown') shutdown.abort();});
  shutdown.signal.addEventListener('abort',()=> {if(process.connected) process.disconnect?.();},{once:true});
  let waiting = false, contents = '';
  while (!shutdown.signal.aborted) {
    try { contents = await readFile(credentialFile,'utf8'); break; }
    catch(error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
      if (!waiting) {console.log('Waiting for account enrollment. Run the agent enroll command to save credentials.');waiting=true;}
      await delay(3000,undefined,{signal:shutdown.signal}).catch(()=>{});
    }
  }
  if (shutdown.signal.aborted) return;
  const credentials = JSON.parse(contents);
  const client = new Client(credentials.origin,credentials.credential);
  const lockProcess = spawn(python,[path.join(pipelineRoot,'agent_lock.py'),agentLockPath(info.gpus[0]!.id),String(process.pid)],{windowsHide:true,stdio:['ignore','pipe','ignore']});
  await new Promise<void>((resolve,reject)=> {lockProcess.stdout.once('data',()=>resolve());lockProcess.once('error',reject);lockProcess.once('exit',()=>reject(new Error('Another agent owns this GPU')));});
  lockProcess.once('exit',()=>shutdown.abort());
  delete process.env.WORKER_ENROLLMENT_TOKEN;
  let clean = await scratchIsClean();
  while (!shutdown.signal.aborted) {
    try {
      const response = await client.post<PollResponse>('poll',{registration:info,clean},shutdown.signal);
      if (response.cleanup?.length) {
        for (const fence of response.cleanup) {
          await removeScratchPath(scratch,fence.scanId);
          await client.post('cleanup',fence,shutdown.signal);
          await client.post('finish',{...fence,state:'interrupted',error:'Cancelled by owner.'},shutdown.signal).catch(()=>{});
        }
        clean=await scratchIsClean();
        continue;
      }
      if (response.command !== 'continue') break;
      if (response.assignment) {
        clean=false;
        const drain=await execute(client,response.assignment,shutdown.signal);
        clean=await scratchIsClean();
        if(drain) {await client.post('poll',{registration:info,clean},shutdown.signal);break;}
      }
      await delay(Math.min(30000,Math.max(1000,response.pollAfterMs)),undefined,{signal:shutdown.signal});
    } catch (error) {
      if (shutdown.signal.aborted) break;
      clean=await scratchIsClean().catch(()=>false);
      console.error(redactSecrets(error instanceof Error?error.message:'Worker request failed'));
      await delay(5000,undefined,{signal:shutdown.signal}).catch(()=>{});
    }
  }
  lockProcess.kill();
}
