import path from "node:path";
import { constants } from "node:fs";
import {
  lstat,
  readdir,
  readFile,
  mkdir,
  copyFile,
  rm,
} from "node:fs/promises";
import { safeRelativePath } from "./paths";

export function inside(root: string, relative: string) {
  safeRelativePath(relative);
  const base = path.resolve(root);
  const target = path.resolve(base, relative);
  if (base === path.parse(base).root || !target.startsWith(base + path.sep))
    throw new Error("Retention path escapes its scratch directory.");
  return target;
}

export function excluded(relative: string, exclusions: string[]) {
  return exclusions.some(
    (prefix) => relative === prefix || relative.startsWith(prefix + "/"),
  );
}

export async function checkedPath(root: string, relative: string) {
  const target = inside(root, relative);
  let cursor = path.resolve(root);
  if ((await lstat(cursor)).isSymbolicLink())
    throw new Error("Retention cannot traverse a symlink.");
  for (const part of path.relative(cursor, target).split(path.sep)) {
    cursor = path.join(cursor, part);
    try {
      if ((await lstat(cursor)).isSymbolicLink())
        throw new Error("Retention cannot traverse a symlink.");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") break;
      throw error;
    }
  }
  return target;
}

export async function retentionExclusions(root: string): Promise<string[]> {
  let state;
  try {
    state = JSON.parse(
      await readFile(path.join(root, "platform-state.json"), "utf8"),
    );
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw error;
  }
  const paths = state.retention?.excludePaths ?? [];
  if (
    !Array.isArray(paths) ||
    paths.some((p: unknown) => typeof p !== "string")
  )
    throw new Error("Invalid retention exclusions.");
  for (const relative of paths) inside(root, relative);
  return paths;
}

export async function* selectedFiles(
  root: string,
  exclusions: string[] = [],
  relative = "",
): AsyncGenerator<string> {
  if (!relative) {
    const entry = await lstat(root);
    if (!entry.isDirectory() || entry.isSymbolicLink())
      throw new Error("Retention requires a real scratch directory.");
    for (const item of exclusions) inside(root, item);
  }
  for (const entry of await readdir(path.join(root, relative), {
    withFileTypes: true,
  })) {
    if (
      entry.name === ".worker" ||
      entry.name === ".archive-acks" ||
      (!relative && entry.name === "retained-runtime-migrations") ||
      (relative === "logs" && /^worker-\d+(?:-\d+)?\.log$/.test(entry.name))
    )
      continue;
    const name = relative ? `${relative}/${entry.name}` : entry.name;
    if (excluded(name, exclusions)) continue;
    if (entry.isSymbolicLink())
      throw new Error("Retention cannot traverse symlinks.");
    if (entry.isDirectory()) yield* selectedFiles(root, exclusions, name);
    else if (entry.isFile()) yield name;
  }
}

/** Freeze declared stage products only. Never copy live engine scratch as a checkpoint. */
export async function copyStageSnapshot(source:string, destination:string, inheritedPaths = new Set<string>()) {
  const state = JSON.parse(await readFile(path.join(source,'platform-state.json'),'utf8'));
  const names = new Set<string>(['platform-state.json']);
  if (await lstat(path.join(source,'logs/worker.log')).catch(()=>null)) names.add('logs/worker.log');
  for (const entry of Object.values({...state.stageCheckpoints,...state.completed}) as {files?:{path:string}[]}[]) {
    for (const file of entry.files ?? []) names.add(file.path);
  }
  // Native checkpoint metadata may name a directory and sidecars, all declared in stageCheckpoints.
  await mkdir(destination,{recursive:true});
  for (const relative of names) {
    const from = await checkedPath(source,relative);
    const entry = await lstat(from).catch(error=>{if(error.code==='ENOENT')return null;throw error;});
    if (!entry && inheritedPaths.has(relative)) continue;
    if (!entry?.isFile() || entry.isSymbolicLink()) throw new Error(`Missing stage output: ${relative}`);
    const to = await checkedPath(destination,relative);
    await mkdir(path.dirname(to),{recursive:true});
    await copyFile(from,to,constants.COPYFILE_FICLONE);
  }
}

// Every recursive removal goes through an absolute containment and symlink check.
// Callers supply only scan/attempt directories established from database UUIDs.
export async function removeScratchPath(root: string, relative: string) {
  const target = inside(root, relative);
  let cursor = path.resolve(root);
  const rootEntry = await lstat(cursor).catch(error=>{if(error.code==='ENOENT')return null;throw error;});
  if (!rootEntry) return;
  if (rootEntry.isSymbolicLink())
    throw new Error("Retention cannot remove through a symlink.");
  for (const part of path.relative(cursor, target).split(path.sep)) {
    cursor = path.join(cursor, part);
    let entry;
    try {
      entry = await lstat(cursor);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return;
      throw error;
    }
    if (entry.isSymbolicLink())
      throw new Error("Retention cannot remove through a symlink.");
  }
  await rm(target, {
    recursive: true,
    force: true,
    maxRetries: 3,
    retryDelay: 250,
  });
}
