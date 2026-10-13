import { describe, expect, test } from 'bun:test';
import { isRunning, progressPercent, statusTitle } from '../src/lib/types';

describe('worker management state', () => {
  test('browser preview cannot claim a connected worker', () => {
    expect(statusTitle({ state: 'idle', connected: true }, false)).toBe('Desktop runtime required');
  });
  test('a draining worker still blocks execution settings', () => {
    expect(isRunning({ state: 'draining', connected: true })).toBe(true);
    expect(isRunning({ state: 'stopped', connected: false })).toBe(false);
    expect(isRunning(null)).toBe(false);
    expect(isRunning({ state: 'error', connected: false, running: true })).toBe(true);
    expect(isRunning({ state: 'error', connected: false, running: false })).toBe(false);
  });
  test('progress is bounded and unknown progress stays indeterminate', () => {
    expect(progressPercent(undefined)).toBeNull();
    expect(progressPercent(NaN)).toBeNull();
    expect(progressPercent(0.321)).toBe(32);
    expect(progressPercent(1.7)).toBe(100);
  });
});
