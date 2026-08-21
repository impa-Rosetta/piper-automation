param(
    [string]$PiHost = "piper-pi",
    [string]$RemoteRoot = "/home/piper/piper_robot_project"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

if ($PiHost -notmatch '^[A-Za-z0-9_.@-]+$') {
    throw "PiHost contains unsupported characters: $PiHost"
}
if ($RemoteRoot -notmatch '^/[A-Za-z0-9_./-]+$') {
    throw "RemoteRoot must be a simple absolute Linux path: $RemoteRoot"
}
if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "Windows OpenSSH Client was not found."
}

$OutputDirectory = Join-Path $ProjectRoot "records\handover_diagnostics"
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$OutputPath = Join-Path $OutputDirectory "handover_diagnostics_$Timestamp.log"

$RemoteCommand = @"
set +e
check_rc=0
root='$RemoteRoot'

echo '=== Identity ==='
printf 'user='; whoami
printf 'host='; hostname
printf 'ip='; hostname -I
uname -a

echo '=== Project ==='
echo "remote_root=`$root"
if [ -d "`$root" ]; then echo 'project=present'; else echo 'project=MISSING'; check_rc=1; fi
if [ -d "`$root/.git" ]; then
  git -C "`$root" rev-parse --short HEAD
else
  echo 'deployment=archive/no-git (supported)'
fi
test -f "`$root/teach/windows_remote_workbench.py" || check_rc=1

echo '=== Python and SDK ==='
if [ -f "`$HOME/.venvs/piper_robot_project_api/bin/activate" ]; then
  . "`$HOME/.venvs/piper_robot_project_api/bin/activate"
  python --version
  python -m pip show piper-sdk | sed -n '1,4p'
else
  echo 'venv=MISSING'
  check_rc=1
fi

echo '=== USB ==='
lsusb | grep -E '1d50:606f|0483:5740|CAN|OpenMoko|STMicroelectronics' || check_rc=1

echo '=== CAN ==='
systemctl is-active can0.service || check_rc=1
ip -details -statistics link show can0 || check_rc=1

echo '=== Gripper ==='
id
ls -l /dev/piper_gripper || check_rc=1
if [ -r /dev/piper_gripper ] && [ -w /dev/piper_gripper ]; then
  echo 'gripper_access=read-write'
else
  echo 'gripper_access=FAILED'
  check_rc=1
fi

echo '=== Site data ==='
if [ -d "`$root/teach/production_tasks" ]; then
  printf 'task_count='
  find "`$root/teach/production_tasks" -mindepth 2 -maxdepth 2 -name task.json | wc -l
else
  echo 'task_count=0'
  check_rc=1
fi
for path in teach/feeder_above.json teach/zero_home.json; do
  if [ -f "`$root/`$path" ]; then echo "`$path=present"; else echo "`$path=missing"; fi
done

echo '=== Piper feedback (read-only, 3 seconds) ==='
if [ -x "`$HOME/.venvs/piper_robot_project_api/bin/python" ] && [ -f "`$root/scripts/read_status.py" ]; then
  cd "`$root"
  timeout 3 python -u scripts/read_status.py --can-port can0
  feedback_rc=`$?
  if [ `$feedback_rc -ne 0 ] && [ `$feedback_rc -ne 124 ]; then check_rc=1; fi
else
  echo 'feedback_check=SKIPPED'
  check_rc=1
fi

echo "=== Result: check_rc=`$check_rc ==="
exit `$check_rc
"@

$Header = @(
    "Piper same-Pi handover diagnostics",
    "generated_at=$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')",
    "windows_user=$env:USERNAME",
    "windows_host=$env:COMPUTERNAME",
    "pi_host=$PiHost",
    "remote_root=$RemoteRoot",
    "This report is read-only and sends no motion or gripper command.",
    ""
)
$Header | Set-Content -Encoding UTF8 $OutputPath

Write-Host "Collecting read-only diagnostics from $PiHost ..."
$Output = & ssh -o BatchMode=yes -o ConnectTimeout=8 $PiHost $RemoteCommand 2>&1
$ExitCode = $LASTEXITCODE
$Output | Tee-Object -FilePath $OutputPath -Append

Write-Host ""
Write-Host "Saved: $OutputPath"
if ($ExitCode -ne 0) {
    Write-Host "Diagnostics found a problem (returncode=$ExitCode)."
    Write-Host "Send this log to the assisting engineer or AI; do not include passwords."
    exit $ExitCode
}
Write-Host "Read-only handover diagnostics passed."
