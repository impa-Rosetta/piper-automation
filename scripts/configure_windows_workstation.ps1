param(
    [Parameter(Mandatory = $true)]
    [string]$PiAddress,

    [string]$PiUser = "piper",
    [string]$Alias = "piper-pi",
    [string]$RemoteRoot = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $RemoteRoot) {
    $RemoteRoot = "/home/$PiUser/piper-automation"
}

foreach ($command in @("git", "python", "ssh", "scp")) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Required command '$command' was not found in PATH."
    }
}

$sshDirectory = Join-Path $HOME ".ssh"
$sshConfig = Join-Path $sshDirectory "config"
$keyPath = Join-Path $sshDirectory "id_ed25519"
New-Item -ItemType Directory -Force -Path $sshDirectory | Out-Null

if (-not (Test-Path $keyPath)) {
    Write-Host "No SSH key found. Creating $keyPath ..."
    & ssh-keygen @("-t", "ed25519", "-f", $keyPath, "-N", "", "-C", "piper-operator")
    if ($LASTEXITCODE -ne 0) {
        throw "ssh-keygen failed with exit code $LASTEXITCODE"
    }
}

$begin = "# BEGIN PIPER-AUTOMATION"
$end = "# END PIPER-AUTOMATION"
$existing = if (Test-Path $sshConfig) { Get-Content -Raw $sshConfig } else { "" }
$pattern = "(?ms)^" + [regex]::Escape($begin) + ".*?^" + [regex]::Escape($end) + "\s*"
$existing = [regex]::Replace($existing, $pattern, "")
$block = @"
$begin
Host $Alias
    HostName $PiAddress
    User $PiUser
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    ServerAliveInterval 15
    ServerAliveCountMax 3
$end
"@
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$sshText = $existing.TrimEnd() + "`r`n" + $block + "`r`n"
[IO.File]::WriteAllText($sshConfig, $sshText, $utf8NoBom)

$configDirectory = Join-Path $ProjectRoot "config"
New-Item -ItemType Directory -Force -Path $configDirectory | Out-Null
$workbenchConfig = [ordered]@{
    host = $Alias
    remote_root = $RemoteRoot
    can_port = "can0"
    gripper_port = "/dev/piper_gripper"
    layer = 1
    slot = 1
    sequence_from = 1
    sequence_to = 27
    speed = 10
    play_speed = 1.0
    anchor_speed = 20
}
$jsonPath = Join-Path $configDirectory "windows_remote_workbench.json"
[IO.File]::WriteAllText($jsonPath, ($workbenchConfig | ConvertTo-Json), $utf8NoBom)

Write-Host "SSH alias and Windows workbench configuration saved."
Write-Host "If the public key has not been installed on the Pi, run:"
Write-Host "  Get-Content `$env:USERPROFILE\.ssh\id_ed25519.pub | ssh $PiUser@$PiAddress `"umask 077; mkdir -p ~/.ssh; cat >> ~/.ssh/authorized_keys`""
Write-Host "Then test: ssh $Alias `"hostname; uname -m`""
