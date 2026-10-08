import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const venv = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const python = process.env.PYTHON || (existsSync(venv) ? venv : process.platform === 'win32' ? 'python' : 'python3');
const result = spawnSync(python, process.argv.slice(2), { stdio: 'inherit', env: process.env, windowsHide: true });
if (result.error) console.error(`Could not run Python (${python}): ${result.error.message}`);
process.exit(result.status ?? 1);
