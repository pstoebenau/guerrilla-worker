<script lang="ts">
  import { onMount } from 'svelte';
  import { fade } from 'svelte/transition';
  import { listen } from '@tauri-apps/api/event';
  import { api, nativeAvailable } from './lib/native';
  import { parseStatus } from './lib/parse';
  import { isRunning, progressPercent, statusTitle } from './lib/types';
  import type { BrowseField, DesktopInfo, Preflight, Profile, WorkerAction, WorkerStatus } from './lib/types';

  type Page = 'overview' | 'setup' | 'logs';
  let page = $state<Page>('overview');
  let info = $state<DesktopInfo | null>(null);
  let profile = $state<Profile | null>(null);
  let status = $state<WorkerStatus | null>(null);
  let check = $state<Preflight | null>(null);
  let error = $state('');
  let notice = $state('');
  let busy = $state('');
  let token = $state('');
  let logs = $state<string[]>([]);
  let logsLoaded = $state(false);
  let disconnectOpen = $state(false);
  let stopOpen = $state(false);
  let refreshing = false;
  let alive = true;
  const running = $derived(isRunning(status));
  const unavailable = $derived(!nativeAvailable || !info?.runtimeAvailable);
  const percent = $derived(progressPercent(status?.progress));
  const displayEngines = $derived(check?.engines ?? status?.engines ?? []);
  const displayGpus = $derived(check?.gpus ?? status?.gpus ?? []);
  const title = $derived(statusTitle(status, nativeAvailable));

  async function refresh() {
    if (!nativeAvailable || refreshing || busy) return;
    refreshing = true;
    try {
      const next = await api.status();
      if (alive) { status = next; if (info) info.enrolled = !!next.workerId; }
      if (page === 'logs') { const result = await api.logs(); if (alive) { logs = result.lines.slice(-200); logsLoaded = true; } }
    } catch (e) { if (alive) error = String(e); }
    finally { refreshing = false; }
  }
  async function perform(label: string, work: () => Promise<void>) {
    if (busy) return;
    busy = label; error = ''; notice = '';
    try { await work(); }
    catch (e) { error = String(e); }
    finally { busy = ''; }
  }
  async function reloadInfo() { info = await api.info(); profile = { ...info.profile }; }
  async function save() { if (profile) { info = await api.save(profile); profile = { ...info.profile }; check = null; } }
  async function doAction(action: WorkerAction) {
    stopOpen = false; disconnectOpen = false;
    await perform(action === 'drain' ? 'Pausing after current job…' : `${action[0]!.toUpperCase()}${action.slice(1)}…`, async () => {
      status = await api.action(action);
      if (action === 'disconnect') { await reloadInfo(); notice = 'Disconnected. Saved checkpoints remain on this computer.'; }
    });
  }
  async function browse(field: BrowseField) {
    await perform('Selecting path…', async () => { const selected = await api.browse(field); if (selected && profile) profile[field] = selected; });
  }
  async function selectPage(next: Page) { page = next; token = ''; if (next === 'logs') await refresh(); }
  function showDialog(node: HTMLDialogElement) { node.showModal(); return { destroy: () => node.close() }; }
  function engineName(name: string) { return name.toLowerCase() === 'lichtfeld' ? 'LichtFeld' : name.toLowerCase() === 'spirula' ? 'Spirula' : name; }
  function stageName(stage?: string) { return stage ? stage.replace(/[_-]/g, ' ').replace(/^./, c => c.toUpperCase()) : 'Waiting for a job'; }

  onMount(() => {
    alive = true;
    const unlisteners: (() => void)[] = [];
    if (nativeAvailable) {
      void perform('Loading worker…', async () => { await reloadInfo(); status = await api.status(); if (!info?.enrolled) page = 'setup'; });
      void listen<unknown>('worker-status', event => { try { status = parseStatus(event.payload); } catch (e) { error = String(e); } }).then(unlisten => alive ? unlisteners.push(unlisten) : unlisten());
      void listen<string>('worker-error', event => { error = event.payload; }).then(unlisten => alive ? unlisteners.push(unlisten) : unlisten());
      void listen('window-hidden', () => { token = ''; }).then(unlisten => alive ? unlisteners.push(unlisten) : unlisten());
    }
    const timer = setInterval(() => { void refresh(); }, 3000);
    return () => { alive = false; clearInterval(timer); unlisteners.forEach(unlisten => unlisten()); };
  });
</script>

