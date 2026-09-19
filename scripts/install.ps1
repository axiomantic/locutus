<#
.SYNOPSIS
    Locutus Windows PowerShell Installer
.DESCRIPTION
    Downloads and installs the latest Locutus native binary for Windows x86_64,
    places it in %LOCALAPPDATA%\Programs\locutus, and configures the User PATH.
.EXAMPLE
    irm https://raw.githubusercontent.com/axiomantic/locutus/main/scripts/install.ps1 | iex
#>

[CmdletBinding()]
param(
    [string]$Version = $env:LOCUTUS_VERSION
)

$ErrorActionPreference = "Stop"
$Repo = "axiomantic/locutus"
$GitHubUrl = "https://github.com/$Repo"

Write-Host "=== Locutus Windows Installer ===" -ForegroundColor Cyan

# 1. Check Architecture
if (-not [Environment]::Is64BitOperatingSystem) {
    Write-Error "Locutus requires a 64-bit Windows operating system (x86_64)."
    exit 1
}

# 2. Resolve Release Version
if (-not $Version) {
    Write-Host "Fetching latest release information from GitHub..."
    try {
        $releaseApi = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -UseBasicParsing
        $Version = $releaseApi.tag_name
        Write-Host "Latest release found: $Version" -ForegroundColor Green
    }
    catch {
        $Version = "v1.0.0"
        Write-Warning "Could not query GitHub API, defaulting to $Version"
    }
}
elseif (-not ($Version.StartsWith("v"))) {
    $Version = "v$Version"
}

# 3. Setup Paths
$zipFile = "locutus-windows-amd64.zip"
$downloadUrl = "$GitHubUrl/releases/download/$Version/$zipFile"
$tempDir = Join-Path $env:TEMP ([System.IO.Path]::GetRandomFileName())
New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
$tempZip = Join-Path $tempDir $zipFile

# 4. Download Release Archive
Write-Host "Downloading Locutus from $downloadUrl..."
try {
    Invoke-WebRequest -Uri $downloadUrl -OutFile $tempZip -UseBasicParsing
}
catch {
    Write-Error "Failed to download $downloadUrl. Ensure the release exists at $GitHubUrl/releases"
    Remove-Item -Path $tempDir -Recurse -Force -ErrorAction SilentlyContinue
    exit 1
}

# 5. Extract Binary
$installDir = Join-Path $env:LOCALAPPDATA "Programs\locutus"
if (-not (Test-Path $installDir)) {
    New-Item -ItemType Directory -Path $installDir -Force | Out-Null
}

Write-Host "Extracting archive to $installDir..."
Expand-Archive -Path $tempZip -DestinationPath $installDir -Force
Remove-Item -Path $tempDir -Recurse -Force -ErrorAction SilentlyContinue

$exePath = Join-Path $installDir "locutus.exe"
if (-not (Test-Path $exePath)) {
    Write-Error "locutus.exe was not found in the extracted package."
    exit 1
}

# 6. Configure User PATH
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -split ";" -notcontains $installDir) {
    Write-Host "Adding $installDir to User PATH..." -ForegroundColor Yellow
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$installDir", "User")
    $env:Path = "$env:Path;$installDir"
}

Write-Host "✓ Locutus successfully installed to: $exePath" -ForegroundColor Green

# 7. Verification
try {
    & $exePath --help | Out-Null
    Write-Host "✓ Locutus executable verified." -ForegroundColor Green
}
catch {
    Write-Warning "Executable ran with exit code or warnings."
}

Write-Host ""
Write-Host "To start using Locutus, reopen your terminal and run:" -ForegroundColor Cyan
Write-Host "    locutus --help" -ForegroundColor White
Write-Host ""
