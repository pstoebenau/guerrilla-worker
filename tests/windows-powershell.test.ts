import { expect, test } from 'bun:test';
import { windowsPowerShellEnvironment } from '../scripts/windows-powershell';

test('Windows PowerShell drops inherited module paths without changing other environment values', () => {
  const inherited = { PSModulePath: 'PowerShell-7/Modules', psmodulepath: 'other/Modules', PATH: 'tools', GUERRILLA_CRT_STAGE_DIRECTORY: 'stage' };
  expect(windowsPowerShellEnvironment(inherited)).toEqual({ PATH: 'tools', GUERRILLA_CRT_STAGE_DIRECTORY: 'stage' });
  expect(inherited.PSModulePath).toBe('PowerShell-7/Modules');
});

// Cold PowerShell startup and module loading can exceed Bun's default five
// seconds on hosted Windows runners. Keep a bounded process integration budget.
test.skipIf(process.platform !== 'win32')('Windows PowerShell can load packaging cmdlets with a polluted parent module path', async () => {
  const child = Bun.spawn(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
    "$ErrorActionPreference = 'Stop'; Get-Command Get-AuthenticodeSignature, Get-FileHash, ConvertTo-Json | Select-Object -ExpandProperty Name"], {
    env: windowsPowerShellEnvironment({ ...process.env, PSModulePath: 'C:\\unavailable-powershell-7\\Modules' }),
    stdin: 'ignore', stdout: 'pipe', stderr: 'pipe', windowsHide: true,
  });
  const [stdout, stderr, code] = await Promise.all([
    new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited,
  ]);
  expect(code, stderr).toBe(0);
  expect(stdout).toContain('Get-AuthenticodeSignature');
  expect(stdout).toContain('Get-FileHash');
  expect(stdout).toContain('ConvertTo-Json');
}, 30_000);
