using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace Guerrilla.Worker;

internal sealed class WorkerProcess(Profile profile, string? entrypoint = null)
{
    private Process? process;
    private Task? outputPump;
    private Task? errorPump;
    private Task? observation;
    internal bool Running => process is { HasExited: false };
    internal string Status { get; private set; } = "Stopped";
    internal event Action? Changed;

    internal ProcessStartInfo CreateStartInfo(params string[] arguments)
    {
        var root = Path.Combine(AppContext.BaseDirectory, "runtime");
        var start = new ProcessStartInfo(Path.Combine(root, "node.exe"))
        {
            UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = root,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true
        };
        start.ArgumentList.Add(entrypoint ?? Path.Combine(root, "worker.mjs"));
        foreach (var argument in arguments) start.ArgumentList.Add(argument);
        // No inherited platform, bucket or provider credentials reach the agent.
        var inherited = new HashSet<string>(StringComparer.OrdinalIgnoreCase) {
            "PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
            "PROGRAMFILES", "PROGRAMFILES(X86)", "CUDA_PATH", "CUDA_HOME", "VK_ICD_FILENAMES", "TORCH_HOME", "GPU_LOCK_PATH",
            "WORKER_AGENT_LOCK_DIRECTORY", "PYTHONPATH", "NVIDIA_DRIVER_CAPABILITIES",
            "SPLAT_TRANSFORM_BIN", "SPLAT_TRANSFORM_GPU", "SPLAT_TRANSFORM_GPU_BACKEND", "NODE_BIN"
        };
        foreach (var key in start.Environment.Keys.ToArray()) if (!inherited.Contains(key)) start.Environment.Remove(key);
        foreach (var (key, value) in profile.Settings) start.Environment[key] = value;
        start.Environment["PIPELINE_ROOT"] = Path.Combine(root, "pipeline");
        start.Environment["WORKER_MODE"] = "native-development";
        start.Environment["WORKER_CREDENTIAL_FILE"] = profile.Credentials;
        start.Environment["WORKER_SCRATCH"] = Path.Combine(profile.DirectoryPath, "scratch");
        start.Environment["WORKER_STOP_FILE"] = profile.StopFile;
        start.Environment["WORKER_TRAY_PID"] = Environment.ProcessId.ToString();
        return start;
    }

    internal async Task<string> Probe(string? token = null)
    {
        if (Running) throw new InvalidOperationException("Stop the worker before checking dependencies or enrolling.");
        File.Delete(profile.StopFile);
        using var child = Process.Start(CreateStartInfo(token == null ? ["--preflight"] : ["enroll", "--token-stdin"]))
            ?? throw new InvalidOperationException("Could not start the worker runtime.");
        var stdout = child.StandardOutput.ReadToEndAsync();
        var stderr = child.StandardError.ReadToEndAsync();
        if (token != null) await child.StandardInput.WriteLineAsync(token);
        child.StandardInput.Close();
        using var timeout = new CancellationTokenSource(TimeSpan.FromMinutes(3));
        try { await child.WaitForExitAsync(timeout.Token); }
        catch (OperationCanceledException)
        {
            child.Kill(entireProcessTree: true);
            await child.WaitForExitAsync();
            throw new TimeoutException("Dependency check timed out. Check the selected engine and Python paths.");
        }
        var output = await stdout;
        var errors = await stderr;
        if (token != null) errors = errors.Replace(token, "[redacted]", StringComparison.Ordinal);
        if (child.ExitCode != 0) throw new InvalidOperationException(errors.Trim());
        if (token != null) return "Enrolled successfully.";
        using var report = JsonDocument.Parse(output);
        if (!report.RootElement.GetProperty("healthy").GetBoolean())
            throw new InvalidOperationException("No working engine found. Verify the installed engines, plugins, and Python dependencies.");
        return "Ready: " + string.Join(", ", report.RootElement.GetProperty("engines").EnumerateObject().Select(p => p.Name)) +
            " — " + report.RootElement.GetProperty("gpus")[0].GetProperty("name").GetString();
    }

    internal void Start()
    {
        if (Running) return;
        if (observation is { IsCompleted: false }) throw new InvalidOperationException("The previous worker is finishing shutdown. Try again shortly.");
        if (!profile.Enrolled) throw new InvalidOperationException("Enroll this worker first.");
        File.Delete(profile.StopFile);
        process?.Dispose();
        process = Process.Start(CreateStartInfo()) ?? throw new InvalidOperationException("Could not start the worker.");
        process.StandardInput.Close();
        var stamp = DateTime.UtcNow.ToString("yyyyMMdd-HHmmss-fff");
        outputPump = Pump(process.StandardOutput, Path.Combine(profile.DirectoryPath, stamp + ".stdout.log"));
        errorPump = Pump(process.StandardError, Path.Combine(profile.DirectoryPath, stamp + ".stderr.log"));
        Status = "Running · check the server for job status";
        Changed?.Invoke();
        observation = Observe(process);
    }

    private async Task Observe(Process child)
    {
        await child.WaitForExitAsync();
        if (outputPump != null && errorPump != null) await Task.WhenAll(outputPump, errorPump);
        Status = child.ExitCode == 0 ? "Stopped" : "Worker exited · open logs for details";
        Changed?.Invoke();
    }

    private static async Task Pump(StreamReader input, string path)
    {
        await using var writer = new StreamWriter(path, false, Encoding.UTF8) { AutoFlush = true };
        while (await input.ReadLineAsync() is { } line) await writer.WriteLineAsync(line);
    }

    internal async Task Stop()
    {
        if (!Running) return;
        Status = "Stopping…";
        Changed?.Invoke();
        await File.WriteAllTextAsync(profile.StopFile, "stop");
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(30));
        try { await process!.WaitForExitAsync(timeout.Token); }
        catch (OperationCanceledException) { throw new TimeoutException("The worker is still stopping. Check its logs and try again; saved checkpoints have not been removed."); }
        if (outputPump != null && errorPump != null) await Task.WhenAll(outputPump, errorPump);
        if (observation != null) await observation;
        Status = "Stopped";
        Changed?.Invoke();
    }
}
