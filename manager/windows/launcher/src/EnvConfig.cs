using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.Text.RegularExpressions;

namespace WorkBuddyLauncher
{
    /// .env 中管理端监听地址（WB_MANAGER_HOST / WB_MANAGER_PORT）的读写与校验。
    internal static class EnvConfig
    {
        public const string DefaultHost = "127.0.0.1";
        public const int DefaultPort = 7864;

        private static readonly Regex HostLine = new Regex(@"^\s*WB_MANAGER_HOST\s*=\s*(\S+)");
        private static readonly Regex PortLine = new Regex(@"^\s*WB_MANAGER_PORT\s*=\s*(\S+)");

        public static void ReadHostPort(string envPath, out string host, out int port)
        {
            host = DefaultHost;
            port = DefaultPort;
            if (!File.Exists(envPath)) { return; }
            foreach (string line in File.ReadAllLines(envPath, Encoding.UTF8))
            {
                Match m = HostLine.Match(line);
                if (m.Success)
                {
                    host = m.Groups[1].Value;
                    continue;
                }
                m = PortLine.Match(line);
                if (m.Success)
                {
                    int parsed;
                    if (int.TryParse(m.Groups[1].Value, out parsed)) { port = parsed; }
                }
            }
        }

        public static bool TryValidateHost(string host, out string error)
        {
            error = null;
            if (string.IsNullOrWhiteSpace(host))
            {
                error = "IP 地址不能为空。";
                return false;
            }
            host = host.Trim();
            if (string.Equals(host, "localhost", StringComparison.OrdinalIgnoreCase)) { return true; }
            IPAddress parsed;
            if (!IPAddress.TryParse(host, out parsed))
            {
                error = "IP 地址格式不正确（示例：127.0.0.1、0.0.0.0 或 ::）";
                return false;
            }
            return true;
        }

        public static bool TryValidatePort(string text, out int port, out string error)
        {
            error = null;
            if (!int.TryParse((text ?? string.Empty).Trim(), out port) || port < 1 || port > 65535)
            {
                error = "端口必须是 1–65535 之间的整数。";
                return false;
            }
            return true;
        }

        /// 写回 .env：逐行替换已有配置，缺失则追加；其余内容与格式保持原样。
        public static void WriteHostPort(string envPath, string host, int port)
        {
            var lines = new List<string>();
            if (File.Exists(envPath)) { lines.AddRange(File.ReadAllLines(envPath, Encoding.UTF8)); }

            bool hostSet = false;
            bool portSet = false;
            for (int i = 0; i < lines.Count; i++)
            {
                if (HostLine.IsMatch(lines[i]))
                {
                    lines[i] = "WB_MANAGER_HOST=" + host;
                    hostSet = true;
                }
                else if (PortLine.IsMatch(lines[i]))
                {
                    lines[i] = "WB_MANAGER_PORT=" + port;
                    portSet = true;
                }
            }
            if (!hostSet) { lines.Add("WB_MANAGER_HOST=" + host); }
            if (!portSet) { lines.Add("WB_MANAGER_PORT=" + port); }

            File.WriteAllLines(envPath, lines.ToArray(), new UTF8Encoding(false));
        }

        /// 探测与打开浏览器用的主机：通配监听地址（0.0.0.0 / ::）归一化为本机回环地址。
        public static string ToConnectHost(string host)
        {
            host = (host ?? string.Empty).Trim();
            if (host.Length == 0) { return DefaultHost; }
            if (host == "0.0.0.0" || host == "::" || host == "*") { return "127.0.0.1"; }
            if (host == "::1") { return "[::1]"; }
            return host;
        }
    }
}
