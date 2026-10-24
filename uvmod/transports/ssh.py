"""ssh transport: the game runs on a Windows box reached over SSH (OpenSSH server, key login, Windows PowerShell).
Every operation is one PowerShell script sent over one ssh call."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import tarfile
import threading
from pathlib import Path
from typing import Iterator

from .. import protocol
from . import Exchange, Status, Transport, TransportError

# Reads the script that follows on stdin as one base64 line, says GO, then decodes and runs it; only this fixed
# bootstrap rides the command line, which cmd.exe caps at 8191 characters.
# Now and then a session over Windows OpenSSH never gets its input (a few percent of calls with ~20 KB of input,
# in our measurements). So the read has a time limit, after which PowerShell exits instead of staying forever, and
# GO comes before anything runs: a client that sees no GO can kill the session and try again safely.
GO = "UVMOD-GO"
GO_TIMEOUT = 10.0
ATTEMPTS = 3
_BOOT = (
    '$ProgressPreference="SilentlyContinue"; [Console]::OutputEncoding=[Text.Encoding]::UTF8; '
    "$r=(New-Object IO.StreamReader([Console]::OpenStandardInput())).ReadLineAsync(); "
    "if (-not $r.Wait(20000)) { exit 75 }; "
    f"[Console]::Out.WriteLine('{GO}'); [Console]::Out.Flush(); "
    "$s=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($r.Result)); "
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
        self.retries = 0  # sessions started again because they never read their input
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

    def _start(self, payload: bytes) -> tuple[subprocess.Popen, threading.Thread, list[bytes]]:
        """Start a session, hand it the script, and wait for GO; retry sessions that never read it.
        Returns the process, the thread reading the rest of stdout, and that thread's output buffer."""
        for attempt in range(1, ATTEMPTS + 1):
            proc = subprocess.Popen(self._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert proc.stdin and proc.stdout and proc.stderr
            first: list[bytes] = []
            got_first = threading.Event()

            def read_first(out=proc.stdout, first=first, got_first=got_first):
                first.append(out.readline())
                got_first.set()

            threading.Thread(target=read_first, daemon=True).start()
            try:
                proc.stdin.write(payload)
                proc.stdin.close()
            except OSError:
                pass
            if got_first.wait(GO_TIMEOUT) and first[0].rstrip(b"\r\n") == GO.encode():
                rest: list[bytes] = []
                reader = threading.Thread(target=lambda: rest.append(proc.stdout.read()), daemon=True)
                reader.start()
                return proc, reader, rest
            if not got_first.is_set() or proc.poll() in (None, 75):
                self.retries += 1
                proc.kill()
                proc.wait()
                continue
            # The session ended without GO: ssh or PowerShell failed before the script could run.
            proc.wait()
            err = (proc.stderr.read() + first[0]).decode("utf-8", errors="replace").strip()
            raise TransportError(f"ssh {self.settings.host} failed (exit {proc.returncode}): {err[:500]}")
        raise TransportError(f"ssh {self.settings.host}: the game box's PowerShell did not read its input "
                             f"({ATTEMPTS} sessions, {GO_TIMEOUT:g} s each); nothing was run")

    def powershell(self, script: str, timeout: float | None = None) -> str:
        payload = base64.b64encode((self.prelude + script).encode("utf-8")) + b"\n"
        proc, reader, rest = self._start(payload)
        assert proc.stderr
        errors: list[bytes] = []
        err_reader = threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True)
        err_reader.start()
        reader.join(timeout)
        if reader.is_alive():
            proc.kill()
            proc.wait()
            raise TransportError(f"ssh {self.settings.host}: the script started but did not finish within {timeout:.0f} s")
        proc.wait()
        err_reader.join(5)
        if proc.returncode == 255:
            raise TransportError(f"ssh {self.settings.host} failed: {b''.join(errors).decode(errors='replace').strip()}")
        return b"".join(rest).decode("utf-8", errors="replace").replace("\r", "")

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
  # One slot: [IO.File]::Move fails while a request is there, so concurrent callers take turns.
  $delivery = 'bridge'; $target = Join-Path $dir '{protocol.BRIDGE_SLOT}'; $tmp = $target + '.{request_id}.tmp'
  [IO.File]::WriteAllBytes($tmp, [Convert]::FromBase64String('{base64.b64encode(slot).decode()}'))
  $placed = $false
  while (-not $placed) {{
    try {{ [IO.File]::Move($tmp, $target); $placed = $true }}
    catch {{ if ((Get-Date) -ge $deadline) {{ Remove-Item $tmp -ErrorAction SilentlyContinue; 'bridge'; 'TIMEOUT'; exit 0 }}; Start-Sleep -Milliseconds 10 }}
  }}
}} else {{
  $delivery = 'native'; $target = Join-Path $dir '{request_id}.request.json'; $tmp = $target + '.tmp'
  [IO.File]::WriteAllBytes($tmp, [Convert]::FromBase64String('{base64.b64encode(native).decode()}'))
  Move-Item -Force $tmp $target
}}
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
        payload = base64.b64encode((self.prelude + script).encode("utf-8")) + b"\n"
        # The handshake as in powershell(), but streaming: lines are read here, not by the reader thread.
        for attempt in range(1, ATTEMPTS + 1):
            proc = subprocess.Popen(self._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            assert proc.stdin and proc.stdout
            proc.stdin.write(payload)
            proc.stdin.close()
            first: list[bytes] = []
            reader = threading.Thread(target=lambda out=proc.stdout, first=first: first.append(out.readline()), daemon=True)
            reader.start()
            reader.join(GO_TIMEOUT)
            if first and first[0].rstrip(b"\r\n") == GO.encode():
                break
            self.retries += 1
            proc.kill()
            proc.wait()
        else:
            raise TransportError(f"ssh {self.settings.host}: the game box's PowerShell did not read its input")
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

    def press_keys(self, codes: list[int], hold_ms: int) -> str:
        # Keys pressed from the SSH session reach no desktop: the presser runs as a scheduled task in the logged-in
        # session, and writes back which window was in front.
        from ..keys import script as key_script

        body = key_script("__PROC__", codes, hold_ms, "__OUT__")
        script = f"""
$out = Join-Path $env:TEMP 'uvmod-key.out'
$ps1 = Join-Path $env:TEMP 'uvmod-key.ps1'
Remove-Item $out -ErrorAction SilentlyContinue
$body = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{base64.b64encode(body.encode("utf-8")).decode()}'))
$body = $body.Replace('__PROC__', $proc).Replace('__OUT__', $out)
Set-Content -Path $ps1 -Value $body -Encoding UTF8
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $ps1 + '"')
$p = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\\$env:USERNAME" -LogonType Interactive
Register-ScheduledTask -TaskName 'uvmod-key' -Action $a -Principal $p -Force | Out-Null
Start-ScheduledTask -TaskName 'uvmod-key'
$sw = [Diagnostics.Stopwatch]::StartNew()
while (-not (Test-Path $out) -and $sw.Elapsed.TotalSeconds -lt 20) {{ Start-Sleep -Milliseconds 100 }}
if (Test-Path $out) {{ Get-Content $out }} else {{ 'ERROR: the key presser did not finish (is a user logged in?)' }}
"""
        out = self.powershell(script, timeout=90).strip()
        if out.startswith("ERROR: "):
            raise TransportError(out[len("ERROR: "):])
        return out

    def recent_shots(self, count: int) -> list[str]:
        script = f"""
$d = {psq(self.settings.shots)}
if (-not $d) {{ $d = Join-Path $env:LOCALAPPDATA (($proc -replace '-(Win64|WinGDK)-Shipping$', '') + '\\Saved\\Screenshots\\Windows') }}
Get-ChildItem -File -Filter *.png $d -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending |
  Select-Object -First {int(count)} | ForEach-Object {{ $_.FullName.Replace('\\','/') }}
"""
        return [l for l in self.powershell(script, timeout=60).splitlines() if l.strip()]

    def make_folder(self, name: str) -> None:
        self.powershell(f"New-Item -ItemType Directory -Force (Join-Path $u {psq(name)}) | Out-Null", timeout=60)

    # --- starting and stopping the game ---------------------------------------------------------------------------

    def launch(self) -> str:
        # A program started over SSH runs in the SSH session, where a game has no desktop. A scheduled task with an
        # interactive principal starts it in the logged-in user's session instead.
        script = f"""
$launch = {psq(self.settings.launch)}
$appid = {psq(self.settings.appid)}
if ($launch) {{ $exe = 'cmd.exe'; $arg = '/c ' + $launch }}
else {{
  if (-not $appid) {{
    $common = $u -replace '(?i)(\\\\steamapps\\\\common\\\\[^\\\\]+).*$', '$1'
    $lib = Split-Path (Split-Path $common)
    $dir = Split-Path $common -Leaf
    foreach ($m in Get-ChildItem -File -Filter 'appmanifest_*.acf' $lib -ErrorAction SilentlyContinue) {{
      $t = Get-Content -Raw $m.FullName
      if ($t -match ('"installdir"\\s+"' + [regex]::Escape($dir) + '"') -and $t -match '"appid"\\s+"(\\d+)"') {{ $appid = $Matches[1]; break }}
    }}
  }}
  if (-not $appid) {{ 'ERROR: no Steam app id: set UVMOD_APPID (or UVMOD_LAUNCH)'; exit 0 }}
  $steam = (Get-ItemProperty 'HKCU:\\Software\\Valve\\Steam' -ErrorAction SilentlyContinue).SteamExe
  if (-not $steam) {{ $steam = 'C:\\Program Files (x86)\\Steam\\steam.exe' }}
  $exe = $steam; $arg = '-applaunch ' + $appid
}}
try {{
  $a = New-ScheduledTaskAction -Execute $exe -Argument $arg
  $p = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\\$env:USERNAME" -LogonType Interactive
  Register-ScheduledTask -TaskName 'uvmod-launch' -Action $a -Principal $p -Force -ErrorAction Stop | Out-Null
  Start-ScheduledTask -TaskName 'uvmod-launch' -ErrorAction Stop
  "started $exe $arg (in the logged-in session)"
}} catch {{ 'ERROR: ' + $_.Exception.Message }}
"""
        out = self.powershell(script, timeout=60).strip()
        if out.startswith("ERROR: "):
            raise TransportError(out[len("ERROR: "):])
        return out

    def stop(self) -> str:
        script = """
if (-not $proc) { 'ERROR: unknown game process (set UVMOD_PROCESS)'; exit 0 }
if (-not (Get-Game)) { "$proc is not running"; exit 0 }
Get-Process -Name $proc, CrashReportClient -ErrorAction SilentlyContinue | Stop-Process -Force
$sw = [Diagnostics.Stopwatch]::StartNew()
while ((Get-Game) -and $sw.Elapsed.TotalSeconds -lt 10) { Start-Sleep -Milliseconds 200 }
if (Get-Game) { "$proc is still running" } else { "stopped $proc" }
"""
        out = self.powershell(script, timeout=60).strip()
        if out.startswith("ERROR: "):
            raise TransportError(out[len("ERROR: "):])
        return out

    # --- crashes --------------------------------------------------------------------------------------------------

    def crash_files(self) -> dict:
        script = """
function T($i) { [DateTimeOffset]::new($i.LastWriteTimeUtc).ToUnixTimeSeconds() }
$r = [ordered]@{ folders = @(); dumps = @() }
if ($proc) {
  $c = Join-Path $env:LOCALAPPDATA (($proc -replace '-(Win64|WinGDK)-Shipping$', '') + '\\Saved\\Crashes')
  $r.folders = @(Get-ChildItem -Directory $c -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 3 |
    ForEach-Object { [ordered]@{ path = $_.FullName.Replace('\\','/'); time = (T $_); files = @(Get-ChildItem -File $_.FullName | ForEach-Object { [string]$_.Name }) } })
}
$r.dumps = @(Get-ChildItem -File -Filter 'crash_*.dmp' $u -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 3 |
  ForEach-Object { [ordered]@{ path = $_.FullName.Replace('\\','/'); time = (T $_) } })
$r | ConvertTo-Json -Compress -Depth 5
"""
        data = json.loads(self.powershell(script, timeout=60).strip() or "{}")
        for key in ("folders", "dumps"):
            value = data.get(key) or []
            data[key] = value if isinstance(value, list) else [value]
            for item in data[key]:
                files = item.get("files")
                if files is not None and not isinstance(files, list):
                    item["files"] = [files]
        return data

    def read_text(self, path: str) -> str:
        out = self.powershell(f"[Convert]::ToBase64String([IO.File]::ReadAllBytes({psq(path)}))", timeout=60).strip()
        return base64.b64decode(out).decode("utf-8", errors="replace")

    def files(self, pattern: str) -> list[dict]:
        folder, _, leaf = pattern.replace("/", "\\").rpartition("\\")
        script = f"""
$d = if ({psq(folder)}) {{ Join-Path $u {psq(folder)} }} else {{ $u }}
@(Get-ChildItem -File -Filter {psq(leaf or '*')} $d -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending |
  ForEach-Object {{ [ordered]@{{ path = $_.FullName.Replace('\\','/'); time = [DateTimeOffset]::new($_.LastWriteTimeUtc).ToUnixTimeSeconds(); size = $_.Length }} }}) |
  ConvertTo-Json -Compress -Depth 3
"""
        out = self.powershell(script, timeout=60).strip()
        data = json.loads(out) if out else []
        return data if isinstance(data, list) else [data]

    def fetch(self, path: str, dest: str) -> None:
        done = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", f"{self.settings.host}:{path}", dest],
                              capture_output=True, timeout=120)
        if done.returncode != 0:
            raise TransportError(f"scp failed: {done.stderr.decode(errors='replace').strip()}")

    def fetch_large(self, path: str, dest: str) -> None:
        """A big file (a reflection dump): gzip it on the box with Windows' own tar, copy that, unpack it here."""
        script = f"""
$src = {psq(path)}
$tgz = Join-Path $env:TEMP 'uvmod-fetch.tgz'
Remove-Item $tgz -ErrorAction SilentlyContinue
tar -czf $tgz -C (Split-Path $src) (Split-Path $src -Leaf) 2>&1 | Out-Null
if (Test-Path $tgz) {{ $tgz.Replace('\\','/') }} else {{ 'ERROR: tar could not pack ' + $src }}
"""
        remote = self.powershell(script, timeout=900).strip()
        if remote.startswith("ERROR: ") or not remote:
            raise TransportError(remote[len("ERROR: "):] or "tar gave no output")
        local_tgz = dest + ".tgz"
        done = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", f"{self.settings.host}:{remote}", local_tgz],
                              capture_output=True, timeout=1800)
        self.powershell(f"Remove-Item {psq(remote)} -ErrorAction SilentlyContinue", timeout=60)
        if done.returncode != 0:
            raise TransportError(f"scp failed: {done.stderr.decode(errors='replace').strip()}")
        try:
            with tarfile.open(local_tgz, "r:gz") as tar:
                member = next(m for m in tar.getmembers() if m.isfile())
                with tar.extractfile(member) as src, open(dest, "wb") as out:  # type: ignore[union-attr]
                    while chunk := src.read(1 << 20):
                        out.write(chunk)
        finally:
            Path(local_tgz).unlink(missing_ok=True)
