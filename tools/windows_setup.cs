using System;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;
using Microsoft.Win32;

internal static class WindowsSetup
{
    private const string AppName = "DiveTrail";
    private const string WindowTitle = "DiveTrail · 尾迹";
    private const string Marker = ".divetrail-install";

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBox(IntPtr window, string message, string title, uint type);

    [STAThread]
    private static int Main()
    {
        try
        {
            string programs = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                           "Programs");
            string target = Path.Combine(programs, AppName);
            var assembly = Assembly.GetExecutingAssembly();
            var version = assembly.GetName().Version;
            if (MessageBox(IntPtr.Zero, "安装或升级 " + WindowTitle + " " + version.ToString(3) + "？\n\n" +
                           "程序目录：" + target + "\n记录保存在 Windows 的「文档」文件夹。", WindowTitle, 0x21) != 1)
                return 0;

            bool available;
            using (var marker = new Mutex(false, @"Local\AftermatchDesktop", out available))
            {
                if (!available)
                    throw new InvalidOperationException("请完全退出正在运行的尾迹或旧版程序，再重新安装。");
                using (Stream payload = assembly.GetManifestResourceStream("MatchLogPayload"))
                    Install(payload, target, version);
            }

            string message = "安装完成。可从开始菜单打开 DiveTrail。";
            try { CreateStartShortcut(target); }
            catch (Exception)
            {
                message = "安装完成。请打开以下文件启动尾迹：\n" + Path.Combine(target, AppName + ".exe");
            }
            try { UpdateStartup(programs, target); }
            catch (Exception)
            {
                message += "\n\n未能更新登录自启，请在尾迹的设置里重新打开。";
            }
            MessageBox(IntPtr.Zero, message, WindowTitle, 0x40);
            return 0;
        }
        catch (Exception error)
        {
            MessageBox(IntPtr.Zero, error.Message, WindowTitle, 0x10);
            return 1;
        }
    }

    private static Version CheckInstallation(string folder)
    {
        if (!Directory.Exists(folder) && !File.Exists(folder))
            return null;
        string marker = Path.Combine(folder, Marker);
        Version version;
        if ((File.GetAttributes(folder) & FileAttributes.ReparsePoint) != 0 || !File.Exists(marker) ||
            (File.GetAttributes(marker) & FileAttributes.ReparsePoint) != 0 ||
            !Version.TryParse(File.ReadAllText(marker), out version))
            throw new InvalidOperationException("此位置已被其他文件使用，安装程序无法替换：\n" + folder);
        return version;
    }

    private static void Install(Stream payload, string target, Version version)
    {
        if (payload == null)
            throw new InvalidDataException("安装程序缺少文件，请重新下载。");
        target = Path.GetFullPath(target);
        string previous = target + ".previous";
        Version current = CheckInstallation(target);
        if (CheckInstallation(previous) != null)
        {
            if (current == null)
            {
                Directory.Move(previous, target);
                current = CheckInstallation(target);
            }
            else
                Retire(previous);
        }
        if (current != null && current > version)
            throw new InvalidOperationException("电脑上已经安装了更新版本的尾迹。");

        string staging = target + ".new-" + Guid.NewGuid().ToString("N");
        Directory.CreateDirectory(staging);
        try
        {
            Extract(payload, staging);
            if (!File.Exists(Path.Combine(staging, AppName + ".exe")) ||
                !File.Exists(Path.Combine(staging, "runtime", "pythonw.exe")))
                throw new InvalidDataException("安装包缺少必要文件，请重新下载。");
            File.WriteAllText(Path.Combine(staging, Marker), version.ToString());

            if (current != null)
                Directory.Move(target, previous);
            try { Directory.Move(staging, target); }
            catch
            {
                if (current != null)
                    Directory.Move(previous, target);
                throw;
            }
            if (current != null)
            {
                try { Retire(previous); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
        }
        finally { TryDelete(staging); }
    }

    private static void Extract(Stream payload, string staging)
    {
        string prefix = staging + Path.DirectorySeparatorChar;
        using (var archive = new ZipArchive(payload, ZipArchiveMode.Read))
        {
            foreach (var entry in archive.Entries)
            {
                string path = Path.GetFullPath(Path.Combine(staging, entry.FullName));
                if (!path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) || entry.FullName.Contains(":"))
                    throw new InvalidDataException("安装包中的文件路径无效。");
                Directory.CreateDirectory(Path.GetDirectoryName(path));
                using (Stream source = entry.Open())
                using (Stream destination = new FileStream(path, FileMode.CreateNew))
                    source.CopyTo(destination);
            }
        }
    }

    private static void Retire(string folder)
    {
        // A failed cleanup must not leave a partially deleted rollback directory.
        string retired = folder + ".retired-" + Guid.NewGuid().ToString("N");
        Directory.Move(folder, retired);
        TryDelete(retired);
    }

    private static void TryDelete(string folder)
    {
        try { Directory.Delete(folder, true); }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }

    private static void CreateStartShortcut(string target)
    {
        string menu = Environment.GetFolderPath(Environment.SpecialFolder.Programs);
        if (String.IsNullOrEmpty(menu))
            throw new DirectoryNotFoundException();
        string shortcutPath = Path.Combine(menu, AppName + ".lnk");
        dynamic shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
        dynamic shortcut = shell.CreateShortcut(shortcutPath);
        shortcut.TargetPath = Path.Combine(target, AppName + ".exe");
        shortcut.WorkingDirectory = target;
        shortcut.IconLocation = Path.Combine(target, AppName + ".exe") + ",0";
        shortcut.Save();
    }

    private static void UpdateStartup(string programs, string target)
    {
        const string valueName = "Aftermatch";
        string oldCommand = "\"" + Path.Combine(programs, "Apex MatchLog", "Apex MatchLog.exe") + "\" --startup";
        string newExecutable = Path.Combine(target, AppName + ".exe");
        string newCommand = (newExecutable.Contains(" ") ? "\"" + newExecutable + "\"" : newExecutable) + " --startup";
        using (RegistryKey key = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run", true))
        {
            string existing = key == null ? null : key.GetValue(valueName) as string;
            if (String.Equals(existing, oldCommand, StringComparison.OrdinalIgnoreCase))
                key.SetValue(valueName, newCommand, RegistryValueKind.String);
        }
    }
}
