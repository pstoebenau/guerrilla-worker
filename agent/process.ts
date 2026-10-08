import { spawn, type ChildProcess } from "node:child_process";
import { createInterface } from "node:readline";
import { createWriteStream } from "node:fs";
import { finished } from "node:stream/promises";

export type PipelineEvent = {
  type: string;
  stage?: string;
  message?: string;
  [key: string]: unknown;
};

export async function runPipeline(options: {
  command: string;
  args: string[];
  cwd: string;
  logPath: string;
  signal: AbortSignal;
  onEvent: (event: PipelineEvent) => Promise<void>;
}): Promise<void> {
  options.signal.throwIfAborted();
  const log = createWriteStream(options.logPath, { flags: "a", mode: 0o600 });
  const child = spawn(options.command, options.args, {
    cwd: options.cwd,
    env: { ...process.env, WORKER_PARENT_PID: String(process.pid) },
    stdio: ["ignore", "pipe", "pipe"],
    detached: process.platform !== "win32",
    windowsHide: true,
  });
  let eventError: unknown;
  let eventFailed = false;
  let pending = Promise.resolve();
  let killTimer: NodeJS.Timeout | undefined;
  const stop = () => {
    terminate(child, "SIGTERM");
    killTimer ??= setTimeout(() => terminate(child, "SIGKILL"), 15_000);
    killTimer.unref();
  };
  const failEvent = (error: unknown) => {
    if (eventFailed) return;
    eventFailed = true;
    eventError = error;
    stop();
  };
  log.on("error", failEvent);
  options.signal.addEventListener("abort", stop, { once: true });
  child.stderr?.pipe(log, { end: false });
  const lines = createInterface({ input: child.stdout! });
  lines.on("line", (line) => {
    log.write(line + "\n");
    if (line.length > 64 * 1024) return;
    let event: PipelineEvent;
    try {
      event = JSON.parse(line);
    } catch {
      return;
    }
    if (!event || typeof event.type !== "string") return;
    // Serialize event persistence and checkpoint archival; finalization waits for all.
    pending = pending.then(async () => {
      // Lines may already be queued when archival fails. Never persist later
      // progress or completion events after the first failed durable operation.
      if (eventFailed) return;
      try {
        await options.onEvent(event);
      } catch (error) {
        failEvent(error);
      }
    });
  });
  let processError: unknown;
  let processFailed = false;
  try {
    await new Promise<void>((resolve, reject) => {
      child.once("error", reject);
      child.once("close", (code, signal) =>
        code === 0
          ? resolve()
          : reject(
              new Error(
                `Pipeline exited ${code ?? signal}. See retained logs.`,
              ),
            ),
      );
    });
  } catch (error) {
    processFailed = true;
    processError = error;
  } finally {
    // A failing callback usually terminates the child. Drain callbacks before
    // choosing the error so its original cause survives that secondary exit.
    await pending;
    options.signal.removeEventListener("abort", stop);
    lines.close();
    log.end();
    await finished(log).catch(failEvent);
    // A leader can exit before a descendant. Keep addressing its process group
    // during cancellation rather than considering leader exit proof of cleanup.
    if (options.signal.aborted || eventFailed) terminate(child, "SIGKILL");
    if (killTimer) clearTimeout(killTimer);
  }
  if (eventFailed) throw eventError;
  options.signal.throwIfAborted();
  if (processFailed) throw processError;
}

function terminate(child: ChildProcess, signal: NodeJS.Signals) {
  if (!child.pid) return;
  try {
    if (process.platform === "win32") child.kill(signal);
    else process.kill(-child.pid, signal);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error;
  }
}
