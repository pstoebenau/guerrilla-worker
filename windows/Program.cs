namespace Guerrilla.Worker;

internal static class Program
{
    internal const string MutexName = @"Local\GuerrillaWorkerTray";
    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Contains("--self-test")) return SelfTest.Run();
        ApplicationConfiguration.Initialize();
        using var mutex = new Mutex(true, MutexName, out var owner);
        if (!owner)
        {
            try { using var activation = EventWaitHandle.OpenExisting(@"Local\GuerrillaWorkerActivate"); activation.Set(); }
            catch (WaitHandleCannotBeOpenedException) { }
            return 0;
        }
        try
        {
            using var context = new TrayContext(args.Contains("--background"));
            Application.Run(context);
            return 0;
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "Guerrilla Worker", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        finally { mutex.ReleaseMutex(); }
    }
}
