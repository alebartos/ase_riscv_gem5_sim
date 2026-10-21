using System;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace AseStudio.Launcher
{
    /// <summary>Main window of ASE Studio for Windows: a native WebView2
    /// window around the backend running inside WSL2. Equivalent of
    /// utils/macOS/launcher.m (WKWebView/NSTask) for Windows.</summary>
    public sealed class MainForm : Form
    {
        private const string Title = "ASE Studio";
        private const int DefaultPort = 8765;

        private readonly Label _status = new Label
        {
            Dock = DockStyle.Fill,
            TextAlign = System.Drawing.ContentAlignment.MiddleCenter,
            Font = new System.Drawing.Font("Segoe UI", 11),
            ForeColor = System.Drawing.Color.Gray,
        };
        private WebView2 _web;
        private CoreWebView2Environment _environment;
        private Process _backend;
        private string _serverUrl;
        private bool _closeConfirmed;

        public MainForm()
        {
            Text = Title;
            // The frontend wants real estate close to 1400x900 (same as the
            // macOS launcher's NSMakeRect) and overlaps/misreflows if given
            // much less. Rather than size relative to Screen.WorkingArea
            // (unreliable across DPI scaling/multi-monitor setups), just
            // maximize: it always gives the most room the display has.
            Width = 1400;
            Height = 900;
            MinimumSize = new System.Drawing.Size(900, 600);
            StartPosition = FormStartPosition.CenterScreen;
            WindowState = FormWindowState.Maximized;
            Controls.Add(_status);
            _status.Text = "Avvio di ASE Studio...";
            Load += OnLoadAsync;
            FormClosing += OnFormClosingAsync;
        }

        private static string ProjectPath =>
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "ASE Studio");

        private static string RootfsTarPath =>
            Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "rootfs", "ase-studio-rootfs.tar");

        // ------------------------------------------------------------ startup
        private async void OnLoadAsync(object sender, EventArgs e)
        {
            try
            {
                if (!await EnsureWebView2RuntimeAsync()) { Close(); return; }
                if (!await EnsureWslReadyAsync()) { Close(); return; }
                await StartBackendAndNavigateAsync();
            }
            catch (Exception ex)
            {
                Fatal($"Avvio non riuscito: {ex.Message}");
            }
        }

        private async Task<bool> EnsureWebView2RuntimeAsync()
        {
            try
            {
                CoreWebView2Environment.GetAvailableBrowserVersionString();
                return true;
            }
            catch (WebView2RuntimeNotFoundException)
            {
                var result = MessageBox.Show(
                    "ASE Studio richiede il componente Microsoft Edge WebView2, che non è installato.\n\n" +
                    "Vuoi aprire la pagina di download?",
                    Title, MessageBoxButtons.YesNo, MessageBoxIcon.Warning);
                if (result == DialogResult.Yes)
                {
                    Process.Start(new ProcessStartInfo(
                        "https://developer.microsoft.com/microsoft-edge/webview2/") { UseShellExecute = true });
                }
                return await Task.FromResult(false);
            }
        }

        private async Task<bool> EnsureWslReadyAsync()
        {
            if (!WslManager.IsWslAvailable())
            {
                var result = MessageBox.Show(
                    "ASE Studio richiede il Sottosistema Windows per Linux (WSL), non ancora installato.\n\n" +
                    "Verrà richiesta l'autorizzazione di amministratore per installarlo; " +
                    "al termine sarà necessario riavviare il PC.",
                    Title, MessageBoxButtons.OKCancel, MessageBoxIcon.Information);
                if (result != DialogResult.OK) return false;
                _status.Text = "Installazione di WSL in corso...";
                bool ok = await Task.Run(() => WslManager.InstallWslFeature());
                if (!ok)
                {
                    Fatal("Impossibile installare WSL (autorizzazione negata o errore).");
                    return false;
                }
                MessageBox.Show("WSL è stato installato. Riavvia il PC e riapri ASE Studio per continuare.",
                    Title, MessageBoxButtons.OK, MessageBoxIcon.Information);
                return false;
            }

            if (!WslManager.IsDistroInstalled())
            {
                if (!File.Exists(RootfsTarPath))
                {
                    Fatal($"File dell'app mancante: {RootfsTarPath}");
                    return false;
                }
                _status.Text = "Preparazione del primo avvio (può richiedere qualche minuto)...";
                var progress = new Progress<string>(message => _status.Text = message);
                var result = await WslManager.ImportDistroAsync(RootfsTarPath, progress);
                if (result.ExitCode != 0)
                {
                    Fatal($"Impossibile creare l'ambiente di ASE Studio:\n{result.StdErr}");
                    return false;
                }
            }
            return true;
        }

        private async Task StartBackendAndNavigateAsync()
        {
            Directory.CreateDirectory(ProjectPath);
            _backend = WslManager.StartBackend(DefaultPort, ProjectPath);

            var output = new StringBuilder();
            var urlFound = new TaskCompletionSource<string>();
            _backend.OutputDataReceived += (s, e) => OnBackendLine(e.Data, output, urlFound);
            _backend.ErrorDataReceived += (s, e) => OnBackendLine(e.Data, output, urlFound);
            _backend.Exited += (s, e) => OnBackendExited(output, urlFound);
            _backend.EnableRaisingEvents = true;
            _backend.BeginOutputReadLine();
            _backend.BeginErrorReadLine();

            var timeout = Task.Delay(TimeSpan.FromSeconds(60));
            var completed = await Task.WhenAny(urlFound.Task, timeout);
            if (completed != urlFound.Task)
            {
                Fatal($"Il server di ASE Studio non si è avviato in tempo.\n\n{output}");
                return;
            }
            _serverUrl = await urlFound.Task;
            await InitializeWebViewAsync(_serverUrl);
        }

        private void OnBackendLine(string line, StringBuilder output, TaskCompletionSource<string> urlFound)
        {
            if (line == null) return;
            output.AppendLine(line);
            var match = Regex.Match(line, @"http://127\.0\.0\.1:\d+");
            if (match.Success) urlFound.TrySetResult(match.Value);
        }

        private void OnBackendExited(StringBuilder output, TaskCompletionSource<string> urlFound)
        {
            urlFound.TrySetResult(null);
            if (_closeConfirmed) return;
            BeginInvoke(new Action(() =>
            {
                if (_closeConfirmed) return;
                string tail = output.Length > 1500 ? output.ToString(output.Length - 1500, 1500) : output.ToString();
                Fatal($"Il server di ASE Studio si è fermato (codice {_backend.ExitCode}).\n\n{tail}");
            }));
        }

        // ------------------------------------------------------------ WebView2
        private async Task InitializeWebViewAsync(string url)
        {
            if (url == null) { Fatal("Il server di ASE Studio non ha restituito un indirizzo."); return; }

            string userData = Path.Combine(
                Environment.GetEnvironmentVariable("LOCALAPPDATA") ?? Path.GetTempPath(),
                "ASE Studio", "WebView2");
            Directory.CreateDirectory(userData);
            _environment = await CoreWebView2Environment.CreateAsync(userDataFolder: userData);

            _web = new WebView2 { Dock = DockStyle.Fill };
            Controls.Add(_web);
            await _web.EnsureCoreWebView2Async(_environment);
            HookWebView(_web.CoreWebView2);
            _status.Visible = false;
            _web.CoreWebView2.Navigate(url);
        }

        private void HookWebView(CoreWebView2 core)
        {
            core.NewWindowRequested += OnNewWindowRequested;
            core.NavigationStarting += OnNavigationStarting;
            core.DownloadStarting += OnDownloadStarting;
        }

        // window.open(): ASE Studio's own pages (e.g. the memory pop-out)
        // open in a window of the app sharing this environment, so
        // BroadcastChannel keeps them in sync; everything else goes to the
        // default browser.
        private void OnNewWindowRequested(object sender, CoreWebView2NewWindowRequestedEventArgs e)
        {
            if (!IsLocal(e.Uri))
            {
                Process.Start(new ProcessStartInfo(e.Uri) { UseShellExecute = true });
                e.Handled = true;
                return;
            }
            e.Handled = true;
            var deferral = e.GetDeferral();
            var popup = new PopupForm((int)e.WindowFeatures.Width, (int)e.WindowFeatures.Height);
            popup.Show();
            popup.Web.EnsureCoreWebView2Async(_environment).ContinueWith(_ =>
            {
                popup.BeginInvoke(new Action(() =>
                {
                    HookWebView(popup.Web.CoreWebView2);
                    e.NewWindow = popup.Web.CoreWebView2;
                    deferral.Complete();
                }));
            }, TaskScheduler.FromCurrentSynchronizationContext());
        }

        private void OnNavigationStarting(object sender, CoreWebView2NavigationStartingEventArgs e)
        {
            if (e.IsUserInitiated && !IsLocal(e.Uri))
            {
                e.Cancel = true;
                Process.Start(new ProcessStartInfo(e.Uri) { UseShellExecute = true });
            }
        }

        private void OnDownloadStarting(object sender, CoreWebView2DownloadStartingEventArgs e)
        {
            using (var dialog = new SaveFileDialog
            {
                FileName = Path.GetFileName(e.ResultFilePath),
                InitialDirectory = Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
            })
            {
                e.Handled = true;
                if (dialog.ShowDialog(this) == DialogResult.OK)
                    e.ResultFilePath = dialog.FileName;
                else
                    e.Cancel = true;
            }
        }

        private static bool IsLocal(string uri) =>
            Uri.TryCreate(uri, UriKind.Absolute, out var parsed) && parsed.Host == "127.0.0.1";

        // ------------------------------------------------------------ shutdown
        private async void OnFormClosingAsync(object sender, FormClosingEventArgs e)
        {
            if (_closeConfirmed) return;
            e.Cancel = true;

            bool dirty = false;
            if (_web?.CoreWebView2 != null)
            {
                try
                {
                    string result = await _web.CoreWebView2.ExecuteScriptAsync(
                        "!!(window.aseHasUnsavedChanges && window.aseHasUnsavedChanges())");
                    dirty = result == "true";
                }
                catch (Exception) { /* page not ready / navigated away: nothing to lose */ }
            }
            if (dirty)
            {
                var choice = MessageBox.Show(
                    "Se chiudi ASE Studio ora, le modifiche al sorgente andranno perse.",
                    "Ci sono modifiche non salvate", MessageBoxButtons.OKCancel, MessageBoxIcon.Warning);
                if (choice != DialogResult.OK) return;
            }

            _closeConfirmed = true;
            await ShutdownBackendAsync();
            Close();
        }

        private async Task ShutdownBackendAsync()
        {
            if (_serverUrl != null)
            {
                try
                {
                    using (var client = new HttpClient { Timeout = TimeSpan.FromSeconds(3) })
                        await client.PostAsync(_serverUrl.TrimEnd('/') + "/api/shutdown", null);
                }
                catch (Exception) { /* fall through to the hard stop below */ }
            }
            await Task.Run(() =>
            {
                try
                {
                    if (_backend != null && !_backend.HasExited) _backend.WaitForExit(2000);
                }
                catch (Exception) { /* ignored */ }
                if (_backend == null || _backend.HasExited) return;
                WslManager.TerminateDistro();
            });
        }

        private void Fatal(string message)
        {
            if (InvokeRequired) { BeginInvoke(new Action(() => Fatal(message))); return; }
            MessageBox.Show(message, Title, MessageBoxButtons.OK, MessageBoxIcon.Error);
            _closeConfirmed = true;
            Close();
        }
    }
}
