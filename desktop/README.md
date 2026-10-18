# Worker desktop development

From the worker root, run `bun scripts/setup.ts`, then `bun run dev`.
Build native installers with `bun run build` on the target OS. The app manages
the Rust worker and downloads the pinned Spirula runtime and model on demand.

For isolated manual testing, set `GUERRILLA_WORKER_DATA_DIR` to an absolute
folder outside your normal profile before launching the app. It must be below
a filesystem root without parent traversal. Isolation preserves the normal
profile and login registration; leave login startup disabled for test profiles.

`bun run check` and `bun test tests` in this directory validate the Svelte UI.
Use the target OS's actual GPU for complete scan acceptance. Package compilation
and browser preview cannot establish a running or connected worker.
