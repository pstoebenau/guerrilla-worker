import path from 'node:path';
const root = path.resolve(import.meta.dir, '..');
if (process.platform !== 'win32') throw new Error('Build the Windows tray package on Windows.');
const build = Bun.spawn(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
  path.join(root, 'scripts/build-windows.ps1'), ...process.argv.slice(2)], { cwd: root, stdout: 'inherit', stderr: 'inherit' });
process.exit(await build.exited);
