# Windows installation

Download the Windows x64 setup EXE and run it normally. The installer includes
WebView2 and signed app-local Microsoft runtime libraries. No system Python,
Node, CUDA toolkit or separate scan engine installation is required.

Open Setup and choose **Install / repair runtime** to download verified Spirula
and reconstruction weights (about 1.2 GB). A Vulkan-capable GPU and its vendor
driver are required. Paste a token from the server's Workers page, choose
**Enroll worker**, then **Start worker**. Enrollment can precede GPU setup.

Stop the worker before repairing its runtime. Closing the window keeps it in
the tray. Uninstall removes app-owned files and preserves the worker profile,
credentials, model cache and checkpoints.

Build the installer natively on Windows with Bun, Rust, Python 3, licensed
Microsoft build tools, and Tauri prerequisites. `bun run build` prepares the
native binaries, Node/FFmpeg converter dependencies, app-local CRT and single
setup EXE. Packaging does not enroll the computer or start a scan.
