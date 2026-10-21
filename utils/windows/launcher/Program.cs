using System;
using System.IO;
using System.Windows.Forms;

namespace AseStudio.Launcher
{
    internal static class Program
    {
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
}
