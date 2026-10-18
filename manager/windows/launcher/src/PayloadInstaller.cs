using System;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Windows.Forms;

namespace WorkBuddyLauncher
{
    /// 内嵌项目包（payload.zip）的解压与安装辅助。
    internal static class PayloadInstaller
    {
        /// 解压内嵌 payload 到目标目录（覆盖同名文件）。
        /// progress 回调参数：已完成条目数、总条目数、当前条目相对路径；可为 null。
        /// collect：可选，把解压出来的相对路径收集起来（安装器据此写安装清单）。
        public static void ExtractPayload(string targetDir, Action<int, int, string> progress)
        {
            ExtractPayload(targetDir, progress, null);
        }

        public static void ExtractPayload(string targetDir, Action<int, int, string> progress,
            System.Collections.Generic.List<string> collect)
        {
            Diag("ExtractPayload 开始");
            using (Stream stream = Assembly.GetExecutingAssembly()
                       .GetManifestResourceStream(LauncherPaths.PayloadResourceName))
            {
                if (stream == null)
                {
                    throw new InvalidOperationException("安装包缺少内嵌项目文件（payload.zip），请用构建脚本重新生成。");
                }
                using (var archive = new ZipArchive(stream, ZipArchiveMode.Read))
                {
                    int total = archive.Entries.Count;
                    Diag("条目总数=" + total);
                    int done = 0;
                    foreach (ZipArchiveEntry entry in archive.Entries)
                    {
                        // PowerShell 5.1 的 ZipFile 生成的条目名以 \ 分隔（.NET Framework 行为），
                        // 目录条目以 \ 结尾 —— 统一成 / 再判断，否则「xx\」会被当作文件名解压卡死
                        string fullName = entry.FullName.Replace('\\', '/');
                        string relative = fullName.Replace('/', Path.DirectorySeparatorChar);
                        string destination = Path.Combine(targetDir, relative);
                        if (fullName.EndsWith("/", StringComparison.Ordinal))
                        {
                            Directory.CreateDirectory(destination);
                        }
                        else
                        {
                            Directory.CreateDirectory(Path.GetDirectoryName(destination));
                            entry.ExtractToFile(destination, true);
                            if (collect != null) { collect.Add(relative); }
                        }
                        done++;
                        // 逐条回调会把 UI 刷爆：按批次节流，最后一条必报
                        if (progress != null && (done % 25 == 0 || done == total))
                        {
                            progress(done, total, relative);
                        }
                    }
                    Diag("循环结束 done=" + done);
                }
                Diag("zip 已释放");
            }
            Diag("ExtractPayload 返回");
        }

        /// 安装过程诊断日志（%TEMP%\wb-install-diag.log），用于排障。
        internal static void Diag(string message)
        {
            try
            {
                File.AppendAllText(Path.Combine(Path.GetTempPath(), "wb-install-diag.log"),
                    DateTime.Now.ToString("HH:mm:ss.fff") + " " + message + Environment.NewLine);
            }
            catch { }
        }

        /// 把自身复制成安装目录里的主程序与卸载程序。
        public static void CopySelf(string installDir)
        {
            string self = Application.ExecutablePath;
            File.Copy(self, Path.Combine(installDir, LauncherPaths.AppExeName), true);
            File.Copy(self, Path.Combine(installDir, LauncherPaths.UninstallExeName), true);
        }

        /// 创建快捷方式（WScript.Shell COM）。
        public static void CreateShortcut(string lnkPath, string targetPath, string workingDir, string description)
        {
            Type shellType = Type.GetTypeFromProgID("WScript.Shell");
            if (shellType == null)
            {
                throw new InvalidOperationException("系统缺少 WScript.Shell，无法创建快捷方式。");
            }
            dynamic shell = Activator.CreateInstance(shellType);
            dynamic shortcut = shell.CreateShortcut(lnkPath);
            shortcut.TargetPath = targetPath;
            shortcut.WorkingDirectory = workingDir;
            shortcut.IconLocation = targetPath + ",0";
            shortcut.Description = description;
            shortcut.Save();
        }
    }
}
