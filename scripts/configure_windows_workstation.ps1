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

$publicKeyPath = "$keyPath.pub"
if (-not (Test-Path $publicKeyPath)) {
    throw "SSH public key was not found: $publicKeyPath"
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
Write-Host "Installing this Windows account's public key on the Raspberry Pi ..."
Write-Host "Enter the Raspberry Pi Linux password once when prompted."

$publicKey = (Get-Content -Raw $publicKeyPath).Trim()
$installCommand = @'
umask 077
mkdir -p ~/.ssh
touch ~/.ssh/authorized_keys
key=$(cat)
grep -qxF "$key" ~/.ssh/authorized_keys || printf '%s\n' "$key" >> ~/.ssh/authorized_keys
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys
'@
$publicKey | & ssh "$PiUser@$PiAddress" $installCommand
if ($LASTEXITCODE -ne 0) {
    throw "Public-key installation failed with exit code $LASTEXITCODE"
}

Write-Host "Testing passwordless SSH ..."
& ssh -o BatchMode=yes $Alias "hostname; whoami; uname -m"
if ($LASTEXITCODE -ne 0) {
    throw "Passwordless SSH test failed with exit code $LASTEXITCODE"
}
Write-Host "Passwordless SSH is ready. Reopen the Piper workbench."
