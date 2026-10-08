using System.Diagnostics;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text.Json;
using Microsoft.Win32;

namespace Guerrilla.Worker;

internal sealed class Profile
{
    internal const string DefaultServer = "https://guerrilla.dad";
    internal static readonly string[] Keys = ["PYTHON", "SPIRULA_BIN", "LICHTFELD_BIN", "LICHTFELD_DENSIFICATION_PLUGIN", "WORKER_CONTROL_URL"];
    internal string DirectoryPath { get; }
    internal string Credentials => Path.Combine(DirectoryPath, "credentials.json");
    internal string StopFile => Path.Combine(DirectoryPath, "tray.stop");
    internal Dictionary<string, string> Settings { get; private set; }
    internal bool Enrolled => File.Exists(Credentials);
    internal bool AutoStart { get; set; } = true;
    internal string Server
    {
        get
        {
            if (!Enrolled) return Settings.GetValueOrDefault("WORKER_CONTROL_URL", DefaultServer);
            using var credentials = JsonDocument.Parse(File.ReadAllText(Credentials));
            return credentials.RootElement.GetProperty("origin").GetString()!;
        }
    }

    internal Profile(string? directory = null)
    {
        DirectoryPath = directory ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Guerrilla", "worker");
        var info = Directory.CreateDirectory(DirectoryPath);
        var acl = new DirectorySecurity();
        acl.SetAccessRuleProtection(true, false);
        foreach (var sid in new[] { WindowsIdentity.GetCurrent().User!, new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null) })
            acl.AddAccessRule(new FileSystemAccessRule(sid, FileSystemRights.FullControl,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
        info.SetAccessControl(acl);
        var config = Path.Combine(DirectoryPath, "config.json");
        Settings = File.Exists(config)
            ? JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(config)) ?? [] : [];
        Settings.Remove("LICHTFELD_COLMAP_PLUGIN"); // Migrate profiles from the former plugin runtime.
        if (Settings.Keys.Except(Keys).Any()) throw new InvalidDataException("Unknown worker configuration. Check config.json in the worker data folder.");
        Settings.TryAdd("WORKER_CONTROL_URL", DefaultServer);
        var preferences = Path.Combine(DirectoryPath, "tray.json");
        if (File.Exists(preferences)) AutoStart = JsonSerializer.Deserialize<Preferences>(File.ReadAllText(preferences))?.AutoStart ?? true;
        Detect();
    }

    internal void Detect()
    {
        void Candidate(string key, string? value)
        {
            if (!Settings.ContainsKey(key) && !string.IsNullOrWhiteSpace(value) && (File.Exists(value) || Directory.Exists(value)))
                Settings[key] = Path.GetFullPath(value);
        }
        foreach (var key in Keys.Where(k => k != "WORKER_CONTROL_URL")) Candidate(key, Environment.GetEnvironmentVariable(key));
        var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        Candidate("SPIRULA_BIN", Path.Combine(local, "Guerrilla", "runtimes", "spirula-2026.9.30", "spirula.exe"));
        Candidate("LICHTFELD_BIN", Path.Combine(local, "Programs", "LichtFeld Studio", "bin", "LichtFeld-Studio.exe"));
        Candidate("LICHTFELD_DENSIFICATION_PLUGIN", Path.Combine(home, ".lichtfeld", "plugins", "densification"));
        // Portable checkouts often have their Python environment in an ancestor.
        for (var ancestor = new DirectoryInfo(AppContext.BaseDirectory); ancestor != null; ancestor = ancestor.Parent)
            Candidate("PYTHON", Path.Combine(ancestor.FullName, ".venv", "Scripts", "python.exe"));
        foreach (var segment in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator))
            if (!segment.Contains("WindowsApps", StringComparison.OrdinalIgnoreCase)) Candidate("PYTHON", Path.Combine(segment.Trim('"'), "python.exe"));
    }

    internal static string NormalizeServer(string value)
    {
        if (!Uri.TryCreate(value.Trim(), UriKind.Absolute, out var uri) ||
            (uri.Scheme != "http" && uri.Scheme != "https") || uri.UserInfo.Length > 0 || uri.Query.Length > 0 || uri.Fragment.Length > 0)
            throw new ArgumentException("Enter an HTTP(S) server URL without credentials, a query, or a fragment.");
        return uri.AbsoluteUri.TrimEnd('/');
    }

    internal void Save(Dictionary<string, string> values, bool autoStart, bool registerStartup = true)
    {
        var server = NormalizeServer(values.GetValueOrDefault("WORKER_CONTROL_URL", DefaultServer));
        if (Enrolled && server != NormalizeServer(Server))
            throw new InvalidOperationException("Disconnect this worker before changing servers. Enrollment belongs to its original server.");
        var result = new Dictionary<string, string> { ["WORKER_CONTROL_URL"] = server };
        foreach (var key in Keys.Where(k => k != "WORKER_CONTROL_URL"))
        {
            var value = values.GetValueOrDefault(key, "").Trim();
            if (value.Length == 0) continue;
            if (!Path.IsPathFullyQualified(value) || !(key.EndsWith("PLUGIN") ? Directory.Exists(value) : File.Exists(value)))
                throw new ArgumentException($"Choose an existing absolute path for {key}.");
            result[key] = value;
        }
        if (!result.ContainsKey("PYTHON")) throw new ArgumentException("Choose the Python executable with your pipeline dependencies installed.");
        AtomicWrite(Path.Combine(DirectoryPath, "config.json"), result);
        AtomicWrite(Path.Combine(DirectoryPath, "tray.json"), new Preferences(autoStart));
        Settings = result;
        AutoStart = autoStart;
        if (registerStartup) ApplyStartup();
    }

    internal void ApplyStartup()
    {
        using var key = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run");
        if (AutoStart) key.SetValue("GuerrillaWorker", $"\"{Environment.ProcessPath}\" --background");
        else key.DeleteValue("GuerrillaWorker", false);
    }

    internal static void AtomicWrite<T>(string path, T value)
    {
        var temporary = path + ".tmp";
        File.WriteAllText(temporary, JsonSerializer.Serialize(value, new JsonSerializerOptions { WriteIndented = true }));
        File.Move(temporary, path, true);
    }
    private sealed record Preferences(bool AutoStart);
}
