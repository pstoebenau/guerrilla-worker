use anyhow::{bail, ensure, Context, Result};
use fs2::FileExt;
use std::{
    fs::{self, File},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread,
    time::{Duration, Instant},
};

static INTERRUPTED: AtomicBool = AtomicBool::new(false);

pub struct Runtime {
    pub interrupted: Arc<AtomicBool>,
    parent: Option<u32>,
    pub lock: Option<File>,
}
impl Runtime {
    pub fn new() -> Result<Self> {
        let interrupted = Arc::new(AtomicBool::new(false));
        let flag = interrupted.clone();
        ctrlc::set_handler(move || {
            flag.store(true, Ordering::SeqCst);
            INTERRUPTED.store(true, Ordering::SeqCst);
        })?;
        Ok(Self {
            interrupted,
            parent: std::env::var("WORKER_PARENT_PID")
                .ok()
                .and_then(|p| p.parse().ok()),
            lock: None,
        })
    }
    pub fn check(&self) -> Result<()> {
        ensure!(
            !self.interrupted.load(Ordering::SeqCst),
            "Runner interrupted"
        );
        if let Some(parent) = self.parent {
            ensure!(parent_alive(parent), "Worker parent exited");
        }
        Ok(())
    }
    pub fn lock_gpu(&mut self) -> Result<()> {
        let home = std::env::var("HOME")
            .or_else(|_| std::env::var("USERPROFILE"))
            .unwrap_or_else(|_| ".".into());
        let path = std::env::var("GPU_LOCK_PATH")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from(home).join(".cache/guerrilla-worker/gpu.lock"));
        fs::create_dir_all(path.parent().context("GPU lock has no parent")?)?;
        let file = File::options()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(path)?;
        let wait = std::env::var("GPU_LOCK_WAIT").as_deref() == Ok("1");
        let mut announced = false;
        loop {
            self.check()?;
            match file.try_lock_exclusive() {
                Ok(()) => break,
                Err(error)
                    if wait
                        && error.raw_os_error() == fs2::lock_contended_error().raw_os_error() =>
                {
                    if !announced {
                        crate::emit(
                            "progress",
                            serde_json::json!({"message":"Waiting for another worker to release the GPU"}),
                        );
                        announced = true;
                    }
                    thread::sleep(Duration::from_millis(250));
                }
                Err(error) => bail!("GPU still owned by another runner: {error}"),
            }
        }
        #[cfg(unix)]
        {
            use std::os::fd::AsRawFd;
            unsafe {
                libc::fcntl(file.as_raw_fd(), libc::F_SETFD, 0);
            }
            std::env::set_var("PIPELINE_GPU_LOCK_FD", file.as_raw_fd().to_string());
        }
        self.lock = Some(file);
        Ok(())
    }
}

