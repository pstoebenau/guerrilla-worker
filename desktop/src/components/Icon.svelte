<script lang="ts">
  export type IconName = 'overview' | 'settings' | 'logs' | 'arrow' | 'external' | 'gpu' | 'engine' | 'monitor' | 'sun' | 'moon' | 'queue';
  let { name, size = 18 }: { name: IconName; size?: number } = $props();
  const paths: Record<IconName, string> = {
    overview: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
    settings: 'M4 7h16 M4 17h16 M8 4v6 M16 14v6',
    logs: 'M8 4H4v16h16V4h-4 M9 3h6v4H9z M8 12h8 M8 16h5',
    arrow: 'M5 12h14 M13 6l6 6-6 6',
    external: 'M14 3h7v7 M21 3l-9 9 M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5',
    gpu: 'M5 5h14v14H5z M9 9h6v6H9z M9 2v3 M15 2v3 M9 19v3 M15 19v3 M2 9h3 M2 15h3 M19 9h3 M19 15h3',
    engine: 'M12 3l9 5-9 5-9-5z M3 12l9 5 9-5 M3 16l9 5 9-5',
    monitor: 'M3 4h18v13H3z M8 21h8 M12 17v4',
    sun: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M12 2v2 M12 20v2 M2 12h2 M20 12h2 M5 5l1.5 1.5 M17.5 17.5L19 19 M5 19l1.5-1.5 M17.5 6.5L19 5',
    moon: 'M20.9 13A9 9 0 0 1 11 3.1 9 9 0 1 0 20.9 13Z',
    queue: 'M4 4v10a4 4 0 0 0 4 4h12 M15 13l5 5-5 5',
  };
</script>

<svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d={paths[name]} /></svg>
