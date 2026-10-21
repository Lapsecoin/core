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
        const string Octet = @"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)";
        // The rules below are src/crashlog.py's, and tests/scrub_cases.json is run through both.
        static readonly Regex Address = new Regex(@"\b(?:[a-z]{3,10}\.){3,}[a-z]{3,10}\b");
        static readonly Regex KeyText = new Regex(@"\blc1-[A-Za-z0-9_-]{20,}");
        static readonly Regex SecretValue = new Regex(
            @"\b(pass(?:word|phrase)?|secret|token|api[_-]?key|private[_-]?key|priv|seed|mnemonic|kek|key)\b" +
            @"(['""]?\s*[=:]\s*)(?:'[^']*'|""[^""]*""|\S+)", RegexOptions.IgnoreCase);
        static readonly Regex Bytes = new Regex(@"\bb(['""])(?:\\x[0-9a-fA-F]{2}|[^'""\\]){8,}\1");
        static readonly Regex Hex = new Regex(@"\b[0-9a-fA-F]{20,}\b");
        static readonly Regex Token = new Regex(@"\b[A-Za-z0-9+/_=-]{32,}\b");
        static readonly Regex Words = new Regex(@"\b(?:[a-z]{3,8} ){5,}[a-z]{3,8}\b");
        static readonly Regex IPv4 = new Regex(@"(?<![\w.])" + Octet + @"(?:\." + Octet + @"){3}(?!\w|\.\d)");
        static readonly Regex IPv6 = new Regex(
            @"(?<![\w:])(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}(?![\w:])" +
            @"|(?<![\w:])(?:(?:[0-9a-fA-F]{1,4}:){1,7}|:):(?:[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4}){0,6})?(?![\w:])");
        static readonly Regex Email = new Regex(@"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}");
        static readonly Regex Stamp = new Regex(@"\b(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)\b");
        static readonly Regex Frame = new Regex(@"File ""([^""]+)"", line (\d+|\?\?\?),? in ([\w<>.]+)");
        static readonly string Fence = new string('`', 3);

        static int Main(string[] args)
        {
            string dir = AppDomain.CurrentDomain.BaseDirectory;
            string scrubDir = Environment.GetEnvironmentVariable("LAPSECOIN_SCRUB_TEST");
            if (!string.IsNullOrEmpty(scrubDir)) return ScrubTest(scrubDir);
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
            // The launcher's own sentence is built from numbers and is not run through Clean (a run of
            // ordinary words looks like a recovery phrase); what Windows said is.
            string what = refused != null ? Clean(refused, 160) : "the program ended with exit code " + hex + " after " + (int)lived.TotalSeconds + " s";

            string logDir = DataDir();
            string steps = FreshTail(logDir, "lapsecoin-start.log", 15);
            string crashLog = Path.Combine(logDir, "lapsecoin-crash.log");
            string earlier = CrashLogSinceSent(logDir);

            StringBuilder seen = new StringBuilder();
            if (earlier.Length > 0) { seen.AppendLine(); seen.AppendLine("the program's own crash log:"); seen.AppendLine(earlier); }
            string events = RecentEvents();
            if (events.Length > 0) { seen.AppendLine(); seen.AppendLine("Windows events in the last 3 minutes:"); seen.AppendLine(events); }

            string title = ("Crash " + sig + ": launcher: " + what).Replace("\r", " ").Replace("\n", " ");
            if (title.Length > 120) title = title.Substring(0, 120);
            string[] timed = RelativeTimes(Clean(seen.ToString(), MaxTrace + MaxEvents + 400), Clean(steps, MaxSteps));
            string body = BuildBody(sig, what, "launcher: " + what + "\n" + timed[0], timed[1], repo);

            if (!string.IsNullOrEmpty(dryRun))
            {
                File.WriteAllText(dryRun, "TITLE: " + title + "\n\n" + body);
                return;
            }
            if (Send(repo, token, api, sig, title, body)) MarkSent(logDir, crashLog);
        }

        static string BuildBody(string sig, string what, string trace, string steps, string repo)
        {
            StringBuilder b = new StringBuilder();
            b.AppendLine("Reported automatically by the LapseCoin launcher.");
            b.AppendLine();
            b.AppendLine("LapseCoin " + Version() + ", launcher, " + (what.Length > 80 ? what.Substring(0, 80) : what) + ". Windows " + WindowsBuild() + " " + Arch() + ".");
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
            string links = CodeLinks(trace, repo, Version());
            if (links.Length > 0)
            {
                b.AppendLine();
                b.AppendLine("Code in the trace, at this release:");
                b.Append(links);
            }
            return b.ToString();
        }

        // The program's own files in the trace, linked at the release's tag. The list of them
        // is the OWN_MODULES file the build puts beside the program.
        static string CodeLinks(string trace, string repo, string version)
        {
            Dictionary<string, string> modules = new Dictionary<string, string>();
            try
            {
                foreach (string line in File.ReadAllLines(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "OWN_MODULES")))
                {
                    string path = line.Trim();
                    if (path.EndsWith(".py")) modules[path.Substring(path.LastIndexOf('/') + 1)] = path;
                }
            }
            catch (Exception) { return ""; }
            if (modules.Count == 0 || string.IsNullOrEmpty(repo) || !Regex.IsMatch(repo, @"^[\w.-]+/[\w.-]+$")
                || !Regex.IsMatch(version, @"^[\w.]+$")) return "";
            StringBuilder b = new StringBuilder();
            Dictionary<string, bool> seen = new Dictionary<string, bool>();
            int count = 0;
            foreach (Match m in Frame.Matches(trace))
            {
                string name = m.Groups[1].Value;
                name = name.Substring(Math.Max(name.LastIndexOf('\\'), name.LastIndexOf('/')) + 1);
                string number = m.Groups[2].Value, func = m.Groups[3].Value, repoPath;
                string key = name + "|" + number + "|" + func;
                if (!modules.TryGetValue(name, out repoPath) || seen.ContainsKey(key)) continue;
                seen[key] = true;
                bool numeric = Regex.IsMatch(number, @"^\d+$");
                b.AppendLine("- [" + Clean(numeric ? name + ":" + number : name, 60) + " in " + Clean(func, 60) + "](https://github.com/" +
                             repo + "/blob/v" + version + "/" + repoPath + (numeric ? "#L" + number : "") + ")");
                if (++count == 15) break;
            }
            return b.ToString();
        }

        // Each local date and time becomes the seconds since the earliest one, so a report
        // shows the order and spacing but not the person's time zone.
        static DateTime? Moment(Match m)
        {
            try
            {
                return new DateTime(int.Parse(m.Groups[1].Value), int.Parse(m.Groups[2].Value), int.Parse(m.Groups[3].Value),
                                    int.Parse(m.Groups[4].Value), int.Parse(m.Groups[5].Value), int.Parse(m.Groups[6].Value));
            }
            catch (Exception) { return null; }
        }

        static string[] RelativeTimes(params string[] texts)
        {
            DateTime? first = null;
            foreach (string text in texts)
                foreach (Match m in Stamp.Matches(text))
                {
                    DateTime? t = Moment(m);
                    if (t.HasValue && (!first.HasValue || t.Value < first.Value)) first = t;
                }
            if (!first.HasValue) return texts;
            string[] result = new string[texts.Length];
            for (int i = 0; i < texts.Length; i++)
            {
                DateTime start = first.Value;
                result[i] = Stamp.Replace(texts[i], delegate(Match m)
                {
                    DateTime? t = Moment(m);
                    return t.HasValue ? "+" + (long)(t.Value - start).TotalSeconds + "s" : "<time>";
                });
            }
            return result;
        }

        static string Version()
        {
            string v = RawVersion();
            return v.Length == 0 ? "unknown" : Clean(v, 20);
        }

        // Without Clean, which needs the version itself (the app's own 1.1.6.0 is not an IP address).
        static string RawVersion()
        {
            try { return File.ReadAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "VERSION")).Trim(); }
            catch (Exception) { return ""; }
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
                if (tail.Length <= MaxTrace) return tail;
                // A native crash dump starts with the thread that crashed, so keep the start too.
                const string gap = "\n[...]\n";
                int head = MaxTrace / 2;
                return tail.Substring(0, head) + gap + tail.Substring(tail.Length - (MaxTrace - head - gap.Length));
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
            // the longer folders first, or the profile folder would swallow them
            List<KeyValuePair<string, string>> folders = new List<KeyValuePair<string, string>>();
            foreach (string key in new string[] { "LOCALAPPDATA", "APPDATA", "TEMP", "USERPROFILE" })
            {
                string value = Environment.GetEnvironmentVariable(key);
                if (!string.IsNullOrEmpty(value)) folders.Add(new KeyValuePair<string, string>(key, value));
            }
            folders.Sort(delegate(KeyValuePair<string, string> a, KeyValuePair<string, string> b) { return b.Value.Length.CompareTo(a.Value.Length); });
            foreach (KeyValuePair<string, string> f in folders) text = Replace(text, f.Value, "%" + f.Key + "%");
            string[][] names = new string[][] { new string[] { "USERNAME", "<user>" }, new string[] { "COMPUTERNAME", "<computer>" }, new string[] { "USERDOMAIN", "<domain>" } };
            foreach (string[] n in names)
            {
                string value = Environment.GetEnvironmentVariable(n[0]);
                if (!string.IsNullOrEmpty(value) && value.Length > 2) text = Replace(text, value, n[1]);
            }
            text = Regex.Replace(text, "[A-Za-z]:\\\\Users\\\\[^\\\\\\s]+", "%USERPROFILE%");
            text = KeyText.Replace(text, "<key>");
            text = Bytes.Replace(text, "b'<bytes>'");
            text = SecretValue.Replace(text, delegate(Match m) { return m.Groups[1].Value + m.Groups[2].Value + "<removed>"; });
            text = Hex.Replace(text, "<hex>");
            text = Token.Replace(text, "<token>");
            text = Words.Replace(text, "<words>");
            string version = RawVersion();
            string own = version.Length > 0 ? version + ".0" : null;
            text = IPv4.Replace(text, delegate(Match m) { return m.Value == own ? m.Value : "<ip>"; });
            text = IPv6.Replace(text, "<ip>");
            text = Email.Replace(text, "<email>");
            text = Address.Replace(text, "<address>");
            text = Regex.Replace(text, "[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f]", "");
            text = text.Replace(Fence, "'''");
            return text.Length > limit ? text.Substring(0, limit) : text;
        }

        // Test hook: LAPSECOIN_SCRUB_TEST names a folder of *.in files; each gets its *.out.
        static int ScrubTest(string folder)
        {
            foreach (string file in Directory.GetFiles(folder, "*.in"))
                File.WriteAllText(Path.ChangeExtension(file, ".out"), Clean(File.ReadAllText(file, Encoding.UTF8), int.MaxValue), new UTF8Encoding(false));
            return 0;
        }

        static string Replace(string text, string find, string with)
        {
            return Regex.Replace(text, Regex.Escape(find), with.Replace("$", "$$"), RegexOptions.IgnoreCase);
        }
    }
}
