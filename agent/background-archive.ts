import { mkdir, writeFile, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import type { ArtifactManifest, Fence } from '@guerrilla/worker-protocol';
import { Client } from './http';
import { checkpoint } from './archive';
import { copyStageSnapshot, removeScratchPath } from './retention-files';

type Snapshot = { id: string; sequence: number; root: string; stages:string[] };
export type UploadStatus = 'queued' | 'uploading' | 'verifying' | 'retrying' | 'saved';
export type UploadProgress = {checkpointId:string; checkpointSequence:number; stages:string[]; current:number; total:number};
const checkpointStages = new Set(['selection','reconstruction','densification','training','export']);

/** Freeze locally before releasing the runner; only remote commit is asynchronous.
 * One active and one latest pending snapshot bound the backlog on slow networks.
 * Every newer snapshot includes all completed stages, so pending work can coalesce.
 */
export class BackgroundArchive {
  private pending?: Snapshot;
  private running?: Promise<void>;
  private failure?: unknown;
  private savedStages = new Set<string>();
  constructor(private client: Client, private fence: Fence, private output: string,
    private signal: AbortSignal,
    private report: (status: UploadStatus, progress:UploadProgress) => Promise<void>,
    private retryMs = 5000,
    private inherited: ArtifactManifest[] = []) {}

  async enqueue(id: string, sequence: number, stage?:string) {
    if (!/^[a-zA-Z0-9_-]{1,100}$/.test(id)) throw new Error('Invalid checkpoint ID');
    this.signal.throwIfAborted();
    const root = join(this.output, '.worker', 'archives', id);
    await mkdir(root, {recursive:true});
    await copyStageSnapshot(this.output,root,new Set(this.inherited.map(file=>file.relativePath)));
    this.signal.throwIfAborted();
    const state = JSON.parse(await readFile(join(root,'platform-state.json'),'utf8'));
    const stages = [...new Set([...Object.keys(state.completed ?? {}), ...(stage ? [stage] : [])])]
      .filter(key=>checkpointStages.has(key) && (!this.savedStages.has(key) || key===stage));
    const previous = this.pending;
    this.pending = {id, sequence, root, stages};
    await this.report('queued',{checkpointId:id,checkpointSequence:sequence,stages,current:0,total:0});
    if (previous) await removeScratchPath(join(this.output,'.worker','archives'), previous.id);
    const ready = join(this.output,'.archive-acks');
    await mkdir(ready,{recursive:true});
    await writeFile(join(ready,`${id}.ready`),'snapshot ready',{mode:0o600});
    if (!this.running) {
      this.running = this.work().catch(error => { this.failure = error; }).finally(() => { this.running = undefined; });
    }
  }

  private async work() {
    for (;;) {
      if (!this.pending) return;
      const snapshot = this.pending;
      this.pending = undefined;
      const cache = new Map<string, {clientId:string; artifactId?:string}>();
      let failures = 0;
      const progress:UploadProgress = {checkpointId:snapshot.id,checkpointSequence:snapshot.sequence,stages:snapshot.stages,current:0,total:0};
      await this.report('uploading',progress);
      for (;;) {
        this.signal.throwIfAborted();
        try {
          await checkpoint(this.client,this.fence,snapshot.root,snapshot.id,this.signal,snapshot.sequence,cache,async update=> {
            progress.current=update.current;progress.total=update.total;
            await this.report(update.status,progress);
          },this.inherited);
          break;
        } catch {
          this.signal.throwIfAborted();
          await this.report('retrying',progress);
          await delay(Math.min(60000, this.retryMs * 2 ** Math.min(failures++,4)),undefined,{signal:this.signal});
        }
      }
      for (const stage of snapshot.stages) this.savedStages.add(stage);
      await this.report('saved',progress);
      await writeFile(join(this.output,'.archive-acks',snapshot.id),'committed',{mode:0o600});
      await removeScratchPath(join(this.output,'.worker','archives'),snapshot.id);
    }
  }

  async drain() {
    // An enqueue can arrive while the prior worker is publishing its last status.
    do {
      if (this.running) await this.running;
      if (this.failure) throw this.failure;
      if (this.pending) this.running = this.work().catch(error => {this.failure=error;}).finally(()=>{this.running=undefined;});
    } while (this.running || this.pending);
    this.signal.throwIfAborted();
  }
}