pub struct ManagedChild {
    pub child: Child,
    #[cfg(windows)]
    job: WindowsJob,
}
impl ManagedChild {
    pub fn spawn(arguments: &[String], log: &Path) -> Result<Self> {
        ensure!(!arguments.is_empty(), "Empty command");
        fs::create_dir_all(log.parent().context("Log has no parent")?)?;
        let file = File::create(log)?;
        // A tiny Rust guardian detects abrupt runner death, including on macOS.
        let mut command = Command::new(std::env::current_exe()?);
        command
            .args(["--guard-parent", &std::process::id().to_string(), "--"])
            .args(arguments);
        command
            .stdout(Stdio::from(file.try_clone()?))
            .stderr(Stdio::from(file));
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            unsafe {
                command.pre_exec(|| {
                    if libc::setsid() == -1 {
                        return Err(std::io::Error::last_os_error());
                    }
                    Ok(())
                });
            }
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000004);
        }
        #[allow(unused_mut)]
        let mut child = command
            .spawn()
            .with_context(|| format!("Starting {}", arguments[0]))?;
        #[cfg(windows)]
        let job = match WindowsJob::new(&child) {
            Ok(job) => job,
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
        };
        #[cfg(windows)]
        if let Err(error) = windows_suspend(child.id(), true).and_then(|resumed| {
            ensure!(
                resumed,
                "Engine exited before its contained launch could resume"
            );
            Ok(())
        }) {
            job.terminate();
            let _ = child.wait();
            return Err(error);
        }
        Ok(Self {
            child,
            #[cfg(windows)]
            job,
        })
    }
    pub fn terminate(&mut self) {
        #[cfg(unix)]
        unsafe {
            libc::kill(-(self.child.id() as i32), libc::SIGTERM);
        }
        #[cfg(windows)]
        self.job.terminate();
        let deadline = Instant::now() + Duration::from_secs(3);
        while Instant::now() < deadline {
            let exited = self.child.try_wait().ok().flatten().is_some();
            #[cfg(unix)]
            let contained_alive = unsafe { libc::kill(-(self.child.id() as i32), 0) == 0 };
            #[cfg(windows)]
            let contained_alive = !exited;
            if exited && !contained_alive {
                return;
            }
            thread::sleep(Duration::from_millis(50));
        }
        #[cfg(unix)]
        unsafe {
            libc::kill(-(self.child.id() as i32), libc::SIGKILL);
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
    pub fn freeze(&mut self) -> Result<Frozen> {
        Frozen::new(self.child.id())
    }
}
impl Drop for ManagedChild {
    fn drop(&mut self) {
        self.terminate();
    }
}

#[cfg(windows)]
pub fn pid_exists(pid: u32) -> bool {
    #[cfg(unix)]
    {
        unsafe {
            libc::kill(pid as i32, 0) == 0
                || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
        }
    }
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{
            GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
        };
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return false;
        }
        let mut code = 0;
        let ok = GetExitCodeProcess(handle, &mut code) != 0 && code == 259;
        CloseHandle(handle);
        ok
    }
}

fn parent_alive(parent: u32) -> bool {
    #[cfg(unix)]
    {
        unsafe { libc::getppid() == parent as i32 }
    }
    #[cfg(windows)]
    {
        pid_exists(parent)
    }
}

pub fn guard(parent: u32, arguments: &[String]) -> Result<()> {
    ensure!(parent_alive(parent), "Runner exited before engine launch");
    let runtime = Runtime::new()?;
    let mut child = Command::new(arguments.first().context("Missing guarded command")?)
        .args(&arguments[1..])
        .spawn()?;
    loop {
        if !parent_alive(parent) || runtime.interrupted.load(Ordering::SeqCst) {
            #[cfg(unix)]
            unsafe {
                libc::kill(0, libc::SIGTERM);
            }
            #[cfg(windows)]
            {
                let _ = Command::new("taskkill")
                    .args(["/PID", &child.id().to_string(), "/T", "/F"])
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status();
            }
            let deadline = Instant::now() + Duration::from_secs(2);
            while Instant::now() < deadline {
                if child.try_wait()?.is_some() {
                    return Ok(());
                }
                thread::sleep(Duration::from_millis(50));
            }
            #[cfg(unix)]
            unsafe {
                libc::kill(0, libc::SIGKILL);
            }
            let _ = child.kill();
            let _ = child.wait();
            bail!("Runner exited");
        }
        if let Some(status) = child.try_wait()? {
            #[cfg(unix)]
            unsafe {
                libc::kill(0, libc::SIGTERM);
            }
            ensure!(status.success(), "Engine exited {status}");
            return Ok(());
        }
        thread::sleep(Duration::from_millis(100));
    }
}

