# Windows 安装入口：WSL 运行成品包，避免在 Windows 安装 Python/Node。
param([ValidateSet('local','server')][string]$Mode,[int]$Port=8020,[string]$Image,[string[]]$CertificateHost=@(),[switch]$Offline,[switch]$Repair,[switch]$Performance,[switch]$Android)
$ErrorActionPreference='Stop'
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { throw '请先安装 WSL 和 Docker Desktop，并启用该发行版的 Docker 集成。https://docs.docker.com/desktop/setup/install/windows-install/' }
$installPath=& wsl.exe -d Ubuntu --exec wslpath -a $PSScriptRoot
if ($LASTEXITCODE -ne 0) { throw '未找到 Ubuntu WSL，请安装 Ubuntu 或在其他 Linux 环境运行 install.sh。' }
$arguments=@('-d','Ubuntu','--exec','bash',"$($installPath.Trim())/install.sh",'--port',"$Port")
if ($Mode) {$arguments+=@('--mode',$Mode)}
if ($Image) {$arguments+=@('--image',$Image)}
foreach ($certificateName in $CertificateHost) {$arguments+=@('--host',$certificateName)}
if ($Offline) {$arguments+='--offline'}
if ($Repair) {$arguments+='--repair'}
if ($Performance) {$arguments+='--performance'}
if ($Android) {$arguments+='--android'}
& wsl.exe @arguments
if ($LASTEXITCODE -ne 0) { throw 'TestPilot 安装未完成，请查看上方诊断；不会清除已有数据。' }
