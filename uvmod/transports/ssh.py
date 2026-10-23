"""ssh transport: the game runs on a Windows box reached over SSH (OpenSSH server, key login, Windows PowerShell).
Every operation is one PowerShell script sent over one ssh call."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Iterator

from .. import protocol
from . import Exchange, Status, Transport, TransportError

# Decodes the script that follows on stdin and runs it; only this fixed bootstrap rides the command line,
# which cmd.exe caps at 8191 characters.
_BOOT = (
    '$ProgressPreference="SilentlyContinue"; [Console]::OutputEncoding=[Text.Encoding]::UTF8; '
    "$s=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([Console]::In.ReadToEnd())); "
    "& ([ScriptBlock]::Create($s))"
)
_BOOT_ENCODED = base64.b64encode(_BOOT.encode("utf-16-le")).decode("ascii")


def psq(text: str) -> str:
    """A PowerShell single-quoted literal."""
    return "'" + text.replace("'", "''") + "'"


class SshTransport(Transport):
    def __init__(self, settings):
        super().__init__(settings)
        if not settings.host:
            raise TransportError("UVMOD_HOST is not set (ssh transport)")
        self.ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", settings.host]
        self.u = psq(settings.ue4ss)
        self.prelude = f"""$u = {self.u}
$proc = {psq(settings.process)}
if (-not $proc) {{
  $exe = Get-ChildItem -File -Filter '*-Shipping.exe' -Path (Split-Path $u), $u -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($exe) {{ $proc = $exe.BaseName }}
}}
function Get-Game {{ if ($proc) {{ Get-Process -Name $proc -ErrorAction SilentlyContinue }} }}
"""

    def _command(self) -> list[str]:
        return self.ssh + [f"powershell -NoProfile -NonInteractive -EncodedCommand {_BOOT_ENCODED}"]

    def powershell(self, script: str, timeout: float | None = None) -> str:
        payload = base64.b64encode((self.prelude + script).encode("utf-8"))
        try:
            done = subprocess.run(self._command(), input=payload, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TransportError(f"ssh {self.settings.host}: no answer within {timeout:.0f} s") from None
        out = done.stdout.decode("utf-8", errors="replace").replace("\r", "")
        if done.returncode == 255:
            raise TransportError(f"ssh {self.settings.host} failed: {done.stderr.decode(errors='replace').strip()}")
        return out

    # --- requests ------------------------------------------------------------------------------------------------

    def exchange(self, request_id: str, native: bytes, slot: bytes, timeout: float) -> Exchange:
        script = f"""
$dir = Join-Path $u '{protocol.FOLDER}'
if (-not (Test-Path $dir)) {{ 'missing'; exit 0 }}
$info = Join-Path $dir '{protocol.BRIDGE_INFO}'
$fresh = (Test-Path $info) -and (((Get-Date) - (Get-Item $info).LastWriteTime).TotalSeconds -le {protocol.BRIDGE_FRESH_SECONDS})
$out = Join-Path $dir '{request_id}.response.json'
$deadline = (Get-Date).AddSeconds({timeout})
if ($fresh) {{
  $delivery = 'bridge'; $target = Join-Path $dir '{protocol.BRIDGE_SLOT}'; $payload = '{base64.b64encode(slot).decode()}'
  while ((Test-Path $target) -and (Get-Date) -lt $deadline) {{ Start-Sleep -Milliseconds 10 }}
  if (Test-Path $target) {{ 'bridge'; 'TIMEOUT'; exit 0 }}
}} else {{
  $delivery = 'native'; $target = Join-Path $dir '{request_id}.request.json'; $payload = '{base64.b64encode(native).decode()}'
}}
$tmp = $target + '.tmp'
[IO.File]::WriteAllBytes($tmp, [Convert]::FromBase64String($payload))
Move-Item -Force $tmp $target
$text = $null
while ((Get-Date) -lt $deadline) {{
  if (Test-Path $out) {{ try {{ $text = [Convert]::ToBase64String([IO.File]::ReadAllBytes($out)); Remove-Item $out; break }} catch {{ }} }}
  Start-Sleep -Milliseconds 10
}}
$delivery
if ($text) {{ $text }} else {{
  if ($delivery -eq 'bridge') {{ try {{ if (((Get-Content -Raw $target) | ConvertFrom-Json).id -eq '{request_id}') {{ Remove-Item $target }} }} catch {{ }} }}
  else {{ Remove-Item $target -ErrorAction SilentlyContinue }}
  'TIMEOUT'
}}
"""
        lines = self.powershell(script, timeout=timeout + 30).strip().splitlines()
        if not lines:
            raise TransportError("the game box sent no answer")
        if lines[0] == "missing":
            return Exchange("missing", None)
        delivery = lines[0]
        if len(lines) < 2 or lines[1] == "TIMEOUT":
            return Exchange(delivery, None)
        return Exchange(delivery, base64.b64decode(lines[1]).decode("utf-8", errors="replace"))

    # --- the game and its files --------------------------------------------------------------------------------

    def status(self, status_pattern: str) -> Status:
        script = f"""
