import path from 'node:path';
import { existsSync } from 'node:fs';
import { redactSecrets } from '@guerrilla/worker-protocol';

// Compiled modules live in Bun's virtual filesystem. The Python pipeline is
// deliberately shipped beside the executable and uses installed engines.
process.env.PIPELINE_ROOT ??= path.join(path.dirname(process.execPath), 'pipeline');
process.env.WORKER_MODE = 'native-development';
process.env.NODE_BIN ??= process.execPath;
process.env.WORKER_CONTROL_URL ||= 'https://guerrilla.dad';
const shutdown = new AbortController();
const stopFile = process.env.WORKER_STOP_FILE;
const trayPid = Number(process.env.WORKER_TRAY_PID);
const timer = stopFile || trayPid ? setInterval(() => {
  if (stopFile && existsSync(stopFile)) shutdown.abort();
  if (Number.isSafeInteger(trayPid) && trayPid > 0) {
    try { process.kill(trayPid, 0); } catch { shutdown.abort(); }
  }
}, 250) : undefined;
try {
  const { main } = await import('./runtime');
  await main(shutdown);
} catch (error) {
  console.error(redactSecrets(error instanceof Error ? error.message : 'Worker failed'));
  process.exitCode = 1;
} finally {
  clearInterval(timer);
}
