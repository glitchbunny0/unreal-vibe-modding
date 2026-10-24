"""OS-level key presses on Windows, for what reads the keyboard directly: UE4SS's own keybinds (RegisterKeyBind)
poll GetAsyncKeyState and only while the game window is in front. The game's Enhanced Input is better driven with
`input press`; this is for testing a mod's keybinds."""

from __future__ import annotations

VK = {"SPACE": 0x20, "RETURN": 0x0D, "ENTER": 0x0D, "ESCAPE": 0x1B, "TAB": 0x09, "BACKSPACE": 0x08,
      "LEFT_ARROW": 0x25, "UP_ARROW": 0x26, "RIGHT_ARROW": 0x27, "DOWN_ARROW": 0x28, "LEFT": 0x25, "UP": 0x26,
      "RIGHT": 0x27, "DOWN": 0x28, "INS": 0x2D, "INSERT": 0x2D, "DEL": 0x2E, "DELETE": 0x2E, "HOME": 0x24, "END": 0x23,
      "PAGE_UP": 0x21, "PAGE_DOWN": 0x22, "MULTIPLY": 0x6A, "ADD": 0x6B, "SUBTRACT": 0x6D, "DECIMAL": 0x6E,
      "DIVIDE": 0x6F, "SHIFT": 0x10, "CONTROL": 0x11, "CTRL": 0x11, "ALT": 0x12}
NUMPAD = ["ZERO", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE"]
for i, word in enumerate(NUMPAD):
    VK[f"NUM_{word}"] = 0x60 + i
    VK[f"NUM{i}"] = 0x60 + i
for i in range(1, 25):
    VK[f"F{i}"] = 0x6F + i
for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    VK[c] = ord(c)
for d in "0123456789":
    VK[d] = ord(d)


def parse(combo: str) -> list[int]:
    """'F8', 'CONTROL+F8', 'num_one' (UE4SS Key names) -> virtual-key codes, modifiers first."""
    codes = []
    for part in combo.replace(" ", "").upper().split("+"):
        if part not in VK:
            raise ValueError(f"unknown key '{part}' (UE4SS Key names: F1-F24, A-Z, 0-9, NUM_ZERO-NUM_NINE, SPACE, RETURN, "
                             "ESCAPE, arrows, CONTROL/SHIFT/ALT+...)")
        codes.append(VK[part])
    return codes


def script(process: str, codes: list[int], hold_ms: int, out_file: str) -> str:
    """PowerShell that brings the game window to the front, holds the keys, and writes what was in front to out_file.
    It has to run in the logged-in user's session (a scheduled task over SSH)."""
    down = "; ".join(f"[U.K]::keybd_event({c}, 0, 0, [UIntPtr]::Zero)" for c in codes)
    up = "; ".join(f"[U.K]::keybd_event({c}, 0, 2, [UIntPtr]::Zero)" for c in reversed(codes))
    return f"""
Add-Type -Namespace U -Name K -MemberDefinition '[DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, System.UIntPtr extra); [DllImport("user32.dll")] public static extern System.IntPtr GetForegroundWindow(); [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(System.IntPtr h, System.Text.StringBuilder s, int n); [DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr h); [DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr h, int c);'
$g = Get-Process -Name '{process}' -ErrorAction SilentlyContinue | Where-Object {{ $_.MainWindowHandle -ne [IntPtr]::Zero }} | Select-Object -First 1
if ($g) {{
  # Windows lets a background process take the foreground right after a key event: tap ALT first.
  [U.K]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero); [U.K]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
  [void][U.K]::ShowWindow($g.MainWindowHandle, 9)
  [void][U.K]::SetForegroundWindow($g.MainWindowHandle)
  Start-Sleep -Milliseconds 300
}}
$sb = New-Object System.Text.StringBuilder 256
[void][U.K]::GetClassName([U.K]::GetForegroundWindow(), $sb, 256)
{down}
Start-Sleep -Milliseconds {int(hold_ms)}
{up}
Set-Content -Path '{out_file}' -Value $sb.ToString()
"""
