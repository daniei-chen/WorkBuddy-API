using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.RegularExpressions;

namespace WorkBuddyLauncher
{
    /// 异步运行 PowerShell 脚本（prepare.ps1 / service-tools.ps1），输出行与退出码通过回调上报。
    internal sealed class ScriptRunner
    {
        private Process _process;

        public bool IsRunning
        {
            get
            {
                try { return _process != null && !_process.HasExited; }
                catch { return false; }
            }
        }

        public void Run(string scriptPath, string[] scriptArgs, Action<string> onLine, Action<int> onExit)
        {
            if (IsRunning) { return; }

            var process = new Process();
            process.StartInfo = BuildStartInfo(scriptPath, scriptArgs, true);
            process.EnableRaisingEvents = true;
            process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e)
            {
                if (e.Data != null && onLine != null) { onLine(e.Data); }
            };
            process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e)
            {
                if (e.Data != null && onLine != null) { onLine(e.Data); }
            };
            process.Exited += delegate
            {
                int code = -1;
                try { code = process.ExitCode; }
                catch { }
                if (onExit != null) { onExit(code); }
            };

            _process = process;
            process.Start();
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
        }

        /// 同步等待脚本结束（卸载等短流程使用），返回退出码。
        public static int RunWait(string scriptPath, string[] scriptArgs)
        {
            using (Process process = Process.Start(BuildStartInfo(scriptPath, scriptArgs, false)))
            {
                if (process == null) { return -1; }
                process.WaitForExit();
                return process.ExitCode;
            }
        }

        private static ProcessStartInfo BuildStartInfo(string scriptPath, string[] scriptArgs, bool redirect)
        {
            var args = new StringBuilder();
            args.Append("-NoProfile -ExecutionPolicy Bypass -File \"").Append(scriptPath).Append("\"");
            if (scriptArgs != null)
            {
                foreach (string arg in scriptArgs) { args.Append(" \"").Append(arg).Append("\""); }
            }

            var psi = new ProcessStartInfo();
            psi.FileName = "powershell.exe";
            psi.Arguments = args.ToString();
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            if (redirect)
            {
                psi.RedirectStandardOutput = true;
                psi.RedirectStandardError = true;
                // Windows PowerShell 5.1 按控制台输出编码写管道（中文 Windows 为 GBK），
                // 用 OEM 代码页解码才能正确显示脚本里的中文输出。
                Encoding oem = Encoding.GetEncoding(CultureInfo.CurrentCulture.TextInfo.OEMCodePage);
                psi.StandardOutputEncoding = oem;
                psi.StandardErrorEncoding = oem;
            }
            return psi;
        }
    }

    /// 通过 /api/healthz 判断面板服务是否已就绪。
    internal static class HealthProbe
    {
        public static bool Check(string connectHost, int port, int timeoutMs)
        {
            try
            {
                var request = (HttpWebRequest)WebRequest.Create(
                    "http://" + connectHost + ":" + port + "/api/healthz");
                request.Timeout = timeoutMs;
                request.Method = "GET";
                using (var response = (HttpWebResponse)request.GetResponse())
                {
                    return response.StatusCode == HttpStatusCode.OK;
                }
            }
            catch
            {
                return false;
            }
        }
    }

    /// 目标端口是否已被占用（用与监听等效的地址试绑定判断）。
    internal static class PortCheck
    {
        public static bool InUse(string connectHost, int port)
        {
            try
            {
                IPAddress addr = connectHost == "0.0.0.0" || connectHost == "::"
                    ? IPAddress.Any
                    : IPAddress.Parse(connectHost == "[::1]" ? "::1" : connectHost);
                var listener = new TcpListener(addr, port);
                listener.Start();
                listener.Stop();
                return false;
            }
            catch
            {
                return true;
            }
        }
    }

    /// 取首次启动生成的管理员账号。
    ///
    /// 优先读 data\initial-admin-password.txt（issue #179）：服务端把随机密码
    /// 落了一份明文，就是为了让「日志那条记录已经翻过去了」也能取回。日志退为
    /// 次选来源 —— err.log 会被轮转，也可能被用户手工清过。
    internal static class InitialCredentials
    {
        public static bool TryExtract(string errLogPath, out string username, out string password)
        {
            username = null;
            password = null;

            if (TryReadPasswordFile(errLogPath, out password))
            {
                username = "admin";
                return true;
            }
            return TryExtractFromLog(errLogPath, out username, out password);
        }

        /// 落盘文件与 err.log 同级（都在 data\ 下）。文件不存在 = 已改过密码或
        /// 走了 WB_ADMIN_PASSWORD，两种情况下都返回 false 是正确的。
        private static bool TryReadPasswordFile(string errLogPath, out string password)
        {
            password = null;
            try
            {
                string dataDir = Path.GetDirectoryName(Path.GetFullPath(errLogPath));
                if (string.IsNullOrEmpty(dataDir)) { return false; }
                string path = Path.Combine(dataDir, "initial-admin-password.txt");
                if (!File.Exists(path)) { return false; }
                string raw;
                using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                using (var reader = new StreamReader(stream, Encoding.UTF8))
                {
                    raw = reader.ReadToEnd();
                }
                string trimmed = (raw ?? string.Empty).Trim();
                if (trimmed.Length == 0) { return false; }
                password = trimmed;
                return true;
            }
            catch { }
            return false;
        }

        private static bool TryExtractFromLog(string errLogPath, out string username, out string password)
        {
            username = null;
            password = null;
            try
            {
                if (!File.Exists(errLogPath)) { return false; }
                // err.log 正被 uvicorn 以写模式持有，必须以 ReadWrite 共享打开，否则 IOException
                string[] lines;
                using (var stream = new FileStream(errLogPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                using (var reader = new StreamReader(stream, Encoding.UTF8))
                {
                    var collected = new List<string>();
                    string line;
                    while ((line = reader.ReadLine()) != null) { collected.Add(line); }
                    lines = collected.ToArray();
                }
                // 从尾部往前找：日志可能含多次安装的记录，取最新一次
                for (int i = lines.Length - 1; i >= 0; i--)
                {
                    if (lines[i].IndexOf("已生成初始管理员账号", StringComparison.Ordinal) < 0) { continue; }
                    for (int j = i; j < Math.Min(i + 6, lines.Length); j++)
                    {
                        Match user = Regex.Match(lines[j], @"用户名:\s*(\S+)");
                        if (user.Success) { username = user.Groups[1].Value; }
                        Match pwd = Regex.Match(lines[j], @"密码\s*:\s*(\S+)");
                        if (pwd.Success) { password = pwd.Groups[1].Value; }
                    }
                    if (username != null && password != null) { return true; }
                }
            }
            catch { }
            return false;
        }
    }
}
