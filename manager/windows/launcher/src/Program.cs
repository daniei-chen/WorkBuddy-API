using System;
using System.IO;
using System.Threading;
using System.Windows.Forms;

namespace WorkBuddyLauncher
{
    /// 安装器 / 启动器 / 卸载器的统一入口。
    /// 同一个 exe 复制成不同名字后承担不同角色：安装包（任意位置）→ 安装模式；
    /// 安装目录里的 WorkBuddyManager.exe → 控制模式；Uninstall.exe → 卸载模式。
    internal static class Program
    {
        [STAThread]
        private static void Main()
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            // 卸载模式：安装目录里的 Uninstall.exe（按文件名判定）
            string exeName = Path.GetFileName(Application.ExecutablePath);
            if (exeName.StartsWith("Uninstall", StringComparison.OrdinalIgnoreCase))
            {
                Application.Run(new UninstallForm());
                return;
            }

            string exeDir = Path.GetDirectoryName(Application.ExecutablePath);

            // 控制模式：exe 所在目录就是一个安装好的项目（单实例）
            if (LauncherPaths.LooksInstalled(exeDir))
            {
                bool createdNew = false;
                using (new Mutex(true, @"Local\WorkBuddyManagerLauncher", out createdNew))
                {
                    if (!createdNew)
                    {
                        MessageBox.Show("WorkBuddy Manager 控制窗口已经在运行中。", LauncherPaths.AppName,
                            MessageBoxButtons.OK, MessageBoxIcon.Information);
                        return;
                    }
                    Application.Run(new MainForm(exeDir));
                }
                return;
            }

            // 安装模式：从任意位置双击的安装包
            Application.Run(new InstallerForm());
        }
    }
}
