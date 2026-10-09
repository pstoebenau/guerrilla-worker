/** Public wire contract. No database, storage credentials, or execution commands. */
export const PROTOCOL_VERSION = 1 as const;
export const PROTOCOL_PACKAGE_VERSION = "1.0.0";
export type Engine = "spirula" | "lichtfeld";
export type Destination = "personal" | "cloud";
export type Settings = Record<string, Record<string, string | number | boolean | null>>;
export interface ScanRequest {
  version: 1;
  backend: Engine;
  inputId: string;
  title: string;
  settings: Settings;
  maxCap: number;
}
export interface Registration {
  protocolVersion: 1;
  build: string;
  runtime: string;
  engines: Partial<Record<Engine, string>>;
  gpus: { id: string; name: string; memoryBytes: number }[];
  healthy: boolean;
}
export interface Fence { scanId: string; attemptId: string; leaseId: string }
export type Command = "continue" | "drain" | "stop";
export interface ArtifactManifest {
  artifactId: string;
  relativePath: string;
  role: string;
  size: number;
  sha256: string;
  url: string;
}
export interface Assignment extends Fence {
  protocolVersion: 1;
  leaseExpiresAt: string;
  settingsHash: string;
  request: ScanRequest;
  input: { kind: "stored" | "source"; filename: string; url: string; sha256: string | null; size: number | null };
  resume: ArtifactManifest[];
  runtimeVersions?: Record<string, string>;
}
export interface PollResponse { assignment: Assignment | null; command: Command; pollAfterMs: number }
export interface HeartbeatResponse { leaseExpiresAt: string; command: Command }
export interface EnrollmentRequest { token: string; registration: Registration }
export interface EnrollmentResponse { workerId: string; credential: string }
export interface WorkerEvent extends Fence {
  eventId: string;
  sequence: number;
  kind: string;
  stage?: string;
  message: string;
  progress?: Record<string, unknown>;
  runtimeVersions?: Record<string, string>;
}
export interface ArtifactAllocation extends Fence {
  action: "allocate";
  clientId: string;
  relativePath: string;
  role: string;
  size: number;
  sha256: string;
  checkpointId?: string;
  reuseVerified?: boolean;
}
export interface UploadIntent {
  artifactId: string;
  method: "put" | "multipart" | "reuse" | "verify";
  url?: string;
  partSize?: number;
  parts?: { partNumber: number; url: string }[];
  expiresAt: string;
}
export interface ArtifactCompletion extends Fence { artifactId: string; parts?: { partNumber: number; etag: string }[] }
export interface CheckpointCommit extends Fence { checkpointId: string; artifactIds: string[]; sequence: number }
export interface FinishRequest extends Fence { state: "completed" | "failed" | "interrupted"; error?: string }

export function isSafeRelativePath(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 1024 &&
    !/[\\\x00-\x1f:]/.test(value) && !value.startsWith("/") &&
    value.split("/").every((part) => part !== "" && part !== "." && part !== ".." && !/[. ]$/.test(part) && !/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(part));
}
export function isSha256(value: unknown): value is string {
  return typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
}
export function redactSecrets(message: string): string {
  return message.replace(/https?:\/\/[^\s"'<>]+/gi, "[redacted URL]").replace(/Bearer\s+[^\s]+/gi, "Bearer [redacted]");
}
