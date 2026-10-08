using System.Diagnostics;

namespace Guerrilla.Worker;

internal sealed class TrayContext : ApplicationContext
{
    private readonly Profile profile = new();
    private readonly WorkerProcess worker;
    private readonly SettingsForm form;
    private readonly NotifyIcon tray;
    private readonly ToolStripMenuItem status = new("Stopped") { Enabled = false };
    private readonly ToolStripMenuItem start = new("Start worker");
    private readonly ToolStripMenuItem stop = new("Stop worker");
    private readonly EventWaitHandle activation = new(false, EventResetMode.AutoReset, @"Local\GuerrillaWorkerActivate");
    private readonly RegisteredWaitHandle activationWait;
    private bool busy;

    internal TrayContext(bool background)
    {
        worker = new WorkerProcess(profile);
        form = new SettingsForm(profile, worker, Run);
        _ = form.Handle;
        var menu = new ContextMenuStrip();
        menu.Items.Add(status);
        menu.Items.Add("Settings and enrollment…", null, (_, _) => ShowSettings());
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(start);
        menu.Items.Add(stop);
        start.Click += (_, _) => Run(() => { worker.Start(); return Task.CompletedTask; });
        stop.Click += (_, _) => Run(worker.Stop);
        menu.Items.Add("Open Guerrilla", null, (_, _) => Run(() => Open(profile.Server)));
        menu.Items.Add("Open logs folder", null, (_, _) => Run(() => Open(profile.DirectoryPath)));
        menu.Items.Add("Download updates…", null, (_, _) => Run(() => Open("https://github.com/pstoebenau/guerrilla-worker/releases/latest")));
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Quit", null, (_, _) => Run(async () => { await worker.Stop(); tray!.Visible = false; ExitThread(); }));
        tray = new NotifyIcon { Icon = SystemIcons.Application, Text = "Guerrilla Worker", ContextMenuStrip = menu, Visible = true };
        tray.DoubleClick += (_, _) => ShowSettings();
        worker.Changed += Refresh;
        activationWait = ThreadPool.RegisterWaitForSingleObject(activation, (_, _) => form.BeginInvoke(ShowSettings), null, Timeout.Infinite, false);
        profile.ApplyStartup();
        if (!background || !profile.Enrolled) ShowSettings();
        if (profile.Enrolled) Run(() => { worker.Start(); return Task.CompletedTask; });
        Refresh();
    }

    private void ShowSettings() { form.Show(); form.WindowState = FormWindowState.Normal; form.Activate(); }
    private static Task Open(string value) { Process.Start(new ProcessStartInfo(value) { UseShellExecute = true }); return Task.CompletedTask; }
    private async void Run(Func<Task> operation)
    {
        if (busy) return;
        busy = true;
        Refresh();
        try { await operation(); }
        catch (Exception error) { form.SetStatus(error.Message); ShowSettings(); }
        finally { busy = false; Refresh(); }
    }
    private void Refresh()
    {
        if (form.InvokeRequired) { form.BeginInvoke(Refresh); return; }
        status.Text = worker.Status;
        start.Enabled = !busy && !worker.Running && profile.Enrolled;
        stop.Enabled = !busy && worker.Running;
        form.SetBusy(busy, worker.Running);
        tray.Text = worker.Running ? "Guerrilla Worker · running" : "Guerrilla Worker · stopped";
    }
    protected override void Dispose(bool disposing)
    {
        if (disposing) { activationWait.Unregister(null); activation.Dispose(); tray.Dispose(); form.Dispose(); }
        base.Dispose(disposing);
    }
}
