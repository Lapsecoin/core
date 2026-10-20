// lapsecoin-launch.exe: what the Start menu, the desktop shortcut and the Store
// package start. It starts lapsecoin.exe, waits for it, and if Windows ended it
// with a fault (a DLL that would not load, an access violation, a file blocked by
// Smart App Control) or would not start it at all, files a sanitized report as an
// issue, which nothing inside lapsecoin.exe could do because none of its code had
// run. A report is not sent when the program ends any other way.
//
// It is written for the C# 5 compiler that ships with Windows and uses nothing
// beyond .NET Framework 4, so it has nothing of its own to fail to load. See
// scripts/build_launcher.ps1. Test hooks: LAPSECOIN_LAUNCHER_CHILD names another
// program to start, and LAPSECOIN_CRASH_DRYRUN names a file to write the report to
// instead of sending it.

using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Diagnostics.Eventing.Reader;
using System.IO;
using System.Net;
using System.Text;
using System.Text.RegularExpressions;
using Microsoft.Win32;

namespace LapseCoin
{
    static class Launcher
    {
        const int MaxTrace = 3500;
        const int MaxSteps = 1200;
        const int MaxEvents = 1800;
        const int TimeoutMs = 5000;
        static readonly Regex Address = new Regex(@"\b(?:[a-z]{3,10}\.){11}[a-z]{3,10}\b");
        static readonly string Fence = new string('`', 3);

        static int Main(string[] args)
        {
            string dir = AppDomain.CurrentDomain.BaseDirectory;
            string child = Environment.GetEnvironmentVariable("LAPSECOIN_LAUNCHER_CHILD");
            if (string.IsNullOrEmpty(child)) child = Path.Combine(dir, "lapsecoin.exe");

            DateTime began = DateTime.UtcNow;
            int code = 0;
            string refused = null;
            try
            {
                ProcessStartInfo info = new ProcessStartInfo(child, JoinArgs(args));
                info.UseShellExecute = false;
                info.WorkingDirectory = dir;
                using (Process p = Process.Start(info))
                {
                    p.WaitForExit();
                    code = p.ExitCode;
                }
            }
            catch (Win32Exception e)
            {
                refused = "Windows would not start the program: error " + e.NativeErrorCode + ", " + e.Message;
                code = e.NativeErrorCode;
            }
            catch (Exception e)
            {
                refused = "the launcher could not start the program: " + e.GetType().Name + ": " + e.Message;
            }

            if (refused != null || IsFault(code))
            {
                try { Report(dir, code, refused, DateTime.UtcNow - began); } catch (Exception) { }
            }
            return code;
        }

        // An exit code that means Windows ended the process for a fault, not one that
        // means it was closed, killed or exited by itself.
        public static bool IsFault(int code)
        {
            uint c = unchecked((uint)code);
            return (c >= 0xC0000000u && c <= 0xC0FFFFFFu) || c == 0x80000003u || c == 0x40000015u;
        }

        static string JoinArgs(string[] args)
        {
            List<string> parts = new List<string>();
            foreach (string a in args)
            {
                if (a.Length > 0 && a.IndexOfAny(new char[] { ' ', '\t', '"' }) < 0) { parts.Add(a); continue; }
                parts.Add("\"" + a.Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"");
            }
            return string.Join(" ", parts.ToArray());
        }

        // ----- what is reported -------------------------------------------------

        static void Report(string dir, int code, string refused, TimeSpan lived)
        {
            if (Environment.GetEnvironmentVariable("LAPSECOIN_CRASH_REPORTS") == "0") return;
            string dryRun = Environment.GetEnvironmentVariable("LAPSECOIN_CRASH_DRYRUN");
            string repo, token, api;
            bool haveTarget = Target(dir, out repo, out token, out api);
            if (!haveTarget && string.IsNullOrEmpty(dryRun)) return;
            if (OptedOut()) return;

            string hex = "0x" + unchecked((uint)code).ToString("X8");
            string sig = refused != null ? "Lstart" + code : "L" + hex.Substring(2).ToLowerInvariant();
            string what = refused != null ? refused : "the program ended with exit code " + hex + " after " + (int)lived.TotalSeconds + " s";

            string logDir = DataDir();
            string steps = FreshTail(logDir, "lapsecoin-start.log", 15);
            string crashLog = Path.Combine(logDir, "lapsecoin-crash.log");
            string earlier = CrashLogSinceSent(logDir);

            StringBuilder trace = new StringBuilder();
            trace.AppendLine("launcher: " + what);
            if (earlier.Length > 0) { trace.AppendLine(); trace.AppendLine("the program's own crash log:"); trace.AppendLine(earlier); }
            string events = RecentEvents();
            if (events.Length > 0) { trace.AppendLine(); trace.AppendLine("Windows events in the last 3 minutes:"); trace.AppendLine(events); }

            string title = Clean("Crash " + sig + ": launcher: " + what, 120).Replace("\r", " ").Replace("\n", " ");
            string body = BuildBody(sig, what, Clean(trace.ToString(), MaxTrace + MaxEvents), Clean(steps, MaxSteps));

            if (!string.IsNullOrEmpty(dryRun))
            {
                File.WriteAllText(dryRun, "TITLE: " + title + "\n\n" + body);
                return;
            }
            if (Send(repo, token, api, sig, title, body)) MarkSent(logDir, crashLog);
        }