pub struct Frozen {
    pids: Vec<u32>,
}
impl Frozen {
    fn new(pid: u32) -> Result<Self> {
        #[cfg(unix)]
        {
            let stopped = unsafe { libc::kill(-(pid as i32), libc::SIGSTOP) == 0 };
            if !stopped {
                let error = std::io::Error::last_os_error();
                // An exiting group can leave only zombies on Darwin. Accept
                // a failed stop only after proving that no live writer remains.
                ensure!(
                    group_members(pid)?
                        .iter()
                        .all(|(_, state)| state.starts_with('Z')),
                    "Cannot freeze engine group: {error}"
                );
            }
            let result = Self { pids: vec![pid] };
            // SIGSTOP completion is checked for all members, not just supervisor.
            let deadline = Instant::now() + Duration::from_secs(5);
            loop {
                let members = group_members(pid)?;
                if members
                    .iter()
                    .all(|(_, state)| state.starts_with('T') || state.starts_with('Z'))
                {
                    return Ok(result);
                }
                ensure!(
                    Instant::now() < deadline,
                    "Could not freeze every engine thread"
                );
                thread::sleep(Duration::from_millis(10));
            }
        }
        #[cfg(windows)]
        {
            let mut result = Self { pids: vec![] };
            let mut exited = vec![];
            if windows_suspend(pid, false)? {
                result.pids.push(pid);
            } else {
                exited.push(pid);
            }
            let deadline = Instant::now() + Duration::from_secs(5);
            loop {
                let descendants = windows_descendants(pid)?;
                let missing = descendants
                    .into_iter()
                    .filter(|pid| !result.pids.contains(pid) && !exited.contains(pid))
                    .collect::<Vec<_>>();
                if missing.is_empty() {
                    return Ok(result);
                }
                ensure!(
                    Instant::now() < deadline,
                    "Could not freeze every engine process"
                );
                for pid in missing {
                    if windows_suspend(pid, false)? {
                        result.pids.push(pid);
                    } else {
                        exited.push(pid);
                    }
                }
            }
        }
    }
    pub fn open_paths(&self) -> Result<Vec<PathBuf>> {
        #[cfg(target_os = "linux")]
        {
            let mut result = Vec::new();
            for (pid, state) in group_members(self.pids[0])? {
                // A zombie has released its descriptors and cannot write. Its
                // stopped parent cannot reap it until this guard resumes.
                if state.starts_with('Z') {
                    continue;
                }
                for file in fs::read_dir(format!("/proc/{pid}/fd"))? {
                    let file = file?;
                    if let Ok(target) = fs::read_link(file.path()) {
                        if target.is_absolute() {
                            result.push(target);
                        }
                    }
                }
            }
            Ok(result)
        }
        #[cfg(target_os = "macos")]
        {
            let pids = group_members(self.pids[0])?
                .iter()
                .filter(|(_, state)| !state.starts_with('Z'))
                .map(|(pid, _)| pid.to_string())
                .collect::<Vec<_>>()
                .join(",");
            if pids.is_empty() {
                return Ok(vec![]);
            }
            let output = Command::new("/usr/sbin/lsof")
                .args(["-F", "n", "-p", &pids])
                .output()?;
            ensure!(
                output.status.success(),
                "Cannot inspect frozen engine open files"
            );
            Ok(String::from_utf8_lossy(&output.stdout)
                .lines()
                .filter_map(|line| {
                    line.strip_prefix("n/")
                        .map(|path| PathBuf::from(format!("/{path}")))
                })
                .collect())
        }
        #[cfg(windows)]
        {
            // Spirula checkpoint reads deny Windows write sharing and validate
            // complete tar framing while this native suspension guard is held.
            Ok(vec![])
        }
    }
}
impl Drop for Frozen {
    fn drop(&mut self) {
        #[cfg(unix)]
        unsafe {
            libc::kill(-(self.pids[0] as i32), libc::SIGCONT);
        }
        #[cfg(windows)]
        for pid in self.pids.iter().rev() {
            let _ = windows_suspend(*pid, true);
        }
    }
}
#[cfg(target_os = "linux")]
fn group_members(group: u32) -> Result<Vec<(u32, String)>> {
    let mut result = vec![];
    for entry in fs::read_dir("/proc")? {
        let entry = entry?;
        let Some(pid) = entry
            .file_name()
            .to_str()
            .and_then(|name| name.parse::<u32>().ok())
        else {
            continue;
        };
        let Ok(stat) = fs::read_to_string(entry.path().join("stat")) else {
            continue;
        };
        let Some(end) = stat.rfind(')') else { continue };
        let fields = stat[end + 1..].split_whitespace().collect::<Vec<_>>();
        if fields.len() >= 3 && fields[2].parse::<u32>().ok() == Some(group) {
            result.push((pid, fields[0].to_string()));
        }
    }
    Ok(result)
}
#[cfg(target_os = "macos")]
fn group_members(group: u32) -> Result<Vec<(u32, String)>> {
    let output = Command::new("ps")
        .args(["-eo", "pid=,pgid=,stat="])
        .output()?;
    ensure!(
        output.status.success(),
        "Cannot inspect engine process group"
    );
    Ok(String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(|line| {
            let fields: Vec<_> = line.split_whitespace().collect();
            if fields.len() != 3 || fields[1].parse::<u32>().ok()? != group {
                return None;
            }
            Some((fields[0].parse().ok()?, fields[2].to_owned()))
        })
        .collect())
}