<div class="app-shell">
  <aside class="sidebar">
    <div class="brand"><span class="brand-mark" aria-hidden="true">g</span><div><strong>guerrilla</strong><span>WORKER</span></div></div>
    <nav aria-label="Worker navigation">
      {#each [{ id: 'overview', title: 'Overview', icon: '◉' }, { id: 'setup', title: 'Setup', icon: '⚙' }, { id: 'logs', title: 'Logs', icon: '≡' }] as item}
        <button class:active={page === item.id} aria-current={page === item.id ? 'page' : undefined} onclick={() => selectPage(item.id as Page)}><span aria-hidden="true">{item.icon}</span>{item.title}</button>
      {/each}
    </nav>
    <div class="sidebar-bottom">
      <div class="sidebar-status"><span class="dot" class:online={status?.connected}></span>{nativeAvailable ? status?.connected ? 'Connected to server' : 'Not connected' : 'Browser preview'}</div>
      <button class="sidebar-link" disabled={!nativeAvailable || !!busy} onclick={() => perform('Opening server…', api.openServer)}>Open server <span aria-hidden="true">↗</span></button>
      <span class="version">{info ? `v${info.version}` : 'Guerrilla Worker'}</span>
    </div>
  </aside>

  <main>
    <header class="page-header"><div><p class="eyebrow">THIS COMPUTER</p><h1>{page === 'overview' ? 'Worker overview' : page === 'setup' ? 'Worker setup' : 'Worker logs'}</h1></div><div class="status-label"><span class="dot" class:online={status?.connected}></span>{nativeAvailable ? status?.state ?? 'Loading' : 'Preview'}</div></header>

    {#if !nativeAvailable}
      <div class="banner" role="status"><strong>Desktop runtime required</strong><p>This browser preview cannot access your GPU, installed engines, or background worker. Open the desktop app to connect and manage this computer.</p></div>
    {:else if info && !info.runtimeAvailable}
      <div class="banner" role="alert"><strong>Worker runtime is incomplete</strong><p>Reinstall the desktop app, or prepare the bundled runtime when developing from source.</p></div>
    {/if}
    {#if error}<div class="alert error" role="alert"><span>{error}</span><button aria-label="Dismiss error" onclick={() => error = ''}>×</button></div>{/if}
    {#if notice}<div class="alert" role="status"><span>{notice}</span><button aria-label="Dismiss notification" onclick={() => notice = ''}>×</button></div>{/if}

    {#key page}
      <div class="page-content" in:fade={{ duration: 140 }}>
        {#if page === 'overview'}
          <section class="worker-summary" aria-labelledby="worker-state">
            <p class="eyebrow">WORKER STATUS</p><h2 id="worker-state">{title}</h2>
            <p class="summary-copy">{status?.message ?? (nativeAvailable ? 'Jobs and training settings are assigned by your Guerrilla server.' : 'Connect this computer from the installed desktop app.')}</p>
            <div class="actions">
              {#if running}<button class="primary" disabled={!!busy || unavailable} onclick={() => doAction('drain')}>{status?.state === 'draining' ? 'Pausing after this job' : 'Finish job, then pause'}</button><button disabled={!!busy || unavailable} onclick={() => status?.state === 'busy' || status?.state === 'draining' ? stopOpen = true : doAction('stop')}>Stop worker</button>
              {:else if info?.enrolled}<button class="primary" disabled={!!busy || unavailable} onclick={() => doAction('start')}>Start worker</button>
              {:else}<button class="primary" onclick={() => selectPage('setup')}>Set up worker <span aria-hidden="true">→</span></button>{/if}
              {#if busy}<span class="pending" role="status">{busy}</span>{/if}
            </div>
          </section>
          <section class="job-section" aria-labelledby="job-title"><div class="section-heading"><h3 id="job-title">Current job</h3><span>{status?.backend ? engineName(status.backend) : 'Server assigned'}</span></div>
            {#if status?.stage || status?.jobId}
              <div class="job-title-row"><strong>{stageName(status.stage)}</strong><span>{percent === null ? 'In progress' : `${percent}%`}</span></div>
              <div class="progress-track" role="progressbar" aria-label="Current stage progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent ?? undefined} class:indeterminate={percent === null}><div style:width={percent === null ? '30%' : `${percent}%`}></div></div>
              {#if status.jobId}<p class="job-id">Job {status.jobId}</p>{/if}
            {:else}<div class="empty-job"><span class="queue-glyph" aria-hidden="true">↳</span><div><strong>{running ? 'Waiting for the next job' : 'No active job'}</strong><p>{running ? 'The worker will pick up compatible jobs from the server.' : 'Start an enrolled worker to receive jobs.'}</p></div></div>{/if}
          </section>
          <div class="machine-columns"><section><div class="section-heading"><h3>GPU</h3></div>{#if displayGpus.length}{#each displayGpus as gpu}<div class="resource-row"><strong>{gpu.name}</strong><span>{gpu.memoryMb ? `${(gpu.memoryMb / 1024).toFixed(1)} GB VRAM` : 'Detected'}</span></div>{/each}{:else}<p class="muted">{check ? 'No compatible GPU detected.' : 'Run a dependency check to inspect available GPUs.'}</p>{/if}</section>
          <section><div class="section-heading"><h3>Engines</h3><button class="text-button" disabled={!!busy || unavailable} onclick={() => perform('Checking dependencies…', async () => { check = await api.preflight(); })}>Check</button></div>{#if displayEngines.length}{#each displayEngines as engine}<div class="resource-row"><strong>{engineName(engine.name)}</strong><span class:ready={engine.available}>{engine.available ? 'Available' : 'Unavailable'}</span></div>{/each}{:else}<p class="muted">Use your own LichtFeld or Spirula installation.</p>{/if}</section></div>
          <footer class="workspace-footer"><span>Closing this window keeps the worker running.</span><button class="text-button" disabled={!nativeAvailable || !!busy} onclick={() => perform('Quitting interface…', api.quit)}>Quit interface</button></footer>

        {:else if page === 'setup'}
          {#if profile}
            <form onsubmit={(e) => { e.preventDefault(); void perform('Saving settings…', async () => { await save(); notice = 'Settings saved.'; }); }}>
              <section class="settings-section"><div class="section-heading"><h3>Connection</h3>{#if info?.enrolled}<span class="ready">Enrolled</span>{/if}</div><label for="server">Server URL</label><input id="server" type="url" bind:value={profile.controlUrl} disabled={!!busy || running || info?.enrolled} required placeholder="https://guerrilla.dad"/><p class="field-help">{info?.enrolled ? 'Enrollment belongs to this server. Stop and disconnect before changing it.' : 'Use the server that will assign jobs to this computer.'}</p></section>
              <section class="settings-section"><div class="section-heading"><div><h3>Installed engines</h3><p>Install LichtFeld or Spirula yourself, then select its executable.</p></div><button type="button" class="text-button" disabled={!!busy || running} onclick={() => perform('Detecting engines…', async () => { const found = await api.detect(); if (profile) { for (const key of ['lichtfeldPath', 'spirulaPath', 'densificationPluginPath'] as const) if (found[key]) profile[key] = found[key]; } notice = 'Detection complete. Review the engine paths below.'; })}>Detect</button></div>
                {#each [{ field: 'lichtfeldPath', label: 'LichtFeld executable', optional: false }, { field: 'spirulaPath', label: 'Spirula executable', optional: false }, { field: 'densificationPluginPath', label: 'LichtFeld densification folder', optional: true }] as setting}
                  <div class="path-field"><label for={setting.field}>{setting.label}{setting.optional ? ' (optional)' : ''}</label><div><input id={setting.field} bind:value={profile[setting.field as BrowseField]} disabled={!!busy || running} placeholder="Select an existing installation"/><button type="button" disabled={!!busy || running} onclick={() => browse(setting.field as BrowseField)}>Browse…</button></div></div>
                {/each}
              </section>
              <section class="settings-section"><div class="section-heading"><h3>Storage and startup</h3></div><div class="path-field"><label for="scratch">Scratch folder</label><div><input id="scratch" bind:value={profile.scratchDirectory} disabled={!!busy || running} required/><button type="button" disabled={!!busy || running} onclick={() => browse('scratchDirectory')}>Browse…</button></div><p class="field-help">Working files and retained checkpoints use this folder.</p></div><label class="checkbox-label"><input type="checkbox" bind:checked={profile.startAtLogin} disabled={!!busy || running}/><span>Start worker when I log in</span></label><p class="field-help">The worker runs under your account. Your GPU drivers and engines come from your existing installations; the app supplies its other dependencies.</p></section>
              <div class="actions"><button type="submit" class="primary" disabled={!!busy || running}>Save settings</button><button type="button" disabled={!!busy || running || unavailable} onclick={() => perform('Checking dependencies…', async () => { await save(); check = await api.preflight(); notice = check.ok ? 'Dependency check complete. A supported engine is available.' : 'Dependency check needs attention. Review the results below.'; })}>Save and check dependencies</button>{#if running}<span class="muted">Stop the worker to change settings.</span>{/if}</div>
            </form>
            {#if check}<section class="check-results"><h3>Dependency check</h3>{#if check.message}<p>{check.message}</p>{/if}{#each [...(check.checks ?? []), ...(check.engines ?? [])] as result}<div class="resource-row"><div><strong>{engineName(result.name)}</strong>{#if result.message}<p>{result.message}</p>{/if}</div><span class:ready={result.available}>{result.available ? 'Available' : 'Needs attention'}</span></div>{/each}{#if !check.gpus?.length}<p class="muted">No compatible GPU was detected.</p>{/if}</section>{/if}
            <section class="settings-section enrollment"><div class="section-heading"><h3>Enrollment</h3></div>{#if info?.enrolled}<p class="muted">Worker {status?.workerId ?? 'identity saved'}. Disconnect removes this computer’s enrollment and keeps checkpoints.</p><button disabled={!!busy || running} onclick={() => disconnectOpen = true}>Disconnect worker…</button>{:else}<label for="token">Enrollment token</label><input id="token" type="password" autocomplete="off" spellcheck="false" maxlength="4096" bind:value={token} disabled={!!busy || unavailable} placeholder="Paste a token from the server’s Workers page"/><p class="field-help">The token is used once for enrollment and is never saved in settings.</p><div class="actions"><button class="primary" disabled={!!busy || unavailable || !token.trim()} onclick={() => { const secret = token; token = ''; void perform('Enrolling worker…', async () => { await save(); status = await api.enroll(secret); await reloadInfo(); page = 'overview'; }); }}>Enroll and start</button><button disabled={!!busy || !nativeAvailable} onclick={() => perform('Opening server…', api.openServer)}>Open server ↗</button></div>{/if}</section>
          {:else}<section class="setup-unavailable"><h2>Connect from the desktop app</h2><p>Choose your server, locate your installed engines, and enroll this computer using a token from the server’s Workers page.</p><p class="muted">Python, FFmpeg, Node, and the export converter are supplied with the worker distribution.</p></section>{/if}

        {:else}
          <div class="section-heading"><div><h3>Recent worker output</h3><p>Last 200 lines. Enrollment credentials are removed from worker logs.</p></div><button disabled={!!busy || !nativeAvailable} onclick={() => perform('Refreshing logs…', async () => { const result = await api.logs(); logs = result.lines.slice(-200); logsLoaded = true; })}>Refresh</button></div>
          <pre class="log-output" aria-label="Worker log output">{logs.length ? logs.join('\n') : logsLoaded ? 'No worker output yet.' : nativeAvailable ? 'Loading worker logs…' : 'Worker logs are available in the desktop app.'}</pre>
          <div class="workspace-footer"><span>{info?.profileDirectory ?? 'Logs stay on this computer.'}</span><button class="text-button" disabled={!nativeAvailable || !!busy} onclick={() => perform('Opening data folder…', api.openData)}>Open data folder ↗</button></div>
        {/if}
      </div>
    {/key}
  </main>
</div>

{#if disconnectOpen || stopOpen}
  <dialog class="modal" aria-labelledby="confirm-title" use:showDialog oncancel={() => { disconnectOpen = false; stopOpen = false; }}><p class="eyebrow">WORKER CONTROL</p><h2 id="confirm-title">{disconnectOpen ? 'Disconnect this worker?' : 'Stop the current job?'}</h2><p>{disconnectOpen ? 'Saved enrollment will be removed. Checkpoints stay on this computer. You can revoke the old identity on the server’s Workers page.' : 'Execution will stop using the worker’s shutdown process. Verified checkpoints remain available for recovery. To finish this job first, choose pause instead.'}</p><div class="actions"><button class="primary" onclick={() => doAction(disconnectOpen ? 'disconnect' : 'stop')}>{disconnectOpen ? 'Disconnect' : 'Stop worker'}</button><button onclick={() => { disconnectOpen = false; stopOpen = false; }}>Cancel</button>{#if stopOpen}<button onclick={() => doAction('drain')}>Finish job, then pause</button>{/if}</div></dialog>
{/if}
