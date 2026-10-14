using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class PortableLauncher
{
    [STAThread]
    private static int Main()
    {
        string home = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        string pythonw = Path.Combine(home, "python", "pythonw.exe");
        string program = Path.Combine(home, "程序");
        if (!File.Exists(pythonw))
        {
            MessageBox.Show("未找到 python\\pythonw.exe\n请确认启动器、python、程序、data、vendor 在同一层。", "企微客户自动化管理工具");
            return 1;
        }
        if (!Directory.Exists(program))
        {
            MessageBox.Show("未找到程序目录。", "企微客户自动化管理工具");
            return 1;
        }

        var start = new ProcessStartInfo
        {
            FileName = pythonw,
            Arguments = "-m gui.main",
            WorkingDirectory = home,
            UseShellExecute = false,
        };
        start.EnvironmentVariables["TESTVOICE_HOME"] = home;
        start.EnvironmentVariables["PYTHONUTF8"] = "1";
        start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        try
        {
            Process.Start(start);
        }
        catch (Exception exc)
        {
            MessageBox.Show("启动失败：\n" + exc.Message, "企微客户自动化管理工具");
            return 1;
        }
        return 0;
    }
}
