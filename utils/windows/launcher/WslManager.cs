using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;

namespace AseStudio.Launcher
{
    /// <summary>
    /// Everything the launcher needs from WSL2: checking it is installed,
    /// importing the "ASEStudio" distro from the bundled rootfs on first run,
    /// and starting/stopping the backend inside it. Mirrors what
    /// utils/macOS/launcher.m does with NSTask, for a WSL2 process instead of
    /// a local one.
    /// </summary>
    internal static class WslManager
    {
        public const string DistroName = "ASEStudio";

        public static bool IsWslAvailable()
        {
            // "wsl --status" fails outright (missing/old wsl.exe, or the
            // Windows feature not enabled) when WSL cannot run at all.
            var result = Run("wsl.exe", "--status");
            return result.ExitCode == 0;
        }

        public static bool IsDistroInstalled()
        {
            // wsl -l -q writes UTF-16LE, including on redirected output.
            var result = Run("wsl.exe", "-l -q", Encoding.Unicode);
            if (result.ExitCode != 0) return false;
            return result.StdOut.Split('\r', '\n')
                .Select(line => line.Trim().Trim('\0'))
                .Any(line => string.Equals(line, DistroName, StringComparison.OrdinalIgnoreCase));
        }

        /// <summary>Enables the WSL2 Windows feature. Requires elevation (UAC);
        /// returns false if the user declines the prompt.</summary>
        public static bool InstallWslFeature()
        {
            try
            {
                var psi = new ProcessStartInfo("wsl.exe", "--install --no-distribution")
                {
                    UseShellExecute = true,
                    Verb = "runas",
                    WindowStyle = ProcessWindowStyle.Hidden,
                };
                using (var process = Process.Start(psi))
                {
                    process.WaitForExit();
                    return process.ExitCode == 0;
                }
            }
            catch (System.ComponentModel.Win32Exception)
            {
                return false;   // UAC prompt declined
            }
        }

        private static string InstallDir => Path.Combine(
            Environment.GetEnvironmentVariable("LOCALAPPDATA") ?? "", "ASE Studio", "wsl");

        // Records the sha256 of the rootfs tar actually imported, next to it
        // (not inside the distro: cheap to read, and survives re-registering
        // the distro under the same name). Used to tell a stale distro from
        // an up-to-date one across app updates -- see EnsureDistroUpToDateAsync.
        private static string ImportedRootfsMarkerPath =>
            Path.Combine(InstallDir, ".rootfs-sha256");

        private static string ComputeSha256(string filePath)
        {
            using (var sha256 = SHA256.Create())
            using (var stream = File.OpenRead(filePath))
                return BitConverter.ToString(sha256.ComputeHash(stream)).Replace("-", "");
        }

        /// <summary>Imports the ASEStudio distro from the rootfs tar bundled
        /// with the installer if it is missing, or replaces it in place if a
        /// newer app version bundled a different rootfs (critical fixes,
        /// e.g. a broken gem5) -- never leaves two imports side by side, nor
        /// silently keeps running a stale one. Tries WSL2 first, falls back
        /// to WSL1 if the machine cannot run WSL2 (e.g. virtualization
        /// disabled).</summary>
        public static async Task<RunResult> EnsureDistroUpToDateAsync(string rootfsTarPath,
            IProgress<string> progress = null)
        {
            // Hashing a 1GB+ tar on the caller's (UI) thread would stall it.
            string currentHash = await Task.Run(() => ComputeSha256(rootfsTarPath));
            bool installed = IsDistroInstalled();
            string importedHash = null;
            try { importedHash = File.ReadAllText(ImportedRootfsMarkerPath).Trim(); }
            catch (IOException) { } catch (UnauthorizedAccessException) { }

            if (installed && importedHash == currentHash)
                return new RunResult { ExitCode = 0 };

            if (installed)
            {
                progress?.Report("Aggiornamento dell'ambiente di ASE Studio...");
                UnregisterDistro();
            }

            Directory.CreateDirectory(InstallDir);
            progress?.Report("Installazione della distribuzione ASEStudio (WSL2)...");
            var result = await RunAsync("wsl.exe",
                $"--import {DistroName} \"{InstallDir}\" \"{rootfsTarPath}\" --version 2");
            if (result.ExitCode != 0)
            {
                progress?.Report("WSL2 non disponibile, provo con WSL1...");
                result = await RunAsync("wsl.exe",
                    $"--import {DistroName} \"{InstallDir}\" \"{rootfsTarPath}\" --version 1");
            }
            if (result.ExitCode == 0)
                File.WriteAllText(ImportedRootfsMarkerPath, currentHash);
            return result;
        }

        public static void UnregisterDistro()
        {
            Run("wsl.exe", $"--unregister {DistroName}");
            try { File.Delete(ImportedRootfsMarkerPath); } catch (IOException) { } catch (UnauthorizedAccessException) { }
        }

        /// <summary>Starts the backend inside WSL and returns the running
        /// process with redirected output (caller reads lines for the
        /// "ASE Studio: http://127.0.0.1:PORT" announcement, as the native
        /// macOS window does with launcher.sh).</summary>
        public static Process StartBackend(int port, string windowsProjectPath)
        {
            // .NET Framework 4.8 has no ProcessStartInfo.ArgumentList: quote by hand.
            string args = $"-d {DistroName} -u ase -- /opt/ase/start.sh {port} \"{windowsProjectPath}\"";
            var psi = new ProcessStartInfo("wsl.exe", args)
            {
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
            return Process.Start(psi);
        }

        /// <summary>Stops everything running in the ASEStudio distro
        /// (used as a fallback if the backend does not exit itself after
        /// /api/shutdown).</summary>
        public static void TerminateDistro()
        {
            Run("wsl.exe", $"--terminate {DistroName}");
        }

        public struct RunResult
        {
            public int ExitCode;
            public string StdOut;
            public string StdErr;
        }

        private static RunResult Run(string exe, string args, Encoding encoding = null)
        {
            var psi = new ProcessStartInfo(exe, args)
            {
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
                StandardOutputEncoding = encoding,
                StandardErrorEncoding = encoding,
            };
            using (var process = Process.Start(psi))
            {
                string stdout = process.StandardOutput.ReadToEnd();
                string stderr = process.StandardError.ReadToEnd();
                process.WaitForExit();
                return new RunResult { ExitCode = process.ExitCode, StdOut = stdout, StdErr = stderr };
            }
        }

        private static Task<RunResult> RunAsync(string exe, string args, Encoding encoding = null) =>
            Task.Run(() => Run(exe, args, encoding));
    }
}
