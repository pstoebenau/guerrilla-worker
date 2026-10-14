<script lang="ts">
  import { onMount } from 'svelte';
  import { fly } from 'svelte/transition';
  import Icon from './components/Icon.svelte';
  import type { IconName } from './components/Icon.svelte';
  import { listen } from '@tauri-apps/api/event';
  import { api, nativeAvailable } from './lib/native';
  import { parseStatus } from './lib/parse';
  import { isRunning, progressPercent, statusTitle } from './lib/types';
  import type { AvailableUpdate, BrowseField, DesktopInfo, Preflight, Profile, WorkerAction, WorkerStatus } from './lib/types';

  type Page = 'overview' | 'setup' | 'logs';
  let page = $state<Page>('overview');
  type Theme = 'light' | 'system' | 'dark';
  let theme = $state<Theme>('system');
  let reducedMotion = $state(false);
  function setTheme(next: Theme) {
    theme = next;
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('guerrilla-worker-theme', next); } catch { /* Storage is optional. */ }
  }
  let info = $state<DesktopInfo | null>(null);
  let profile = $state<Profile | null>(null);
  let status = $state<WorkerStatus | null>(null);
  let check = $state<Preflight | null>(null);
  let error = $state('');
  let notice = $state('');
  let busy = $state('');
  let installingRuntime = $state(false);
  let token = $state('');
  let logs = $state<string[]>([]);
  let logsLoaded = $state(false);
  let disconnectOpen = $state(false);
  let stopOpen = $state(false);
  let advancedOpen = $state(false);
  let availableUpdate = $state<AvailableUpdate | null>(null);
  let dismissedVersion = '';
  let checkingUpdates = false;
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
  async function checkForUpdates() {
    if (!nativeAvailable || checkingUpdates) return;
    checkingUpdates = true;
    try {
      const update = await api.checkForUpdates();
      if (alive) availableUpdate = update?.version === dismissedVersion ? null : update;
    } catch { /* Offline or rate-limited checks should not interrupt worker management. */ }
    finally { checkingUpdates = false; }
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
  async function selectPage(next: Page) { page = next; token = ''; document.getElementById('main-content')?.scrollTo({ top: 0 }); if (next === 'logs') await refresh(); }
  async function installRuntime() {
    await perform('Installing GPU dependencies…', async () => {
      installingRuntime = true;
      try {
        info = await api.installGpuRuntime();
        if (profile) profile.pythonPath = info.profile.pythonPath;
        check = null;
        notice = 'GPU dependencies are installed and verified. Save and check dependencies to test your selected engine.';
      } finally { installingRuntime = false; }
    });
  }
  function showDialog(node: HTMLDialogElement) { node.showModal(); return { destroy: () => node.close() }; }
  function engineName(name: string) { return name.toLowerCase() === 'lichtfeld' ? 'LichtFeld' : name.toLowerCase() === 'spirula' ? 'Spirula' : name; }
  function stageName(stage?: string) { return stage ? stage.replace(/[_-]/g, ' ').replace(/^./, c => c.toUpperCase()) : 'Waiting for a job'; }

  onMount(() => {
    alive = true;
    try {
      const saved = localStorage.getItem('guerrilla-worker-theme');
      if (saved === 'light' || saved === 'dark' || saved === 'system') setTheme(saved);
    } catch { /* Use the system theme if storage is unavailable. */ }
    const motion = matchMedia('(prefers-reduced-motion: reduce)');
    const updateMotion = () => { reducedMotion = motion.matches; };
    updateMotion();
    motion.addEventListener('change', updateMotion);
    const unlisteners: (() => void)[] = [];
    if (nativeAvailable) {
      void checkForUpdates();
      void perform('Loading worker…', async () => { await reloadInfo(); status = await api.status(); if (!info?.enrolled) page = 'setup'; });
      void listen<unknown>('worker-status', event => { try { status = parseStatus(event.payload); } catch (e) { error = String(e); } }).then(unlisten => alive ? unlisteners.push(unlisten) : unlisten());
      void listen<string>('worker-error', event => { error = event.payload; }).then(unlisten => alive ? unlisteners.push(unlisten) : unlisten());
      void listen<{ message: string; downloadedBytes?: number; totalBytes?: number }>('runtime-setup-progress', event => {
        if (!installingRuntime || typeof event.payload.message !== 'string') return;
        const downloaded = event.payload.downloadedBytes ?? 0, total = event.payload.totalBytes ?? 0;
        busy = event.payload.message.slice(0, 300) + (total > 0 ? ` (${Math.min(100, Math.round(downloaded / total * 100))}%)` : '…');
      }).then(unlisten => alive ? unlisteners.push(unlisten) : unlisten());
      void listen('window-hidden', () => { token = ''; }).then(unlisten => alive ? unlisteners.push(unlisten) : unlisten());
    }
    const timer = setInterval(() => { void refresh(); }, 3000);
    const updateTimer = setInterval(() => { void checkForUpdates(); }, 6 * 60 * 60 * 1000);
    return () => { alive = false; motion.removeEventListener('change', updateMotion); clearInterval(timer); clearInterval(updateTimer); unlisteners.forEach(unlisten => unlisten()); };
  });
</script>

<a class="skip-link" href="#main-content">Skip to content</a>
<div class="app-shell">
  <header class="app-header">
    <div class="brand" aria-label="Guerrilla Worker"><span class="brand-logo" aria-hidden="true"></span><span>Worker</span></div>
    <div class="connection"><span class="dot" class:online={status?.connected}></span><span>{nativeAvailable ? status?.connected ? 'Connected' : 'Not connected' : 'Browser preview'}</span></div>
  </header>
  <nav aria-label="Worker navigation">
    {#each [{ id: 'overview', title: 'Overview', icon: 'overview' }, { id: 'setup', title: 'Setup', icon: 'settings' }, { id: 'logs', title: 'Logs', icon: 'logs' }] as item}
      <button class:active={page === item.id} aria-current={page === item.id ? 'page' : undefined} onclick={() => selectPage(item.id as Page)}><Icon name={item.icon as IconName} size={15} />{item.title}</button>
    {/each}
    <button class="server-link" disabled={!nativeAvailable || !!busy} onclick={() => perform('Opening server…', api.openServer)}>Open server <Icon name="external" size={13} /></button>
  </nav>
  <main id="main-content" tabindex="-1" aria-label={page === 'overview' ? 'Worker overview' : page === 'setup' ? 'Worker setup' : 'Worker logs'}>
    {#if !nativeAvailable}
      <div class="banner" role="status"><p>Preview only. Open the desktop app to manage this worker.</p></div>
    {:else if info && !info.runtimeAvailable}
      <div class="banner" role="alert"><strong>Worker runtime is incomplete</strong><p>Reinstall the desktop app, or prepare the bundled runtime when developing from source.</p></div>
    {/if}
    {#if error}<div class="alert error" role="alert"><span>{error}</span><button aria-label="Dismiss error" onclick={() => error = ''}>×</button></div>{/if}
    {#if notice}<div class="alert" role="status"><span>{notice}</span><button aria-label="Dismiss notification" onclick={() => notice = ''}>×</button></div>{/if}
    {#if availableUpdate}
      <div class="update-notice" role="status">
        <span>Version {availableUpdate.version} available</span>
        <button class="text-button" disabled={!!busy} onclick={() => perform('Opening release…', () => api.openRelease(availableUpdate!.releaseUrl))}>View release <Icon name="external" size={12} /></button>
        <button class="dismiss-update" aria-label="Dismiss update notification" onclick={() => { dismissedVersion = availableUpdate!.version; availableUpdate = null; }}>×</button>
      </div>
    {/if}

    {#key page}
      <div class="page-content" class:logs-page={page === 'logs'} in:fly={{ y: 2, duration: reducedMotion ? 0 : 100 }}>
        {#if page === 'overview'}
          <section class="worker-summary" class:is-running={running} aria-labelledby="worker-state"><div class="worker-summary-body">
            <h1 id="worker-state"><span class="dot" class:online={running}></span>{title}</h1>
            {#if status?.message}<p class="summary-copy">{status.message}</p>{/if}
            </div>
            <div class="actions">
              {#if running}<button class="primary" disabled={!!busy || unavailable} onclick={() => doAction('drain')}>{status?.state === 'draining' ? 'Pausing after this job' : 'Finish job, then pause'}</button><button disabled={!!busy || unavailable} onclick={() => status?.state === 'busy' || status?.state === 'draining' ? stopOpen = true : doAction('stop')}>Stop worker</button>
              {:else if info?.enrolled}<button class="primary" disabled={!!busy || unavailable} onclick={() => doAction('start')}>Start worker</button>
              {:else}<button class="primary" onclick={() => selectPage('setup')}>Set up worker <Icon name="arrow" size={16} /></button>{/if}

            </div>

          </section>
          <section class="job-section" aria-labelledby="job-title"><div class="section-heading"><h3 id="job-title">Current job</h3>{#if status?.backend}<span>{engineName(status.backend)}</span>{/if}</div>
            {#if status?.stage || status?.jobId}
              <div class="job-title-row"><strong>{stageName(status.stage)}</strong><span>{percent === null ? 'In progress' : `${percent}%`}</span></div>
              <div class="progress-track" role="progressbar" aria-label="Current stage progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent ?? undefined} class:indeterminate={percent === null}><div style:width={percent === null ? '30%' : `${percent}%`}></div></div>
              {#if status.jobId}<p class="job-id">Job {status.jobId}</p>{/if}
            {:else}<div class="empty-job"><div><strong>{running ? 'Waiting for the next job' : 'No active job'}</strong></div></div>{/if}
          </section>
          <div class="machine-columns"><section><div class="section-heading"><h3><Icon name="gpu" size={17} />Graphics</h3></div>{#if displayGpus.length}{#each displayGpus as gpu}<div class="resource-row"><strong>{gpu.name}</strong><span>{gpu.memoryMb ? `${(gpu.memoryMb / 1024).toFixed(1)} GB VRAM` : 'Detected'}</span></div>{/each}{:else}<p class="muted">{check ? 'No compatible GPU detected.' : 'Not checked yet.'}</p>{/if}</section>
          <section><div class="section-heading"><h3><Icon name="engine" size={17} />Engines</h3><button class="text-button" disabled={!!busy || unavailable} onclick={() => perform('Checking dependencies…', async () => { check = await api.preflight(); })}>Check</button></div>{#if displayEngines.length}{#each displayEngines as engine}<div class="resource-row"><strong>{engineName(engine.name)}</strong><span class:ready={engine.available}>{engine.available ? 'Available' : 'Unavailable'}</span></div>{/each}{:else}<p class="muted">Not checked yet.</p>{/if}</section></div>
          <footer class="workspace-footer"><span>Jobs keep running while the app is in the tray.</span><button class="text-button" disabled={!nativeAvailable || !!busy} onclick={() => perform('Stopping jobs and quitting…', api.quit)}>Quit and stop all jobs</button></footer>

        {:else if page === 'setup'}
          {#if profile}
            <section class="settings-section enrollment"><div class="section-heading"><h3>Enrollment</h3>{#if info?.enrolled}<span class="ready">Enrolled</span>{/if}</div><p class="field-help">Server: {profile.controlUrl}</p>{#if info?.enrolled}<p class="muted">Worker {status?.workerId ?? 'identity saved'}. Disconnect removes this computer’s enrollment and keeps checkpoints.</p><button disabled={!!busy || running} onclick={() => disconnectOpen = true}>Disconnect worker…</button>{:else}<label for="token">Enrollment token</label><input id="token" type="password" autocomplete="off" spellcheck="false" maxlength="4096" bind:value={token} disabled={!!busy || unavailable} placeholder="Paste a token from the server’s Workers page"/><p class="field-help">The token is used once for enrollment and is never saved in settings.</p><div class="actions"><button class="primary" disabled={!!busy || unavailable || !token.trim()} onclick={() => { const secret = token; token = ''; void perform('Enrolling worker…', async () => { await save(); status = await api.enroll(secret); await reloadInfo(); await selectPage('overview'); }); }}>Enroll and start</button><button disabled={!!busy || !nativeAvailable} onclick={() => perform('Opening server…', api.openServer)}>Open server ↗</button></div>{/if}</section>
            <form onsubmit={(e) => { e.preventDefault(); void perform('Saving settings…', async () => { await save(); notice = 'Settings saved.'; }); }}>

              {#if info?.gpuRuntime.supported}
                <section class="settings-section"><div class="section-heading"><div><h3>GPU dependencies</h3><p>{info.gpuRuntime.installed ? 'GPU libraries and reconstruction models are installed on this computer.' : 'Install GPU libraries and reconstruction models for NVIDIA scanning. Downloads need internet and at least 27 GB of free disk space.'}</p></div></div><div class="actions"><button type="button" disabled={!!busy || running || unavailable} onclick={installRuntime}>{info.gpuRuntime.installed ? 'Verify installation' : 'Install GPU dependencies'}</button><span class="muted">{info.gpuRuntime.installed ? 'Installed and verified' : 'One-time setup · several GB'}</span></div></section>
              {/if}

              <section class="settings-section"><div class="section-heading"><div><h3>Installed engines</h3><p>Select an installed engine or detect it automatically.</p></div><button type="button" class="text-button" disabled={!!busy || running} onclick={() => perform('Detecting engines…', async () => { const found = await api.detect(); if (profile) { for (const key of ['lichtfeldPath', 'spirulaPath', 'densificationPluginPath'] as const) if (found[key]) profile[key] = found[key]; } notice = 'Detection complete. Review the engine paths below.'; })}>Detect</button></div>
                {#each [{ field: 'lichtfeldPath', label: 'LichtFeld executable', optional: false }, { field: 'spirulaPath', label: 'Spirula executable', optional: false }, { field: 'densificationPluginPath', label: 'LichtFeld densification folder', optional: true }] as setting}
                  <div class="path-field"><label for={setting.field}>{setting.label}{setting.optional ? ' (optional)' : ''}</label><div><input id={setting.field} bind:value={profile[setting.field as BrowseField]} disabled={!!busy || running} placeholder="Select an existing installation"/><button type="button" disabled={!!busy || running} onclick={() => browse(setting.field as BrowseField)}>Browse…</button></div></div>
                {/each}
              </section>
              <section class="settings-section"><div class="section-heading"><h3>Startup</h3></div><label class="checkbox-label"><input type="checkbox" bind:checked={profile.startAtLogin} disabled={!!busy || running}/><span>Start worker when I log in</span></label></section>
            {#if check}<section class="check-results"><h3>Dependency check</h3>{#if check.message}<p>{check.message}</p>{/if}{#each [...(check.checks ?? []), ...(check.engines ?? [])] as result}<div class="resource-row"><div><strong>{engineName(result.name)}</strong>{#if result.message}<p>{result.message}</p>{/if}</div><span class:ready={result.available}>{result.available ? 'Available' : 'Needs attention'}</span></div>{/each}{#if !check.gpus?.length}<p class="muted">No compatible GPU was detected.</p>{/if}</section>{/if}
              <details class="advanced-settings" bind:open={advancedOpen}>
                <summary>Advanced <span>Server and storage</span></summary>
              <div class="advanced-field"><label for="server">Server URL</label><input id="server" type="url" oninvalid={() => advancedOpen = true} bind:value={profile.controlUrl} disabled={!!busy || running || info?.enrolled} required placeholder="https://guerrilla.dad"/><p class="field-help">{info?.enrolled ? 'Stop and disconnect to change servers.' : 'The server that assigns jobs to this worker.'}</p></div>
                <div class="path-field"><label for="scratch">Scratch folder</label><div><input id="scratch" bind:value={profile.scratchDirectory} disabled={!!busy || running} required oninvalid={() => advancedOpen = true}/><button type="button" disabled={!!busy || running} onclick={() => browse('scratchDirectory')}>Browse…</button></div><p class="field-help">Working files and retained checkpoints. Leave this at its default unless you need a different disk.</p></div>
              </details>

              <div class="actions save-actions"><button type="submit" class="primary" disabled={!!busy || running}>Save settings</button><button type="button" disabled={!!busy || running || unavailable} onclick={() => perform('Checking dependencies…', async () => { await save(); check = await api.preflight(); notice = check.ok ? 'Dependency check complete. A supported engine is available.' : 'Dependency check needs attention. Review the results below.'; })}>Save and check dependencies</button>{#if running}<span class="muted">Stop the worker to change settings.</span>{/if}</div>
            </form>


          {:else}<section class="setup-unavailable"><h2>Setup requires the desktop app</h2><p>Choose a server, select your engines, and paste an enrollment token there.</p></section>{/if}

        {:else}
          <div class="section-heading"><h3>Worker logs <span class="log-count">{logs.length} / 200 lines</span></h3><button disabled={!!busy || !nativeAvailable} onclick={() => perform('Refreshing logs…', async () => { const result = await api.logs(); logs = result.lines.slice(-200); logsLoaded = true; })}>Refresh</button></div>
          <!-- svelte-ignore a11y_no_noninteractive_tabindex (Scrollable output must be keyboard accessible.) -->
          <pre class="log-output" role="region" tabindex="0" aria-label="Worker log output">{logs.length ? logs.join('\n') : logsLoaded ? 'No worker output yet.' : nativeAvailable ? 'Loading worker logs…' : 'Worker logs are available in the desktop app.'}</pre>
          <div class="workspace-footer"><span>{info?.profileDirectory ?? 'Logs stay on this computer.'}</span><button class="text-button" disabled={!nativeAvailable || !!busy} onclick={() => perform('Opening data folder…', api.openData)}>Open data folder ↗</button></div>
        {/if}
      </div>
    {/key}
  </main>
  <footer class="app-footer">
    <span>{info ? `v${info.version}` : 'Guerrilla Worker'}</span>
    {#if busy}<span class="pending" role="status">{busy}</span>{/if}
    <div class="theme-switch" role="group" aria-label="Appearance">
      {#each [{ value: 'light', icon: 'sun', label: 'Light' }, { value: 'system', icon: 'monitor', label: 'System' }, { value: 'dark', icon: 'moon', label: 'Dark' }] as option}
        <button aria-label={`${option.label} theme`} title={`${option.label} theme`} aria-pressed={theme === option.value} onclick={() => setTheme(option.value as Theme)}><Icon name={option.icon as IconName} size={14} /></button>
      {/each}
    </div>
  </footer>
</div>

{#if disconnectOpen || stopOpen}
  <dialog class="modal" aria-labelledby="confirm-title" aria-describedby="confirm-description" use:showDialog oncancel={() => { disconnectOpen = false; stopOpen = false; }}><h2 id="confirm-title">{disconnectOpen ? 'Disconnect this worker?' : 'Stop the current job?'}</h2><p id="confirm-description">{disconnectOpen ? 'Saved enrollment will be removed. Checkpoints stay on this computer. You can revoke the old identity on the server’s Workers page.' : 'Execution will stop using the worker’s shutdown process. Verified checkpoints remain available for recovery. To finish this job first, choose pause instead.'}</p><div class="actions"><button class="primary" onclick={() => doAction(disconnectOpen ? 'disconnect' : 'stop')}>{disconnectOpen ? 'Disconnect' : 'Stop worker'}</button><button onclick={() => { disconnectOpen = false; stopOpen = false; }}>Cancel</button>{#if stopOpen}<button onclick={() => doAction('drain')}>Finish job, then pause</button>{/if}</div></dialog>
{/if}
