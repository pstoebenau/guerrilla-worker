using System.Diagnostics;

namespace Guerrilla.Worker;

internal sealed class SettingsForm : Form
{
    private readonly Profile profile;
    private readonly WorkerProcess worker;
    private readonly Dictionary<string, TextBox> fields = [];
    private readonly CheckBox autoStart = new() { Text = "Start at Windows sign-in", AutoSize = true, Margin = new Padding(0, 10, 0, 10) };
    private readonly TextBox token = new() { UseSystemPasswordChar = true, Dock = DockStyle.Fill, PlaceholderText = "Paste a token from Workers on your server", MaxLength = 4096 };
    private readonly Label status = new() { AutoSize = true, MaximumSize = new Size(620, 0), Margin = new Padding(0, 14, 0, 0) };
    private readonly List<Button> actions = [];
    private readonly Button enroll = new() { Text = "Enroll and start", AutoSize = true };
    private readonly Button disconnect = new() { Text = "Disconnect…", AutoSize = true };

    internal SettingsForm(Profile profile, WorkerProcess worker, Action<Func<Task>> run)
    {
        this.profile = profile;
        this.worker = worker;
        SuspendLayout();
        Text = "Guerrilla Worker";
        Font = new Font("Segoe UI", 10);
        ClientSize = new Size(710, 655);
        MinimumSize = new Size(710, 655);
        StartPosition = FormStartPosition.CenterScreen;
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(24), ColumnCount = 3, AutoScroll = true };
        layout.SuspendLayout();
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 160));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 85));
        Controls.Add(layout);
        void Wide(Control control) { var row = layout.RowCount++; layout.RowStyles.Add(new RowStyle(SizeType.AutoSize)); layout.Controls.Add(control, 0, row); layout.SetColumnSpan(control, 3); }
        Wide(new Label { Text = "Your GPU. Your worker.", Font = new Font(Font, FontStyle.Bold), AutoSize = true, Margin = new Padding(0, 0, 0, 8) });
        Wide(new Label { Text = "Enroll once, then let Guerrilla run in your system tray. Closing this window keeps the worker running.", AutoSize = true, MaximumSize = new Size(620, 0), Margin = new Padding(0, 0, 0, 18) });
        foreach (var (key, title) in new[] { ("WORKER_CONTROL_URL", "Server"), ("PYTHON", "Python executable"), ("SPIRULA_BIN", "Spirula executable"),
                     ("LICHTFELD_BIN", "LichtFeld executable"), ("LICHTFELD_DENSIFICATION_PLUGIN", "Densification folder") })
        {
            var row = layout.RowCount++;
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.Controls.Add(new Label { Text = title, AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(0, 8, 4, 8) }, 0, row);
            var field = new TextBox { Text = key == "WORKER_CONTROL_URL" ? profile.Server : profile.Settings.GetValueOrDefault(key, ""), Dock = DockStyle.Fill, Margin = new Padding(0, 5, 6, 5), AccessibleName = title };
            fields[key] = field;
            layout.Controls.Add(field, 1, row);
            if (key == "WORKER_CONTROL_URL") { layout.SetColumnSpan(field, 2); continue; }
            var browse = new Button { Text = "Browse…", AutoSize = true, Dock = DockStyle.Fill, Margin = new Padding(0, 4, 0, 4) };
            browse.Click += (_, _) => {
                if (key.EndsWith("PLUGIN")) { using var dialog = new FolderBrowserDialog(); if (dialog.ShowDialog(this) == DialogResult.OK) field.Text = dialog.SelectedPath; }
                else { using var dialog = new OpenFileDialog { Filter = "Executables (*.exe)|*.exe", CheckFileExists = true }; if (dialog.ShowDialog(this) == DialogResult.OK) field.Text = dialog.FileName; }
            };
            actions.Add(browse);
            layout.Controls.Add(browse, 2, row);
        }
        autoStart.Checked = profile.AutoStart;
        Wide(autoStart);
        Wide(new Label { Text = "Use your existing engine installations and a Python environment with the pipeline dependencies. FFmpeg and NVIDIA tools must be on PATH.", AutoSize = true, MaximumSize = new Size(620, 0), ForeColor = SystemColors.GrayText });
        var controls = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill, Margin = new Padding(0, 12, 0, 12) };
        var save = new Button { Text = "Save settings", AutoSize = true };
        var check = new Button { Text = "Check dependencies", AutoSize = true };
        save.Click += (_, _) => run(() => { Save(); SetStatus("Settings saved."); return Task.CompletedTask; });
        check.Click += (_, _) => run(async () => { Save(); SetStatus("Checking GPU and engines…"); SetStatus(await worker.Probe()); });
        controls.Controls.AddRange([save, check]);
        actions.AddRange([save, check]);
        Wide(controls);
        Wide(new Label { Text = "Enrollment token", AutoSize = true, Margin = new Padding(0, 4, 0, 5) });
        Wide(token);
        var enrollment = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill, Margin = new Padding(0, 10, 0, 0) };
        var website = new Button { Text = "Open server", AutoSize = true };
        website.Click += (_, _) => run(() => { Process.Start(new ProcessStartInfo(Profile.NormalizeServer(fields["WORKER_CONTROL_URL"].Text)) { UseShellExecute = true }); return Task.CompletedTask; });
        enroll.Click += (_, _) => run(async () => {
            Save();
            var secret = token.Text.Trim();
            token.Clear();
            if (secret.Length == 0) throw new ArgumentException("Paste an enrollment token from the server's Workers page.");
            SetStatus("Checking dependencies and enrolling…");
            await worker.Probe(secret);
            SetStatus("Enrolled. The worker is starting in the background.");
            worker.Start();
        });
        disconnect.Click += (_, _) => run(async () => {
            if (MessageBox.Show(this, "Stop and remove this computer's saved enrollment? Checkpoints will remain. You can revoke the old worker on the server's Workers page.", "Disconnect worker", MessageBoxButtons.OKCancel) != DialogResult.OK) return;
            await worker.Stop();
            File.Delete(profile.Credentials);
            SetStatus("Disconnected. Choose a server and enroll again when ready.");
        });
        enrollment.Controls.AddRange([enroll, website, disconnect]);
        Wide(enrollment);
        Wide(status);
        SetStatus(profile.Enrolled ? "Enrolled. Use the tray menu to start or stop the worker." : "Not enrolled. Check your dependencies, then paste an enrollment token.");
        FormClosing += (_, e) => { if (e.CloseReason == CloseReason.UserClosing) { e.Cancel = true; token.Clear(); Hide(); } };
        AutoScaleDimensions = new SizeF(96, 96);
        AutoScaleMode = AutoScaleMode.Dpi;
        layout.ResumeLayout(false);
        ResumeLayout(true);
    }

    private void Save() => profile.Save(fields.ToDictionary(p => p.Key, p => p.Value.Text), autoStart.Checked);
    internal void SetStatus(string message) => status.Text = message;
    internal void SetBusy(bool busy, bool running)
    {
        foreach (var action in actions) action.Enabled = !busy && !running;
        foreach (var field in fields.Values) field.Enabled = !busy && !running;
        fields["WORKER_CONTROL_URL"].Enabled = !busy && !running && !profile.Enrolled;
        autoStart.Enabled = !busy && !running;
        token.Enabled = !busy && !profile.Enrolled;
        enroll.Enabled = !busy && !profile.Enrolled;
        disconnect.Enabled = !busy && profile.Enrolled;
        UseWaitCursor = busy;
    }
}
