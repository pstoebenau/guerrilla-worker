import path from 'node:path';

const runtimeKeys = [
  'PATH', 'Path', 'SystemRoot', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP',
  'HOME', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)',
  'CUDA_PATH', 'CUDA_HOME', 'VK_ICD_FILENAMES', 'NVIDIA_DRIVER_CAPABILITIES',
  'PYTHON', 'SPIRULA_BIN', 'SPLAT_TRANSFORM_BIN', 'SPLAT_TRANSFORM_GPU',
  'SPLAT_TRANSFORM_GPU_BACKEND', 'NODE_BIN', 'LICHTFELD_BIN', 'LICHTFELD_COLMAP_PLUGIN',
  'LICHTFELD_DENSIFICATION_PLUGIN', 'TORCH_HOME', 'GPU_LOCK_PATH', 'GPU_LOCK_WAIT',
  'WORKER_SCRATCH', 'WORKER_CONTROL_URL', 'WORKER_CREDENTIAL_FILE',
  'WORKER_AGENT_LOCK_DIRECTORY', 'WORKER_ENROLLMENT_TOKEN', 'WORKER_RUNTIME_ROOT', 'WORKER_CONFIG_FILE', 'WORKER_PIPELINE_BIN',
];
const developmentKeys = [
  'WORKER_IMAGE', 'WORKER_CONTAINER_NAME', 'WORKER_GPU_DIRECTORY', 'WORKER_GPU_DEVICE',
  'WORKER_CREDENTIAL_DIRECTORY', 'WORKER_RUNTIME_DIRECTORY', 'WORKER_BUILD_CACHE',
  'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_CONFIG', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH',
  'DOCKER_BUILDKIT', 'BUILDX_BUILDER', 'BUILDKIT_PROGRESS',
  'CARGO_HOME', 'RUSTUP_HOME', 'RUSTC', 'RUSTUP_TOOLCHAIN', 'CARGO_BUILD_TARGET',
  'CC', 'CXX', 'AR', 'LIB', 'INCLUDE', 'LIBPATH', 'SDKROOT', 'DEVELOPER_DIR',
  'MACOSX_DEPLOYMENT_TARGET', 'PKG_CONFIG_PATH', 'CMAKE_PREFIX_PATH',
];

function select(source: NodeJS.ProcessEnv, keys: string[], platform: NodeJS.Platform): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const key of keys) {
    const actual = platform === 'win32'
      ? Object.keys(source).find(candidate => candidate.toUpperCase() === key.toUpperCase())
      : key;
    if (actual && source[actual] !== undefined) env[key === 'Path' ? 'PATH' : key] = source[actual];
  }
  return env;
}

// Execution receives only runtime configuration, never platform secrets or
// interpreter injection such as NODE_OPTIONS, PYTHONPATH or loader overrides.
export function workerEnvironment(source: NodeJS.ProcessEnv, sourceDirectory: string, platform = process.platform): NodeJS.ProcessEnv {
  const env = select(source, runtimeKeys, platform);
  env.PYTHON ??= path.join(sourceDirectory, '.venv', platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  env.WORKER_CONTROL_URL ??= source.ORIGIN;
  if (platform === 'win32') env.WORKER_MODE = 'native-development';
  return env;
}

// The parent can apply this before entering the independent checkout. Build
// tools need their own locations, but those values never reach the agent.
export function developmentEnvironment(source: NodeJS.ProcessEnv, sourceDirectory: string, platform = process.platform): NodeJS.ProcessEnv {
  return { ...workerEnvironment(source, sourceDirectory, platform), ...select(source, developmentKeys, platform) };
}
