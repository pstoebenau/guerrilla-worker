import { describe, expect, test } from 'bun:test';
import { latestUpdate, parseUpdateSnapshot, updatePercent } from '../src/lib/updates';

const snapshot = {
  revision: 4, status: 'downloading' as const, version: '0.4.0', downloaded: 25,
  total: 100, installQueued: false, workerBusy: true, error: null, releaseUrl: null,
};

describe('desktop update state', () => {
  test('validates download and queued install events', () => {
    expect(parseUpdateSnapshot(snapshot)).toEqual(snapshot);
    expect(parseUpdateSnapshot({ ...snapshot, status: 'waitingForIdle', installQueued: true }).installQueued).toBe(true);
    expect(parseUpdateSnapshot({ ...snapshot, status: 'error', error: 'Signature verification failed' }).error).toContain('Signature');
  });

  test('malformed events cannot enable install actions or hide failures', () => {
    for (const invalid of [null, [], {}, { ...snapshot, status: 'available' }, { ...snapshot, revision: -1 },
      { ...snapshot, downloaded: NaN }, { ...snapshot, total: -10 }, { ...snapshot, installQueued: 'false' },
      { ...snapshot, workerBusy: undefined }, { ...snapshot, error: {} }, { ...snapshot, version: 4 }]) {
      expect(() => parseUpdateSnapshot(invalid)).toThrow('invalid update status');
    }
  });

  test('a delayed snapshot cannot overwrite a newer progress or installation event', () => {
    const waiting = parseUpdateSnapshot({ ...snapshot, revision: 9, status: 'waitingForIdle', installQueued: true });
    const stale = parseUpdateSnapshot(snapshot);
    expect(latestUpdate(waiting, stale)).toBe(waiting);
    expect(latestUpdate(stale, waiting)).toBe(waiting);
    expect(latestUpdate(null, stale)).toBe(stale);
  });

  test('unknown download size stays indeterminate and progress is bounded', () => {
    expect(updatePercent(null)).toBeNull();
    expect(updatePercent(parseUpdateSnapshot({ ...snapshot, total: null }))).toBeNull();
    expect(updatePercent(parseUpdateSnapshot({ ...snapshot, total: 0 }))).toBeNull();
    expect(updatePercent(parseUpdateSnapshot(snapshot))).toBe(25);
    expect(updatePercent(parseUpdateSnapshot({ ...snapshot, downloaded: 120 }))).toBe(100);
  });
});
