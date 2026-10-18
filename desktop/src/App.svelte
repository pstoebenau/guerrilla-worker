<script lang="ts">
  import { onMount } from 'svelte';
  import { fly } from 'svelte/transition';
  import Icon from './components/Icon.svelte';
  import type { IconName } from './components/Icon.svelte';
  import { listen } from '@tauri-apps/api/event';
  import { api, nativeAvailable } from './lib/native';
  import { parseStatus } from './lib/parse';
  import { downloadSize, latestUpdate, parseUpdateSnapshot, updatePercent } from './lib/updates';
  import type { UpdateSnapshot } from './lib/updates';
  import { isRunning, progressPercent, statusTitle } from './lib/types';
  import type { BrowseField, DesktopInfo, Logs, Preflight, Profile, WorkerAction, WorkerStatus } from './lib/types';

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
  let logResult = $state<Logs | null>(null);
  let logSource = $state('');
  let loadingLogs = $state(false);
  let logRequest = 0;
  let disconnectOpen = $state(false);
  let stopOpen = $state(false);
  let advancedOpen = $state(false);
  let update = $state.raw<UpdateSnapshot | null>(null);
  let updateError = $state('');
  let updatePending = $state(false);
  let checkingUpdates = $state(false);
  let refreshing = false;
  let alive = true;
  const running = $derived(isRunning(status));
  const unavailable = $derived(!nativeAvailable || !info?.runtimeAvailable);
  const percent = $derived(progressPercent(status?.progress));
  const displayEngines = $derived(check?.engines ?? status?.engines ?? []);
  const displayGpus = $derived(check?.gpus ?? status?.gpus ?? []);
  const title = $derived(statusTitle(status, nativeAvailable));
  const downloadPercent = $derived(updatePercent(update));
  const updateActive = $derived(update?.status === 'checking' || update?.status === 'downloading' || update?.status === 'installing');
  const updateQueued = $derived(update?.installQueued === true);
  const updateCanceling = $derived(update?.status === 'waitingForIdle' && !update.installQueued);
  const updateWorkerBusy = $derived(update?.workerBusy || status?.state === 'busy' || status?.state === 'draining' || status?.state === 'starting');

  async function refresh() {
    if (!nativeAvailable || refreshing || busy) return;
    refreshing = true;
    try {
      const next = await api.status();
      if (alive) { status = next; if (info) info.enrolled = !!next.workerId; }
      if (page === 'logs') await loadLogs();
    } catch (e) { if (alive) error = String(e); }
    finally { refreshing = false; }
  }
  async function loadLogs(source = logSource) {
    const request = ++logRequest;
    loadingLogs = true;
    try {
      const result = await api.logs(source || undefined);
      if (alive && request === logRequest) { logResult = result; logSource = result.source; }
    } catch (e) { if (alive && request === logRequest) error = String(e); }
    finally { if (request === logRequest) loadingLogs = false; }
  }
  function receiveUpdate(next: UpdateSnapshot) {
    if (alive) {
      const latest = latestUpdate(update, next);
      if (latest === next) { update = latest; updateError = ''; }
    }
  }
  async function checkForUpdates() {
    if (!nativeAvailable || checkingUpdates || updateActive || updateQueued || updateCanceling) return;
    checkingUpdates = true; updateError = '';
    try { receiveUpdate(await api.checkForUpdates()); }
    catch (e) { if (alive) updateError = String(e); }
    finally { checkingUpdates = false; }
  }
  async function updateAction(cancel = false) {
    if (updatePending) return;
    updatePending = true; updateError = '';
    try { receiveUpdate(await (cancel ? api.cancelUpdate() : api.scheduleUpdate(true))); }
    catch (e) { if (alive) updateError = String(e); }
    finally { updatePending = false; }
  }
  async function subscribeToUpdates(unlisteners: (() => void)[]) {
    try {
      const unlisten = await listen<unknown>('updater-state', event => {
        if (!alive) return;
        try { receiveUpdate(parseUpdateSnapshot(event.payload)); }
        catch (e) { updateError = String(e); }
      });
      if (!alive) { unlisten(); return; }
      unlisteners.push(unlisten);
      receiveUpdate(await api.updateSnapshot());
      if (alive && update?.status !== 'ready') await checkForUpdates();
    } catch (e) { if (alive) updateError = String(e); }
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
    await perform('Installing runtime…', async () => {
      installingRuntime = true;
      try {
        info = await api.installGpuRuntime();
        check = null;
        notice = 'Spirula and its reconstruction model are installed and verified.';
      } finally { installingRuntime = false; }
    });
  }
  function showDialog(node: HTMLDialogElement) { node.showModal(); return () => node.close(); }
  function engineName(name: string) { return name.toLowerCase() === 'spirula' ? 'Spirula' : name; }
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
      void subscribeToUpdates(unlisteners);
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
    {#each [{ id: 'overview', title: 'Overview', icon: 'overview' }, { id: 'setup', title: 'Setup', icon: 'settings' }, { id: 'logs', title: 'Logs', icon: 'logs' }] as item (item.id)}
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
    {#if updateError || (update && update.status !== 'idle')}
      <section class="update-notice" aria-label="App update">
        <div class="update-copy" role="status">
          <strong>{update?.status === 'installing' ? 'Installing update…' : update?.status === 'checking' ? 'Checking for updates…' : update?.version ? `Version ${update.version}${update.status === 'downloading' ? ' is downloading' : ' available'}` : 'App update'}</strong>
          {#if updateError || update?.status === 'error'}
            <p class="update-error" role="alert">{updateError || update?.error || 'The update could not finish. Check your connection and try again.'}</p>
          {:else if updateCanceling}
            <p>{update?.error || 'Update canceled. Finishing the current job before restoring worker activity.'}</p>
          {:else if updateQueued}
            <p>The update will install and restart the app after current jobs finish. Your settings and enrollment are saved.</p>
          {:else if update?.status === 'ready'}
            <p>Ready to install. The app will restart and keep your settings and enrollment.</p>
          {:else if update?.status === 'downloading'}
            <p>{downloadSize(update.downloaded)}{update.total ? ` of ${downloadSize(update.total)}` : ''}{downloadPercent === null ? '' : ` · ${downloadPercent}%`} · Jobs continue while downloading.</p>
          {:else if update?.status === 'installing'}
            <p>The app will restart when installation finishes.</p>
          {/if}
        </div>
        {#if updateCanceling}
          <span class="muted">Restoring worker activity…</span>
        {:else if updateQueued}
          <button disabled={updatePending || update?.status === 'installing'} onclick={() => updateAction(true)}>Cancel update</button>
        {:else if update?.status === 'ready'}
          <button class="primary" disabled={updatePending || !!busy} onclick={() => updateAction()}>{updateWorkerBusy ? 'Update when jobs finish' : 'Update and restart'}</button>
        {:else if updateError || update?.status === 'error'}
          <button disabled={checkingUpdates || updatePending || updateActive} onclick={checkForUpdates}>Retry update</button>
        {/if}
        {#if update?.status === 'downloading'}
          <div class="progress-track update-progress" role="progressbar" aria-label="Update download progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={downloadPercent ?? undefined} class:indeterminate={downloadPercent === null}><div style:width={downloadPercent === null ? '30%' : `${downloadPercent}%`}></div></div>
        {/if}
      </section>
    {/if}

    {#key page}
      <div class="page-content" class:logs-page={page === 'logs'} in:fly={{ y: 2, duration: reducedMotion ? 0 : 100 }}>
        {#if page === 'overview'}
          <section class="worker-summary" class:is-running={running} aria-labelledby="worker-state"><div class="worker-summary-body">
            <h1 id="worker-state"><span class="dot" class:online={running}></span>{title}</h1>
            {#if status?.message}<p class="summary-copy">{status.message}</p>{/if}
            </div>
            <div class="actions">
              <button disabled={!nativeAvailable || !!busy} onclick={() => selectPage('logs')}>View job logs</button>
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
          <div class="machine-columns"><section><div class="section-heading"><h3><Icon name="gpu" size={17} />Graphics</h3></div>{#if displayGpus.length}{#each displayGpus as gpu (gpu.uuid ?? gpu.name)}<div class="resource-row"><strong>{gpu.name}</strong><span>{gpu.memoryMb ? `${(gpu.memoryMb / 1024).toFixed(1)} GB VRAM` : 'Detected'}</span></div>{/each}{:else}<p class="muted">{check ? 'No compatible GPU detected.' : 'Not checked yet.'}</p>{/if}</section>
          <section><div class="section-heading"><h3><Icon name="engine" size={17} />Engines</h3><button class="text-button" disabled={!!busy || unavailable} onclick={() => perform('Checking dependencies…', async () => { check = await api.preflight(); })}>Check</button></div>{#if displayEngines.length}{#each displayEngines as engine (engine.name)}<div class="resource-row"><strong>{engineName(engine.name)}</strong><span class:ready={engine.available}>{engine.available ? 'Available' : 'Unavailable'}</span></div>{/each}{:else}<p class="muted">Not checked yet.</p>{/if}</section></div>
          <footer class="workspace-footer"><span>Jobs keep running while the app is in the tray.</span><button class="text-button" disabled={!nativeAvailable || !!busy} onclick={() => perform('Stopping jobs and quitting…', api.quit)}>Quit and stop all jobs</button></footer>

        {:else if page === 'setup'}
          {#if profile}
            <section class="settings-section enrollment"><div class="section-heading"><h3>Enrollment</h3>{#if info?.enrolled}<span class="ready">Enrolled</span>{/if}</div><p class="field-help">Server: {profile.controlUrl}</p>{#if info?.enrolled}<p class="muted">Worker {status?.workerId ?? 'identity saved'}. Disconnect removes this computer’s enrollment and keeps checkpoints.</p><button disabled={!!busy || running} onclick={() => disconnectOpen = true}>Disconnect worker…</button>{:else}<label for="token">Enrollment token</label><input id="token" type="password" autocomplete="off" spellcheck="false" maxlength="4096" bind:value={token} disabled={!!busy || !nativeAvailable} placeholder="Paste a token from the server’s Workers page"/><p class="field-help">The token is used once for enrollment and is never saved in settings.</p><div class="actions"><button class="primary" disabled={!!busy || !nativeAvailable || !token.trim()} onclick={() => { const secret = token; token = ''; void perform('Enrolling worker…', async () => { status = await api.enroll(secret, profile!.controlUrl); await reloadInfo(); await selectPage('overview'); notice = 'Worker enrolled. Start the worker when ready.'; }); }}>Enroll worker</button><button disabled={!!busy || !nativeAvailable} onclick={() => perform('Opening server…', api.openServer)}>Open server ↗</button></div>{/if}</section>
            <form onsubmit={(e) => { e.preventDefault(); void perform('Saving settings…', async () => { await save(); notice = 'Settings saved.'; }); }}>

              {#if info?.gpuRuntime.supported}
                <section class="settings-section"><div class="section-heading"><div><h3>Spirula runtime</h3><p>{info.gpuRuntime.installed ? 'Spirula and its reconstruction model are installed on this computer.' : 'Install Spirula and its reconstruction model for scanning. The initial download is about 1.2 GB.'}</p></div></div><div class="actions"><button type="button" disabled={!!busy || running || unavailable} onclick={installRuntime}>Install / repair runtime</button><span class="muted">{info.gpuRuntime.installed ? 'Installed' : 'Internet required for initial setup'}</span></div></section>
              {/if}

              <section class="settings-section"><div class="section-heading"><h3>Startup</h3></div><label class="checkbox-label"><input type="checkbox" bind:checked={profile.startAtLogin} disabled={!!busy || running}/><span>Start worker when I log in</span></label></section>
            {#if check}<section class="check-results"><h3>Dependency check</h3>{#if check.message}<p>{check.message}</p>{/if}{#each [...(check.checks ?? []), ...(check.engines ?? [])] as result (result.name)}<div class="resource-row"><div><strong>{engineName(result.name)}</strong>{#if result.message}<p>{result.message}</p>{/if}</div><span class:ready={result.available}>{result.available ? 'Available' : 'Needs attention'}</span></div>{/each}{#if !check.gpus?.length}<p class="muted">No compatible GPU was detected.</p>{/if}</section>{/if}
              <details class="advanced-settings" bind:open={advancedOpen}>
                <summary>Advanced <span>Server and storage</span></summary>
              <div class="advanced-field"><label for="server">Server URL</label><input id="server" type="url" oninvalid={() => advancedOpen = true} bind:value={profile.controlUrl} disabled={!!busy || running || info?.enrolled} required placeholder="https://guerrilla.dad"/><p class="field-help">{info?.enrolled ? 'Stop and disconnect to change servers.' : 'The server that assigns jobs to this worker.'}</p></div>
                <div class="path-field"><label for="scratch">Scratch folder</label><div><input id="scratch" bind:value={profile.scratchDirectory} disabled={!!busy || running} required oninvalid={() => advancedOpen = true}/><button type="button" disabled={!!busy || running} onclick={() => browse('scratchDirectory')}>Browse…</button></div><p class="field-help">Working files and retained checkpoints. Leave this at its default unless you need a different disk.</p></div>
              </details>

              <div class="actions save-actions"><button type="submit" class="primary" disabled={!!busy || running}>Save settings</button><button type="button" disabled={!!busy || running || unavailable} onclick={() => perform('Checking dependencies…', async () => { await save(); check = await api.preflight(); notice = check.ok ? 'Dependency check complete. Spirula is ready.' : 'Dependency check needs attention. Review the results below.'; })}>Save and check dependencies</button>{#if running}<span class="muted">Stop the worker to change settings.</span>{/if}</div>
            </form>


          {:else}<section class="setup-unavailable"><h2>Setup requires the desktop app</h2><p>Choose a server, install the runtime, and paste an enrollment token there.</p></section>{/if}

        {:else}
          <div class="section-heading"><h3>Job and worker logs</h3><div class="actions"><button disabled={!!busy || !nativeAvailable || loadingLogs} onclick={() => loadLogs()}>Refresh</button><button disabled={!nativeAvailable || !logResult || loadingLogs} onclick={() => perform('Copying log…', async () => { await navigator.clipboard.writeText(`${logResult!.path}\n\n${logResult!.lines.join('\n')}`); notice = 'Log copied.'; })}>Copy log</button></div></div>
          <p class="field-help">Pipeline details, source download failures, and worker service output. Newest logs appear first. Updates every 3 seconds.</p>
          <label class="log-source" for="log-source">Log file</label>
          <select id="log-source" value={logSource} disabled={!nativeAvailable || !logResult} onchange={(event) => { logSource = event.currentTarget.value; void loadLogs(logSource); }}>
            {#each logResult?.sources ?? [] as source (source.id)}<option value={source.id}>{source.label}</option>{/each}
          </select>
          {#if logResult}<p class="log-path">{logResult.path}</p><p class="field-help">{logResult.truncated ? 'Showing the last 1,000 lines, up to 256 KiB. Open the log folder for the complete file.' : `${logResult.lines.length} lines`}</p>{/if}
          <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
          <pre class="log-output" role="region" tabindex="0" aria-label="Worker log output" aria-busy={loadingLogs}>{logResult ? logResult.lines.join('\n') || 'This log is empty.' : nativeAvailable ? 'Loading logs…' : 'Logs are available in the desktop app.'}</pre>
          <div class="workspace-footer"><span>Logs and checkpoints stay on this computer.</span><button class="text-button" disabled={!nativeAvailable || !!busy || !logResult || loadingLogs} onclick={() => perform('Opening log folder…', () => api.openLogFolder(logSource))}>Open log folder ↗</button></div>
        {/if}
      </div>
    {/key}
  </main>
  <footer class="app-footer">
    <span>{info ? `v${info.version}` : 'Guerrilla Worker'}</span>
    {#if nativeAvailable}<button class="text-button" disabled={checkingUpdates || updateActive || updateQueued || updateCanceling || updatePending} onclick={checkForUpdates}>{checkingUpdates || update?.status === 'checking' ? 'Checking…' : update?.status === 'idle' ? 'Up to date · Check again' : 'Check for updates'}</button>{/if}
    {#if busy}<span class="pending" role="status">{busy}</span>{/if}
    <div class="theme-switch" role="group" aria-label="Appearance">
      {#each [{ value: 'light', icon: 'sun', label: 'Light' }, { value: 'system', icon: 'monitor', label: 'System' }, { value: 'dark', icon: 'moon', label: 'Dark' }] as option (option.value)}
        <button aria-label={`${option.label} theme`} title={`${option.label} theme`} aria-pressed={theme === option.value} onclick={() => setTheme(option.value as Theme)}><Icon name={option.icon as IconName} size={14} /></button>
      {/each}
    </div>
  </footer>
</div>

{#if disconnectOpen || stopOpen}
  <dialog class="modal" aria-labelledby="confirm-title" aria-describedby="confirm-description" {@attach showDialog} oncancel={() => { disconnectOpen = false; stopOpen = false; }}><h2 id="confirm-title">{disconnectOpen ? 'Disconnect this worker?' : 'Stop the current job?'}</h2><p id="confirm-description">{disconnectOpen ? 'Saved enrollment will be removed. Checkpoints stay on this computer. You can revoke the old identity on the server’s Workers page.' : 'Execution will stop using the worker’s shutdown process. Verified checkpoints remain available for recovery. To finish this job first, choose pause instead.'}</p><div class="actions"><button class="primary" onclick={() => doAction(disconnectOpen ? 'disconnect' : 'stop')}>{disconnectOpen ? 'Disconnect' : 'Stop worker'}</button><button onclick={() => { disconnectOpen = false; stopOpen = false; }}>Cancel</button>{#if stopOpen}<button onclick={() => doAction('drain')}>Finish job, then pause</button>{/if}</div></dialog>
{/if}
