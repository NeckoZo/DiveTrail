using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32;

internal static class PortableLauncher
{
    private const string WebView2Key =
        @"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}";

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBox(IntPtr window, string message, string title, uint type);

    private static bool HasWebView2()
    {
        foreach (RegistryHive hive in new[] { RegistryHive.CurrentUser, RegistryHive.LocalMachine })
        {
            foreach (RegistryView view in new[] { RegistryView.Registry32, RegistryView.Registry64 })
            {
                using (RegistryKey root = RegistryKey.OpenBaseKey(hive, view))
                using (RegistryKey key = root.OpenSubKey(WebView2Key))
                {
                    Version version;
                    if (key != null && Version.TryParse(key.GetValue("pv") as string, out version)
                        && version > new Version(0, 0, 0, 0))
                        return true;
                }
            }
        }
        return false;
    }

    [STAThread]
    private static void Main(string[] args)
    {
        try
        {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            string runtime = Path.Combine(root, "runtime", "pythonw.exe");
            #if INSTALLED
            string entry = Path.Combine(root, "matchlog-entry.py");
            const string mode = "--installed";
            const string missingFiles = "尾迹的安装文件不完整，请重新运行 DiveTrail 安装程序。";
            #else
            string entry = Path.Combine(root, "portable-entry.py");
            const string mode = "--portable";
            const string missingFiles = "请先解压整个 ZIP 文件，再运行文件夹里的 DiveTrail.exe。";
            #endif
            if (!File.Exists(runtime) || !File.Exists(entry))
                throw new InvalidOperationException(missingFiles);
            if (!HasWebView2())
                throw new InvalidOperationException("请从微软官网安装 WebView2 Evergreen Runtime，然后重新打开尾迹。");
            var start = new ProcessStartInfo(runtime, "-B -X utf8 \"" + entry + "\" " + mode);
            if (Array.IndexOf(args, "--startup") >= 0)
                start.Arguments += " --startup";
            start.WorkingDirectory = root;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            Process.Start(start);
        }
        catch (Exception error)
        {
            MessageBox(IntPtr.Zero, error.Message, "DiveTrail · 尾迹", 0x10);
        }
    }
}