        static string BuildBody(string sig, string what, string trace, string steps)
        {
            StringBuilder b = new StringBuilder();
            b.AppendLine("Reported automatically by the LapseCoin launcher.");
            b.AppendLine();
            b.AppendLine("LapseCoin " + Version() + ", launcher, " + Clean(what, 80) + ". Windows " + WindowsBuild() + " " + Arch() + ".");
            b.AppendLine();
            b.AppendLine("What the launcher saw:");
            b.AppendLine(Fence);
            b.AppendLine(trace);
            b.AppendLine(Fence);
            b.AppendLine();
            b.AppendLine("Last start-up steps:");
            b.AppendLine(Fence);
            b.AppendLine(steps);
            b.AppendLine(Fence);
            return b.ToString();
        }

        static string Version()
        {
            try { return Clean(File.ReadAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "VERSION")).Trim(), 20); }
            catch (Exception) { return "unknown"; }
        }

        static string WindowsBuild()
        {
            try
            {
                RegistryKey k = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows NT\CurrentVersion");
                object build = k.GetValue("CurrentBuildNumber"), ubr = k.GetValue("UBR");
                return "10.0." + build + (ubr != null ? "." + ubr : "");
            }
            catch (Exception) { return "unknown"; }
        }

        static string Arch()
        {
            string a = Environment.GetEnvironmentVariable("PROCESSOR_ARCHITEW6432");
            if (string.IsNullOrEmpty(a)) a = Environment.GetEnvironmentVariable("PROCESSOR_ARCHITECTURE");
            return string.IsNullOrEmpty(a) ? "unknown" : Clean(a, 12);
        }

        // ----- where it goes ----------------------------------------------------

        static bool Target(string dir, out string repo, out string token, out string api)
        {
            repo = Environment.GetEnvironmentVariable("LAPSECOIN_CRASH_REPO");
            token = Environment.GetEnvironmentVariable("LAPSECOIN_CRASH_TOKEN");
            api = Environment.GetEnvironmentVariable("LAPSECOIN_CRASH_API");
            if (string.IsNullOrEmpty(api)) api = "https://api.github.com";
            if (!string.IsNullOrEmpty(repo) && !string.IsNullOrEmpty(token)) return true;
            try
            {
                string json = File.ReadAllText(Path.Combine(dir, "CRASH_REPORT"));
                Match r = Regex.Match(json, "\"repo\"\\s*:\\s*\"([^\"]+)\"");
                Match t = Regex.Match(json, "\"token\"\\s*:\\s*\"([^\"]+)\"");
                if (r.Success && t.Success) { repo = r.Groups[1].Value; token = t.Groups[1].Value; return true; }
            }
            catch (Exception) { }
            return false;
        }

        static bool OptedOut()
        {
            foreach (string d in Candidates())
                if (File.Exists(Path.Combine(d, "crash-reports-off"))) return true;
            return false;
        }

        static List<string> Candidates()
        {
            string home = Environment.GetEnvironmentVariable("USERPROFILE");
            List<string> list = new List<string>();
            if (!string.IsNullOrEmpty(home)) { list.Add(Path.Combine(home, "LapseCoin")); list.Add(home); }
            list.Add(Path.GetTempPath());
            return list;
        }

        static string DataDir()
        {
            foreach (string d in Candidates())
                if (File.Exists(Path.Combine(d, "lapsecoin-start.log")) || File.Exists(Path.Combine(d, "lapsecoin-crash.log"))) return d;
            return Candidates()[0];
        }

        static bool Send(string repo, string token, string api, string sig, string title, string body)
        {
            try
            {
                ServicePointManager.SecurityProtocol = (SecurityProtocolType)3072;     // TLS 1.2
                string query = "repo:" + repo + " is:issue in:title \"Crash " + sig + "\"";
                string found = Call("GET", api + "/search/issues?q=" + Uri.EscapeDataString(query) + "&per_page=1", token, null);
                Match number = Regex.Match(found, "\"number\"\\s*:\\s*(\\d+)");
                if (number.Success)
                {
                    Call("POST", api + "/repos/" + repo + "/issues/" + number.Groups[1].Value + "/comments", token,
                         "{\"body\":" + Quote("Again: " + title) + "}");
                }
                else
                {
                    Call("POST", api + "/repos/" + repo + "/issues", token,
                         "{\"title\":" + Quote(title) + ",\"body\":" + Quote(body) + ",\"labels\":[\"crash-report\"]}");
                }
                return true;
            }
            catch (Exception) { return false; }
        }

        static string Call(string method, string url, string token, string json)
        {
            HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
            req.Method = method;
            req.Timeout = TimeoutMs;
            req.ReadWriteTimeout = TimeoutMs;
            req.UserAgent = "LapseCoin-launcher/1";
            req.Accept = "application/vnd.github+json";
            req.Headers["Authorization"] = "Bearer " + token;
            if (json != null)
            {
                byte[] data = Encoding.UTF8.GetBytes(json);
                req.ContentType = "application/json";
                req.ContentLength = data.Length;
                using (Stream s = req.GetRequestStream()) s.Write(data, 0, data.Length);
            }
            using (WebResponse res = req.GetResponse())
            using (StreamReader r = new StreamReader(res.GetResponseStream()))
                return r.ReadToEnd();
        }

        static string Quote(string text)
        {
            StringBuilder b = new StringBuilder("\"");
            foreach (char c in text)
            {
                if (c == '"') b.Append("\\\"");
                else if (c == '\\') b.Append("\\\\");
                else if (c == '\n') b.Append("\\n");
                else if (c == '\r') b.Append("\\r");
                else if (c == '\t') b.Append("\\t");
                else if (c < ' ') b.Append("\\u" + ((int)c).ToString("x4"));
                else b.Append(c);
            }
            return b.Append('"').ToString();
        }

        // ----- the program's own files, and Windows' own record of the crash ----

        static string FreshTail(string dir, string name, int lines)
        {
            try
            {
                string path = Path.Combine(dir, name);
                // an older run's log would mislead, so only one written in the last few minutes
                if (DateTime.UtcNow - File.GetLastWriteTimeUtc(path) > TimeSpan.FromMinutes(5)) return "";
                string[] all = File.ReadAllLines(path);
                int from = Math.Max(0, all.Length - lines);
                return string.Join("\n", all, from, all.Length - from);
            }
            catch (Exception) { return ""; }
        }

        static string CrashLogSinceSent(string dir)
        {
            try
            {
                string text = File.ReadAllText(Path.Combine(dir, "lapsecoin-crash.log"));
                long offset = 0;
                try { offset = long.Parse(File.ReadAllText(Path.Combine(dir, "lapsecoin-crash.sent")).Trim()); } catch (Exception) { }
                if (offset < 0 || offset > text.Length) offset = 0;
                string tail = text.Substring((int)offset);
                return tail.Length > MaxTrace ? tail.Substring(tail.Length - MaxTrace) : tail;
            }
            catch (Exception) { return ""; }
        }

        static void MarkSent(string dir, string crashLog)
        {
            try
            {
                if (File.Exists(crashLog))
                    File.WriteAllText(Path.Combine(dir, "lapsecoin-crash.sent"), new FileInfo(crashLog).Length.ToString());
            }
            catch (Exception) { }
        }

        static string RecentEvents()
        {
            StringBuilder all = new StringBuilder();
            string recent = "TimeCreated[timediff(@SystemTime) <= 180000]";
            AppendEvents(all, "Application", "*[System[(Provider[@Name='Application Error'] or Provider[@Name='Windows Error Reporting']) and " + recent + "]]", 3);
            AppendEvents(all, "Microsoft-Windows-CodeIntegrity/Operational", "*[System[" + recent + "]]", 6);
            string s = all.ToString();
            return s.Length > MaxEvents ? s.Substring(0, MaxEvents) : s;
        }

        static void AppendEvents(StringBuilder into, string log, string xpath, int max)
        {
            try
            {
                EventLogQuery q = new EventLogQuery(log, PathType.LogName, xpath);
                q.ReverseDirection = true;
                using (EventLogReader reader = new EventLogReader(q))
                {
                    EventRecord rec;
                    int n = 0;
                    while (n < max && (rec = reader.ReadEvent()) != null)
                    {
                        using (rec)
                        {
                            string text;
                            try { text = rec.FormatDescription(); } catch (Exception) { text = null; }
                            into.AppendLine("[" + log + " " + rec.Id + "] " + (text ?? "(no description)").Replace("\r", ""));
                        }
                        n++;
                    }
                }
            }
            catch (Exception) { /* the log may not be readable without administrator rights */ }
        }

        // ----- keeping the person out of it -------------------------------------

        public static string Clean(string text, int limit)
        {
            if (text == null) return "";
            foreach (string key in new string[] { "LOCALAPPDATA", "APPDATA", "TEMP", "USERPROFILE" })
            {
                string value = Environment.GetEnvironmentVariable(key);
                if (!string.IsNullOrEmpty(value)) text = Replace(text, value, "%" + key + "%");
            }
            string user = Environment.GetEnvironmentVariable("USERNAME");
            if (!string.IsNullOrEmpty(user) && user.Length > 2) text = Replace(text, user, "<user>");
            text = Address.Replace(text, "<address>");
            text = Regex.Replace(text, "[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f]", "");
            text = Regex.Replace(text, "[A-Za-z]:\\\\Users\\\\[^\\\\\\s]+", "%USERPROFILE%");
            text = text.Replace(Fence, "'''");
            return text.Length > limit ? text.Substring(0, limit) : text;
        }

        static string Replace(string text, string find, string with)
        {
            return Regex.Replace(text, Regex.Escape(find), with.Replace("$", "$$"), RegexOptions.IgnoreCase);
        }
    }
}
