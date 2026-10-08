import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { redactSecrets } from '@guerrilla/worker-protocol';
import { main } from './runtime';

export { main, execute, scratchIsClean } from './runtime';

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => {
    console.error(redactSecrets(error instanceof Error ? error.message : 'Worker failed'));
    process.exitCode = 1;
  });
}
