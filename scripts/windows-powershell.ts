/** Let Windows PowerShell construct its own module paths when launched via Bun. */
export function windowsPowerShellEnvironment(env: NodeJS.ProcessEnv = process.env): NodeJS.ProcessEnv {
  // PowerShell 7 paths inherited through an intermediate process can shadow
  // Windows PowerShell's built-in Security and Utility modules.
  return Object.fromEntries(Object.entries(env).filter(([name]) => name.toUpperCase() !== 'PSMODULEPATH'));
}
