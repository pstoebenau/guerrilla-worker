import path from 'node:path';

const root = path.resolve(import.meta.dir, '..');
const args = process.argv.slice(2);
// A complete release needs native sidecars, vendored runtimes, and the Windows
// installer. Keep that preparation behind the standard `tauri build` command.
let commandIndex = 0;
while (args[commandIndex] === '--verbose' || /^-v+$/.test(args[commandIndex] ?? '')) commandIndex++;
const build = args[commandIndex] === 'build' && !args.some(arg => arg === '--help' || arg === '-h');
const command = build
  ? [process.execPath, path.join(import.meta.dir, 'build-desktop.ts'), ...args.slice(0, commandIndex), ...args.slice(commandIndex + 1)]
  : [process.execPath, 'run', 'tauri', ...args];
const child = Bun.spawn(command, {
  cwd: build ? root : path.join(root, 'desktop'),
  stdin: 'inherit', stdout: 'inherit', stderr: 'inherit',
});
process.exit(await child.exited);