#[cfg(windows)]
struct WindowsJob(windows_sys::Win32::Foundation::HANDLE);
#[cfg(windows)]
impl WindowsJob {
    fn new(child: &Child) -> Result<Self> {
        unsafe {
            use std::os::windows::io::AsRawHandle;
            use windows_sys::Win32::System::JobObjects::*;
            let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            ensure!(!handle.is_null(), "Cannot create engine containment job");
            let result = Self(handle);
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            ensure!(
                SetInformationJobObject(
                    handle,
                    JobObjectExtendedLimitInformation,
                    &limits as *const _ as _,
                    std::mem::size_of_val(&limits) as u32
                ) != 0,
                "Cannot set engine containment limits"
            );
            ensure!(
                AssignProcessToJobObject(handle, child.as_raw_handle() as _) != 0,
                "Cannot contain engine"
            );
            Ok(result)
        }
    }
    fn terminate(&self) {
        unsafe {
            windows_sys::Win32::System::JobObjects::TerminateJobObject(self.0, 1);
        }
    }
}
#[cfg(windows)]
impl Drop for WindowsJob {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.0);
        }
    }
}
#[cfg(windows)]
fn windows_descendants(root: u32) -> Result<Vec<u32>> {
    unsafe {
        use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
        use windows_sys::Win32::System::Diagnostics::ToolHelp::*;
        let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        ensure!(
            snapshot != INVALID_HANDLE_VALUE,
            "Cannot enumerate engine processes"
        );
        let mut entry: PROCESSENTRY32W = std::mem::zeroed();
        entry.dwSize = std::mem::size_of_val(&entry) as u32;
        let mut entries = Vec::new();
        let mut more = Process32FirstW(snapshot, &mut entry);
        while more != 0 {
            entries.push((entry.th32ProcessID, entry.th32ParentProcessID));
            more = Process32NextW(snapshot, &mut entry);
        }
        CloseHandle(snapshot);
        let mut result = vec![root];
        loop {
            let old = result.len();
            for (pid, parent) in &entries {
                if result.contains(parent) && !result.contains(pid) {
                    result.push(*pid);
                }
            }
            if old == result.len() {
                break;
            }
        }
        Ok(result)
    }
}
#[cfg(windows)]
// True means successfully stopped/resumed; false only means confirmed exited.
fn windows_suspend(pid: u32, resume: bool) -> Result<bool> {
    unsafe {
        use windows_sys::Win32::Foundation::{
            CloseHandle, GetLastError, ERROR_INVALID_PARAMETER, HANDLE, WAIT_OBJECT_0,
        };
        use windows_sys::Win32::System::Threading::{
            OpenProcess, WaitForSingleObject, PROCESS_QUERY_LIMITED_INFORMATION,
            PROCESS_SUSPEND_RESUME, PROCESS_SYNCHRONIZE,
        };
        #[link(name = "ntdll")]
        extern "system" {
            fn NtSuspendProcess(process: HANDLE) -> i32;
            fn NtResumeProcess(process: HANDLE) -> i32;
        }
        let process = OpenProcess(
            PROCESS_SUSPEND_RESUME | PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
            0,
            pid,
        );
        if process.is_null() {
            // ERROR_INVALID_PARAMETER for this previously observed positive PID
            // means its process has already disappeared. Access failures remain
            // errors and must never be interpreted as a safe dead process.
            ensure!(
                GetLastError() == ERROR_INVALID_PARAMETER,
                "Cannot open engine process for suspension"
            );
            return Ok(false);
        }
        if WaitForSingleObject(process, 0) == WAIT_OBJECT_0 {
            CloseHandle(process);
            return Ok(false);
        }
        let status = if resume {
            NtResumeProcess(process)
        } else {
            NtSuspendProcess(process)
        };
        // A successfully resumed short command may already have exited by the
        // next scheduling turn. Its successful resume remains a valid launch.
        if resume && status == 0 {
            CloseHandle(process);
            return Ok(true);
        }

        // A process can exit after opening its handle but before suspension.
        // Only its signalled termination handle proves no threads can write.
        let exited =
            WaitForSingleObject(process, if status == 0 { 0 } else { 100 }) == WAIT_OBJECT_0;
        CloseHandle(process);
        if exited {
            return Ok(false);
        }
        ensure!(
            status == 0,
            "Cannot suspend/resume live engine process (NT status {status:#x})"
        );
        Ok(true)
    }
}

