<#
.SYNOPSIS
    Locutus Windows PowerShell Installer & Uninstaller
.DESCRIPTION
    Installs Locutus for Windows x86_64:
    1. Installs via Scoop if Scoop is present.
    2. Downloads and installs pre-compiled binary zip from GitHub Releases.
    3. Falls back to installing Nim and compiling from source if no binary is found.
    Configures %LOCALAPPDATA%\Programs\locutus and updates the User PATH.
.EXAMPLE
    # Install:
    irm https://raw.githubusercontent.com/axiomantic/locutus/main/scripts/install.ps1 | iex

    # Uninstall:
    & .\scripts\install.ps1 -Uninstall
#>

[CmdletBinding()]
param(
    [string]$Version = $env:LOCUTUS_VERSION,
    [switch]$Uninstall,
    [switch]$BuildFromSource
)

$ErrorActionPreference = "Stop"
$Repo = "axiomantic/locutus"
$GitHubUrl = "https://github.com/$Repo"
$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\locutus"

# 0. Handle Uninstallation
if ($Uninstall) {
    Write-Host "=== Locutus Windows Uninstaller ===" -ForegroundColor Cyan
    $removed = $false

    # Check Scoop
    if ((Get-Command scoop -ErrorAction SilentlyContinue) -and (scoop list | Select-String "^locutus\b")) {
        Write-Host "Detected Scoop package. Uninstalling via Scoop..." -ForegroundColor Yellow
        scoop uninstall locutus
        $removed = $true
    }

    # Check Standalone Directory
    if (Test-Path $InstallDir) {
        Write-Host "Removing installation directory: $InstallDir..." -ForegroundColor Yellow
        Remove-Item -Path $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
        $removed = $true
    }

    # Clean User PATH
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -split ";" -contains $InstallDir) {
        Write-Host "Removing $InstallDir from User PATH..." -ForegroundColor Yellow
        $newPath = ($userPath -split ";" | Where-Object { $_ -ne $InstallDir -and $_ -ne "" }) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        $removed = $true
    }

    if ($removed) {
        Write-Host "✓ Locutus has been successfully uninstalled." -ForegroundColor Green
        Write-Host "Note: Configuration files in %APPDATA%\locutus were preserved."
    } else {
        Write-Host "Locutus does not appear to be installed on this system." -ForegroundColor Gray
    }
    exit 0
}

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

# Helper: Build from Source
function Build-FromSource {
    Write-Host "`n=== Building Locutus from Source ===" -ForegroundColor Cyan

    # Check for Nim
    if (-not (Get-Command nim -ErrorAction SilentlyContinue)) {
        Write-Host "Nim compiler not detected. Attempting automatic installation..." -ForegroundColor Yellow
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Write-Host "Installing Nim via winget..."
            winget install --id Nim.Nim -e --silent --accept-source-agreements --accept-package-agreements
            $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
        }
    }

    if (-not (Get-Command nim -ErrorAction SilentlyContinue)) {
        Write-Error "Nim compiler not found. Please install Nim: https://nim-lang.org/install_windows.html"
        exit 1
    }

    Write-Host "Using Nim: $((nim --version)[0])" -ForegroundColor Green

    $tempDir = Join-Path $env:TEMP ([System.IO.Path]::GetRandomFileName())
    New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
    $srcZip = Join-Path $tempDir "source.zip"

    Write-Host "Fetching Locutus source ($Version)..."
    $srcUrl = "$GitHubUrl/archive/refs/tags/$Version.zip"
    try {
        Invoke-WebRequest -Uri $srcUrl -OutFile $srcZip -UseBasicParsing
    } catch {
        Write-Host "Release zip not found, falling back to main branch..."
        Invoke-WebRequest -Uri "$GitHubUrl/archive/refs/heads/main.zip" -OutFile $srcZip -UseBasicParsing
    }

    Expand-Archive -Path $srcZip -DestinationPath $tempDir -Force
    $extractedDir = Get-ChildItem -Path $tempDir -Directory | Select-Object -First 1

    if (-not (Test-Path $InstallDir)) {
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    }

    Write-Host "Compiling native binary with optimizations..."
    Push-Location $extractedDir.FullName
    try {
        nim c -d:release --opt:speed -o:"$InstallDir\locutus.exe" src\locutus.nim
    } finally {
        Pop-Location
        Remove-Item -Path $tempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# 3. Check for Scoop Package Manager
if (-not $BuildFromSource -and (Get-Command scoop -ErrorAction SilentlyContinue)) {
    Write-Host "Detected Scoop package manager. Installing via Scoop..." -ForegroundColor Green
    if (scoop install "https://raw.githubusercontent.com/$Repo/main/packaging/scoop/locutus.json") {
        Write-Host "✓ Locutus successfully installed via Scoop." -ForegroundColor Green
        exit 0
    }
    Write-Warning "Scoop installation failed. Falling back to binary release..."
}

# 4. Standalone Binary Installation
$installed = $false
if (-not $BuildFromSource) {
    $zipFile = "locutus-windows-amd64.zip"
    $downloadUrl = "$GitHubUrl/releases/download/$Version/$zipFile"
    $tempDir = Join-Path $env:TEMP ([System.IO.Path]::GetRandomFileName())
    New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
    $tempZip = Join-Path $tempDir $zipFile

    Write-Host "Downloading pre-compiled binary from $downloadUrl..."
    try {
        Invoke-WebRequest -Uri $downloadUrl -OutFile $tempZip -UseBasicParsing
        if (-not (Test-Path $InstallDir)) {
            New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
        }
        Write-Host "Extracting archive to $InstallDir..."
        Expand-Archive -Path $tempZip -DestinationPath $InstallDir -Force
        $installed = $true
    }
    catch {
        Write-Warning "Pre-compiled binary was not found or download failed."
    }
    finally {
        Remove-Item -Path $tempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# 5. Fallback: Build from source if binary was not found
if (-not $installed) {
    Build-FromSource
}

$exePath = Join-Path $InstallDir "locutus.exe"
if (-not (Test-Path $exePath)) {
    Write-Error "locutus.exe was not found at $exePath."
    exit 1
}

# 6. Configure User PATH
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -split ";" -notcontains $InstallDir) {
    Write-Host "Adding $InstallDir to User PATH..." -ForegroundColor Yellow
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$InstallDir", "User")
    $env:Path = "$env:Path;$InstallDir"
}

Write-Host "✓ Locutus successfully installed to: $exePath" -ForegroundColor Green

# 7. Verification
try {
    & $exePath --help | Out-Null
    Write-Host "✓ Locutus executable verified and ready to use!" -ForegroundColor Green
    Write-Host "Run 'locutus --help' to get started."
}
catch {
    Write-Warning "Executable installed, but execution check failed. You may need to restart your terminal."
}
