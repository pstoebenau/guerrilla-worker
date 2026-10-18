# Platform prerequisites

Native packages target Windows x64, Ubuntu 24.04 x64 and macOS 14 or later on
Apple Silicon. Windows/Linux require a Vulkan-capable GPU and current vendor
drivers. macOS uses MoltenVK. Other OS/architecture combinations need separate
validation; Intel Mac, Windows ARM and Linux ARM packages are not provided.

Spirula and its RoMaV2 model are managed automatically. Setup can install them
before a scan; initial setup requires internet. Incomplete installations are
never selected. Profiles, credentials, scratch and model caches live outside
the application bundle. The operating-system GPU driver remains external.

Native execution does not require Python, CUDA, a COLMAP installation or a
densification plugin. Node and FFmpeg are bundled for converter support.

The Linux container requires an NVIDIA driver and NVIDIA Container Toolkit
with working Vulkan. GPU acceptance must run on the actual hardware; package
and CPU tests do not establish scan compatibility.

macOS packages use ad-hoc signing and require copying the app out of the DMG.
Apple Developer ID signing and notarization require separately configured
credentials. Default builds do not remove quarantine or disable Gatekeeper.