$p = Get-Game | Select-Object -First 1
$l = Join-Path $u 'UE4SS.log'; $ini = Join-Path $u 'UE4SS-settings.ini'; $dir = Join-Path $u '{protocol.FOLDER}'
$info = Join-Path $dir '{protocol.BRIDGE_INFO}'
$r = [ordered]@{{
  process = [string]$proc; running = [bool]$p; pid = $(if ($p) {{ $p.Id }} else {{ $null }})
  started = $(if ($p) {{ $p.StartTime.ToString('HH:mm:ss') }} else {{ '' }})
  log_time = $(if (Test-Path $l) {{ (Get-Item $l).LastWriteTime.ToString('HH:mm:ss') }} else {{ '' }})
  log_lines = @($(if (Test-Path $l) {{ Get-Content -Encoding UTF8 $l | Where-Object {{ $_ -match {psq(status_pattern)} }} | Select-Object -Last 6 | ForEach-Object {{ [string]$_ }} }}))
  repl_setting = [string](Select-String -Path $ini -Pattern '^\\s*ExternalLuaReplEnabled\\s*=' -ErrorAction SilentlyContinue | Select-Object -Last 1).Line
  hot_reload_setting = [string](Select-String -Path $ini -Pattern '^\\s*EnableHotReloadSystem\\s*=' -ErrorAction SilentlyContinue | Select-Object -Last 1).Line
  repl_folder = [bool](Test-Path $dir)
  bridge_age = $(if (Test-Path $info) {{ ((Get-Date) - (Get-Item $info).LastWriteTime).TotalSeconds }} else {{ $null }})
  bridge_installed = [bool](Test-Path (Join-Path $u 'Mods\\VibeBridge\\enabled.txt'))
}}
$r | ConvertTo-Json -Compress
"""
        data = json.loads(self.powershell(script, timeout=60).strip() or "{}")
        s = Status(**{k: v for k, v in data.items() if k in Status.__dataclass_fields__})
        s.process = s.process or ""
        if not s.process:
            s.running = None
        s.log_lines = [l for l in (s.log_lines or []) if l]
        return s

    def log(self, lines: int, pattern: str = "") -> list[str]:
        tail = 20000 if pattern else max(lines, 1)
        filt = f"$lines = @($lines | Where-Object {{ $_ -match {psq(pattern)} }}) | Select-Object -Last {lines}" if pattern else ""
        out = self.powershell(f"$lines = Get-Content -Encoding UTF8 -Tail {tail} (Join-Path $u 'UE4SS.log')\n{filt}\n$lines", timeout=60)
        return out.splitlines()[-lines:] if lines > 0 else []

    def follow(self, pattern: str = "") -> Iterator[str]:
        script = f"Get-Content -Encoding UTF8 -Tail 0 -Wait (Join-Path $u 'UE4SS.log') | Where-Object {{ $_ -match {psq(pattern or '.')} }}"
        payload = base64.b64encode((self.prelude + script).encode("utf-8"))
        proc = subprocess.Popen(self._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        assert proc.stdin and proc.stdout
        proc.stdin.write(payload)
        proc.stdin.close()
        try:
            for raw in proc.stdout:
                yield raw.decode("utf-8", errors="replace").rstrip("\r\n")
        finally:
            proc.kill()

    @staticmethod
    def _tar(local_dir: str) -> str:
        src = Path(local_dir)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            tar.add(src, arcname=src.name, filter=lambda ti: None if ti.name.startswith(f"{src.name}/hotswap") else ti)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def _push_script(self, local_dir: str) -> str:
        return f"""
