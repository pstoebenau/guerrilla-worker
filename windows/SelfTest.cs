using System.Text.Json;

namespace Guerrilla.Worker;

internal static class SelfTest
{
    internal static int Run()
    {
        var directory = Path.Combine(Path.GetTempPath(), "guerrilla-tray-test-" + Guid.NewGuid().ToString("N"));
        try
        {
            var profile = new Profile(directory);
            if (profile.Server != Profile.DefaultServer || !profile.AutoStart) throw new Exception("Defaults failed");
            var values = new Dictionary<string, string> { ["PYTHON"] = Environment.ProcessPath!, ["WORKER_CONTROL_URL"] = "http://localhost:23456/" };
            profile.Save(values, false, registerStartup: false);
            var legacy = new Dictionary<string, string>(profile.Settings) { ["LICHTFELD_COLMAP_PLUGIN"] = directory };
            File.WriteAllText(Path.Combine(directory, "config.json"), JsonSerializer.Serialize(legacy));
            profile = new Profile(directory);
            if (profile.Settings.ContainsKey("LICHTFELD_COLMAP_PLUGIN")) throw new Exception("Legacy COLMAP setting was not migrated");
            if (profile.Server != "http://localhost:23456" || profile.AutoStart) throw new Exception("Settings persistence failed");
            foreach (var invalid in new[] { "file:///C:/secret", "https://user:secret@example.com", "https://example.com/?token=secret", "not a URL" })
            {
                try { Profile.NormalizeServer(invalid); throw new Exception("Invalid URL accepted"); } catch (ArgumentException) { }
            }
            File.WriteAllText(profile.Credentials, JsonSerializer.Serialize(new { origin = "https://guerrilla.dad", credential = "test-only" }));
            try { profile.Save(values, false, false); throw new Exception("Enrolled server changed"); } catch (InvalidOperationException) { }
            Environment.SetEnvironmentVariable("WORKER_ENROLLMENT_TOKEN", "test-only");
            Environment.SetEnvironmentVariable("AWS_SECRET_ACCESS_KEY", "test-only");
            var start = new WorkerProcess(profile).CreateStartInfo("enroll", "--token-stdin");
            if (start.Environment.ContainsKey("WORKER_ENROLLMENT_TOKEN") || start.Environment.ContainsKey("AWS_SECRET_ACCESS_KEY")) throw new Exception("Secret environment leaked");
            if (!start.RedirectStandardInput || !start.CreateNoWindow || !start.ArgumentList.Contains("--token-stdin")) throw new Exception("Unsafe enrollment launch");
            File.Delete(profile.Credentials);
            var stub = Path.Combine(directory, "agent-stub.mjs");
            File.WriteAllText(stub, """
                import fs from 'node:fs';
                if (process.argv.includes('--preflight')) {
                  console.log(JSON.stringify({healthy:true,engines:{spirula:'test'},gpus:[{name:'Test GPU'}]}));
                } else if (process.argv.includes('enroll')) {
                  let token='';
                  for await (const chunk of process.stdin) token += chunk;
                  if (token.trim() !== 'stdin-test-token') process.exit(2);
                  fs.writeFileSync(process.env.WORKER_CREDENTIAL_FILE, JSON.stringify({origin:process.env.WORKER_CONTROL_URL,credential:'test-only'}));
                  console.log('Enrolled');
                } else {
                  console.log('Started');
                  setInterval(() => { if(fs.existsSync(process.env.WORKER_STOP_FILE)) process.exit(0); }, 50);
                }
                """);
            var testWorker = new WorkerProcess(profile, stub);
            if (!testWorker.Probe().GetAwaiter().GetResult().Contains("Test GPU")) throw new Exception("Preflight output not parsed");
            testWorker.Probe("stdin-test-token").GetAwaiter().GetResult();
            if (!profile.Enrolled) throw new Exception("Stdin enrollment failed");
            testWorker.Start();
            testWorker.Start(); // Starting twice must not create a second worker.
            testWorker.Stop().GetAwaiter().GetResult();
            if (testWorker.Running) throw new Exception("Graceful stop failed");
            testWorker.Start();
            testWorker.Stop().GetAwaiter().GetResult();
            if (Directory.GetFiles(directory, "*.log").Any(f => File.ReadAllText(f).Contains("stdin-test-token"))) throw new Exception("Enrollment token leaked to logs");
            File.WriteAllText(Path.Combine(directory, "result.txt"), "PASS: defaults, persisted overrides, URL validation, enrollment binding, isolated process environment");
            return 0;
        }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
        finally
        {
            // Only this test's freshly generated direct children; no recursive cleanup.
            foreach (var file in Directory.GetFiles(directory)) File.Delete(file);
            Directory.Delete(directory);
        }
    }
}
