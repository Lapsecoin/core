using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

class Probe
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    static extern int GetCurrentPackageFullName(ref int len, StringBuilder name);

    static string Pkg()
    {
        int len = 0;
        int rc = GetCurrentPackageFullName(ref len, null);
        if (rc == 15700) return "none (not packaged)";
        var sb = new StringBuilder(len);
        rc = GetCurrentPackageFullName(ref len, sb);
        return rc == 0 ? sb.ToString() : "error " + rc;
    }

    static string[][] Targets()
    {
        string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string roaming = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
        string docs = Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments);
        string pd = Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData);
        string prof = Environment.GetEnvironmentVariable("USERPROFILE");
        return new string[][] {
            new [] { "local_existing", Path.Combine(local, "LapseLab", "existing.txt") },
            new [] { "local_new", Path.Combine(local, "LapseLab", "new_by_pkg.txt") },
            new [] { "roaming", Path.Combine(roaming, "LapseLabRoaming", "roaming.txt") },
            new [] { "profile", Path.Combine(prof, "LapseLabProfile", "profile.txt") },
            new [] { "documents", Path.Combine(docs, "LapseLabDocs", "doc.txt") },
            new [] { "excluded", Path.Combine(local, "LapseLabExcl", "excl.txt") },
            new [] { "programdata", Path.Combine(pd, "LapseLabPD", "pd.txt") },
        };
    }

    static int Main(string[] a)
    {
        string mode = a.Length > 0 ? a[0] : "write";
        string tag = a.Length > 1 ? a[1] : "run";
        string outdir = @"C:\labout";
        Directory.CreateDirectory(outdir);
        var log = new StringBuilder();
        log.AppendLine("mode=" + mode + " tag=" + tag);
        log.AppendLine("package: " + Pkg());

        if (mode == "write" || mode == "read")
        {
            foreach (var t in Targets())
            {
                try
                {
                    if (mode == "write")
                    {
                        Directory.CreateDirectory(Path.GetDirectoryName(t[1]));
                        File.AppendAllText(t[1], "written-by-package:" + tag + Environment.NewLine);
                    }
                    string seen = File.Exists(t[1]) ? File.ReadAllText(t[1]).Trim().Replace("\r\n", " | ").Replace("\n", " | ") : "MISSING";
                    log.AppendLine(string.Format("{0,-15} package sees: {1}   ({2})", t[0], seen, t[1]));
                }
                catch (Exception e)
                {
                    log.AppendLine(string.Format("{0,-15} ERROR {1}", t[0], e.Message));
                }
            }
            File.WriteAllText(Path.Combine(outdir, "probe_" + tag + ".log"), log.ToString());
            return 0;
        }

        if (mode == "listen")
        {
            var l = new TcpListener(IPAddress.Any, 18333);
            l.Start();
            File.WriteAllText(Path.Combine(outdir, "probe_listen_ready.txt"), "listening as " + Pkg());
            var end = DateTime.UtcNow.AddSeconds(40);
            while (DateTime.UtcNow < end)
            {
                if (l.Pending())
                {
                    using (var c = l.AcceptTcpClient())
                    {
                        var b = Encoding.ASCII.GetBytes("hello-from-package " + Pkg() + "\n");
                        c.GetStream().Write(b, 0, b.Length);
                    }
                }
                else Thread.Sleep(100);
            }
            return 0;
        }
        return 1;
    }
}
