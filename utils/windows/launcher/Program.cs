using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;

namespace AseStudio.Launcher
{
    internal static class Program
    {
        // Per-user (not "Global\"): each Windows user gets their own WSL
        // distro and workspace, so two different users running ASE Studio
        // at once on the same machine (fast user switching, RDP) is fine;
        // only a second instance for the *same* user would fight over the
        // same backend port and WSL distro.
        private const string SingleInstanceMutexName = "ASEStudio.Launcher.SingleInstance";

        [DllImport("user32.dll")]
        private static extern IntPtr FindWindow(string lpClassName, string lpWindowName);
        [DllImport("user32.dll")]
        private static extern bool SetForegroundWindow(IntPtr hWnd);
        [DllImport("user32.dll")]
        private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
        private const int SW_RESTORE = 9;

        private static string LogPath => Path.Combine(
            Environment.GetEnvironmentVariable("LOCALAPPDATA") ?? Path.GetTempPath(),
            "ASE Studio", "crash.log");

        private static void LogCrash(Exception exception)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(LogPath));
                File.WriteAllText(LogPath, exception?.ToString() ?? "(unknown exception)");
            }
            catch (Exception) { /* best effort; still show the dialog below */ }
        }

        [STAThread]
        private static void Main()
        {
            // Holds the mutex for the process lifetime; releasing/disposing
            // it early would let a second instance slip in.
            using (var singleInstance = new Mutex(true, SingleInstanceMutexName, out bool createdNew))
            {
                if (!createdNew)
                {
                    ActivateRunningInstance();
                    return;
                }

                Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
                Application.ThreadException += (sender, e) =>
                {
                    LogCrash(e.Exception);
                    MessageBox.Show(e.Exception.ToString(), "ASE Studio", MessageBoxButtons.OK, MessageBoxIcon.Error);
                };
                AppDomain.CurrentDomain.UnhandledException += (sender, e) =>
                    LogCrash(e.ExceptionObject as Exception);
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.Run(new MainForm());
            }
        }

        // ASE Studio only ever starts one backend (hence one WSL distro
        // instance) per user: a second launch brings the running window to
        // the front instead of starting a second wsl.exe/start.sh, which
        // would otherwise fight over the same port and workspace.
        private static void ActivateRunningInstance()
        {
            IntPtr window = FindWindow(null, "ASE Studio");
            if (window != IntPtr.Zero)
            {
                ShowWindow(window, SW_RESTORE);
                SetForegroundWindow(window);
            }
        }
    }
}
