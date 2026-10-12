import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describeWorkerError } from './http';
import { main } from './runtime';

export { main, execute, scratchIsClean } from './runtime';

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => {
    console.error(describeWorkerError(error));
    process.exitCode = 1;
  });
}
