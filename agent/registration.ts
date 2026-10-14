import { createHash } from 'node:crypto';
import { ENGINES, PROTOCOL_VERSION, type Engine, type Registration } from '@guerrilla/worker-protocol';
import { gpuInventory } from './gpu';
import packageInfo from '../package.json';

type PreflightReport = {
  backends?: Partial<Record<Engine, { available: boolean; runtimeVersions?: Record<string, string> }>>;
  gpus?: unknown;
};

export function registrationFromReport(report: PreflightReport): Registration {
  const engines: Registration['engines'] = {};
  for (const engine of ENGINES) {
    const capability = report.backends?.[engine];
    if (!capability?.available) continue;
    if (!capability.runtimeVersions) throw new Error(`Missing runtime identity for ${engine}`);
    // An unrelated engine's installation must not change this engine's identity.
    const versions = JSON.stringify(capability.runtimeVersions, Object.keys(capability.runtimeVersions).sort());
    engines[engine] = createHash('sha256').update(versions).digest('hex');
  }
  return {
    ...enrollmentIdentity(),
    engines,
    gpus: gpuInventory(report),
    healthy: Object.keys(engines).length > 0,
  };
}

export function enrollmentIdentity(): Registration {
  return {
    protocolVersion: PROTOCOL_VERSION,
    build: packageInfo.version,
    runtime: `${process.platform}-${process.arch}`,
    engines: {},
    gpus: [],
    healthy: false,
  };
}