pub fn run_capture(arguments: &[String], timeout: Duration) -> Result<String> {
    capture(arguments, timeout, false)
}
pub fn capture_unchecked(arguments: &[String], timeout: Duration) -> Result<String> {
    capture(arguments, timeout, true)
}
fn capture(arguments: &[String], timeout: Duration, allow_failure: bool) -> Result<String> {
    let log = std::env::temp_dir().join(format!("guerrilla-probe-{}.log", uuid::Uuid::new_v4()));
    let mut child = ManagedChild::spawn(arguments, &log)?;
    let deadline = Instant::now() + timeout;
    let result = loop {
        if INTERRUPTED.load(Ordering::SeqCst) {
            break Err(anyhow::anyhow!(
                "Runner interrupted during dependency probe"
            ));
        }
        if std::env::var("WORKER_PARENT_PID")
            .ok()
            .and_then(|value| value.parse::<u32>().ok())
            .is_some_and(|parent| !parent_alive(parent))
        {
            break Err(anyhow::anyhow!(
                "Worker parent exited during dependency probe"
            ));
        }
        if let Some(status) = child.child.try_wait()? {
            let text = fs::read_to_string(&log)?;
            if !status.success() && !allow_failure {
                break Err(anyhow::anyhow!(
                    "Command failed: {}",
                    text.chars()
                        .rev()
                        .take(2000)
                        .collect::<String>()
                        .chars()
                        .rev()
                        .collect::<String>()
                ));
            }
            break Ok(text);
        }
        if Instant::now() >= deadline {
            child.terminate();
            break Err(anyhow::anyhow!("Probe timed out"));
        }
        thread::sleep(Duration::from_millis(100));
    };
    let _ = fs::remove_file(log);
    result
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader};
    use std::os::unix::process::CommandExt;

    #[test]
    fn freeze_accepts_zombie_members_without_accepting_live_writers() {
        // Deliberately retain an exited child: freezing its parent prevents
        // wait/reaping, so requiring every member to be T can never succeed.
        let script="import os,time; pid=os.fork(); os._exit(0) if pid==0 else None; time.sleep(.1); print('ready',flush=True); time.sleep(60)";
        let mut command = Command::new("python3");
        command.args(["-c", script]).stdout(Stdio::piped());
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut child = command.spawn().unwrap();
        let mut ready = String::new();
        BufReader::new(child.stdout.take().unwrap())
            .read_line(&mut ready)
            .unwrap();
        assert_eq!(ready.trim(), "ready");
        let frozen = Frozen::new(child.id());
        let result = frozen
            .as_ref()
            .map_err(|error| error.to_string())
            .and_then(|guard| guard.open_paths().map_err(|error| error.to_string()));
        let states = group_members(child.id()).unwrap();
        unsafe {
            libc::kill(-(child.id() as i32), libc::SIGKILL);
        }
        let _ = child.wait();
        assert!(result.is_ok(), "{result:?}; group states: {states:?}");
        assert!(
            states.iter().any(|(_, state)| state.starts_with('Z')),
            "Fixture must exercise zombie membership: {states:?}"
        );
        assert!(
            states
                .iter()
                .filter(|(_, state)| !state.starts_with('Z'))
                .all(|(_, state)| state.starts_with('T')),
            "Live group members must be stopped: {states:?}"
        );
    }

    #[test]
    fn freeze_handles_group_exit_between_status_poll_and_stop() {
        let mut command = Command::new("python3");
        command.args(["-c", "pass"]);
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut child = command.spawn().unwrap();
        assert!(child.wait().unwrap().success());
        let frozen = Frozen::new(child.id()).unwrap();
        assert!(frozen.open_paths().unwrap().is_empty());
    }
}