$mods = Join-Path $u 'Mods'
$tar = Join-Path $env:TEMP 'uvmod-push.tar'
[IO.File]::WriteAllBytes($tar, [Convert]::FromBase64String('{self._tar(local_dir)}'))
tar -xf $tar -C $mods
Remove-Item $tar
"""

    def push(self, local_dir: str) -> str:
        name = Path(local_dir).name
        script = self._push_script(local_dir) + f"""
$n = (Get-ChildItem -Recurse -File (Join-Path $mods {psq(name)})).Count
"pushed {name} -> $mods\\{name} ($n files)"
"""
        return self.powershell(script, timeout=120).strip()

    def swap(self, local_dir: str, swap_id: str, timeout: float) -> str | None:
        name = Path(local_dir).name
        script = self._push_script(local_dir) + f"""
$hs = Join-Path $mods {psq(name + '/hotswap')}
New-Item -ItemType Directory -Force $hs | Out-Null
$req = Join-Path $hs 'req'; $tmp = Join-Path $hs 'req.tmp'; $out = Join-Path $hs '{swap_id}.out'
[IO.File]::WriteAllBytes($tmp, [Text.Encoding]::ASCII.GetBytes('{swap_id}'))
Move-Item -Force $tmp $req
$deadline = (Get-Date).AddSeconds({timeout})
while (-not (Test-Path $out) -and (Get-Date) -lt $deadline) {{ Start-Sleep -Milliseconds 20 }}
if (Test-Path $out) {{ 'ANSWER'; [IO.File]::ReadAllText($out, [Text.Encoding]::UTF8); Remove-Item $out }}
else {{ Remove-Item $req -ErrorAction SilentlyContinue; 'TIMEOUT' }}
"""
        out = self.powershell(script, timeout=timeout + 120).strip()
        if out.startswith("ANSWER\n"):
            return out[len("ANSWER\n"):]
        return None

    def newest_shot(self) -> tuple[str, str] | None:
        script = f"""
$d = {psq(self.settings.shots)}
if (-not $d) {{ $d = Join-Path $env:LOCALAPPDATA (($proc -replace '-(Win64|WinGDK)-Shipping$', '') + '\\Saved\\Screenshots\\Windows') }}
$f = Get-ChildItem -File -Filter *.png $d -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($f) {{ $f.FullName.Replace('\\','/') + '|' + $f.LastWriteTime.Ticks }}
"""
        out = self.powershell(script, timeout=60).strip()
        if "|" not in out:
            return None
        path, stamp = out.rsplit("|", 1)
        return path, stamp

    def make_folder(self, name: str) -> None:
        self.powershell(f"New-Item -ItemType Directory -Force (Join-Path $u {psq(name)}) | Out-Null", timeout=60)

    def fetch(self, path: str, dest: str) -> None:
        done = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", f"{self.settings.host}:{path}", dest],
                              capture_output=True, timeout=120)
        if done.returncode != 0:
            raise TransportError(f"scp failed: {done.stderr.decode(errors='replace').strip()}")
