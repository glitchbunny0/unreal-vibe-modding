"""A screenshot of the game's window taken from the screen, for engines with no screenshot call of their own."""

from __future__ import annotations


def script(process: str, out_png: str, out_text: str) -> str:
    """PowerShell that copies the client area of the game's main window into `out_png` and writes, to `out_text`, either
    `OK <width>x<height>` or `ERROR: <why>`. It has to run in the logged-in user's session (a scheduled task over SSH):
    other sessions see no windows. It copies nothing when another window is in front: the screen then shows the
    player's other programs, which are not ours to look at. It never brings the game to the front."""
    process, out_png, out_text = (s.replace("'", "''") for s in (process, out_png, out_text))
    return f"""
$ErrorActionPreference = 'Stop'
function Done($text) {{ Set-Content -Path '{out_text}' -Value $text -Encoding UTF8; exit }}
try {{
  Add-Type -AssemblyName System.Drawing
  Add-Type -Namespace U -Name W -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
[DllImport("user32.dll")] public static extern System.IntPtr GetForegroundWindow();
[DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(System.IntPtr h, System.Text.StringBuilder s, int n);
[DllImport("user32.dll")] public static extern bool IsIconic(System.IntPtr h);
[DllImport("user32.dll")] public static extern bool GetClientRect(System.IntPtr h, out RECT r);
[DllImport("user32.dll")] public static extern bool ClientToScreen(System.IntPtr h, ref POINT p);
public struct RECT {{ public int Left, Top, Right, Bottom; }}
public struct POINT {{ public int X, Y; }}
'@
  [void][U.W]::SetProcessDPIAware()
  $g = Get-Process -Name '{process}' -ErrorAction SilentlyContinue | Where-Object {{ $_.MainWindowHandle -ne [IntPtr]::Zero }} | Select-Object -First 1
  if (-not $g) {{ Done 'ERROR: {process} has no window in the logged-in session (not running, or still starting)' }}
  $h = $g.MainWindowHandle
  if ([U.W]::IsIconic($h)) {{ Done 'ERROR: the game window is minimized' }}
  $fg = [U.W]::GetForegroundWindow()
  if ($fg -ne $h) {{
    $sb = New-Object System.Text.StringBuilder 256; [void][U.W]::GetClassName($fg, $sb, 256)
    Done ('ERROR: the game is not the window in front (' + $sb.ToString() + ' is), and copying the screen now would show the player''s other programs; ask the player to switch to the game')
  }}
  $r = New-Object U.W+RECT
  [void][U.W]::GetClientRect($h, [ref]$r)
  $p = New-Object U.W+POINT
  [void][U.W]::ClientToScreen($h, [ref]$p)
  $w = $r.Right - $r.Left; $ht = $r.Bottom - $r.Top
  if ($w -le 0 -or $ht -le 0) {{ Done 'ERROR: the game window has no area' }}
  $bmp = New-Object System.Drawing.Bitmap $w, $ht
  $gr = [System.Drawing.Graphics]::FromImage($bmp)
  $gr.CopyFromScreen($p.X, $p.Y, 0, 0, $bmp.Size)
  $bmp.Save('{out_png}', [System.Drawing.Imaging.ImageFormat]::Png)
  $gr.Dispose(); $bmp.Dispose()
  Done "OK ${{w}}x${{ht}}"
}} catch {{ Done ('ERROR: ' + $_.Exception.Message) }}
"""
