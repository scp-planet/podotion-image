function Get-Sha256Hex([string]$Path) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $hash = $sha.ComputeHash($stream)
    } finally {
        $stream.Dispose()
        $sha.Dispose()
    }
    return ([System.BitConverter]::ToString($hash)).Replace('-', '').ToLowerInvariant()
}
$ErrorActionPreference = 'Stop'

$BinDir = $PSScriptRoot
$SkillDir = Split-Path -Parent $BinDir
$ManifestPath = Join-Path $SkillDir 'runtime.json'
if (-not (Test-Path -LiteralPath $ManifestPath)) {
    Write-Error "Podotion Image runtime manifest is missing: $ManifestPath"
    exit 1
}

try {
    $Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    Write-Error "Podotion Image runtime manifest is invalid."
    exit 1
}

$Architecture = $env:PROCESSOR_ARCHITECTURE
if ($env:PROCESSOR_ARCHITEW6432) {
    $Architecture = $env:PROCESSOR_ARCHITEW6432
}
if (-not $Architecture) {
    Write-Error "Cannot determine the Windows architecture."
    exit 1
}
switch ($Architecture.ToUpperInvariant()) {
    'AMD64' { $Arch = 'amd64' }
    'ARM64' { $Arch = 'arm64' }
    default {
        Write-Error "Unsupported Windows architecture: $Architecture"
        exit 1
    }
}

$Key = "windows-$Arch"
$AssetProperty = $Manifest.assets.PSObject.Properties[$Key]
if (-not $AssetProperty -or -not $AssetProperty.Value) {
    Write-Error "No Podotion Image runtime is published for $Key."
    exit 1
}
$Asset = [string]$AssetProperty.Value
$Version = [string]$Manifest.version
$Tag = [string]$Manifest.tag
$Repository = [string]$Manifest.repository

$HomeDir = $HOME
if (-not $HomeDir) {
    $HomeDir = $env:USERPROFILE
}
$CodexHome = $env:CODEX_HOME
if (-not $CodexHome) {
    $CodexHome = Join-Path $HomeDir '.codex'
}
if ($CodexHome -eq '~') {
    $CodexHome = $HomeDir
} elseif ($CodexHome.StartsWith('~/') -or $CodexHome.StartsWith('~\')) {
    $CodexHome = Join-Path $HomeDir $CodexHome.Substring(2)
}
if (-not [System.IO.Path]::IsPathRooted($CodexHome)) {
    Write-Error "CODEX_HOME must be an absolute native path."
    exit 1
}

$CacheDir = Join-Path $CodexHome "podotion-image\runtime\$Version\$Key"
$Binary = Join-Path $CacheDir 'podotion-image.exe'

if (-not (Test-Path -LiteralPath $Binary)) {
    New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
    $Lock = Join-Path $CacheDir '.download.lock'
    $OwnsLock = $false
    for ($Attempt = 0; $Attempt -lt 120; $Attempt++) {
        if (Test-Path -LiteralPath $Binary) { break }
        try {
            New-Item -ItemType Directory -Path $Lock -ErrorAction Stop | Out-Null
            $OwnsLock = $true
            break
        } catch {
            if (Test-Path -LiteralPath $Binary) { break }
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not (Test-Path -LiteralPath $Binary)) {
        if (-not $OwnsLock) {
            Write-Error "Timed out waiting for another Podotion Image runtime download."
            exit 1
        }
        $TempBinary = Join-Path $CacheDir ("$Asset.download-" + [guid]::NewGuid().ToString('N'))
        $TempHash = "$TempBinary.sha256"
        try {
            $BaseUrl = [string]$Manifest.release_base_url
            if (-not $BaseUrl) {
                $BaseUrl = "https://github.com/$Repository/releases/download/$Tag"
            }
            $AssetUrl = "$BaseUrl/$Asset"
            Invoke-WebRequest -UseBasicParsing -Uri $AssetUrl -OutFile $TempBinary
            $HashText = (Invoke-WebRequest -UseBasicParsing -Uri "$AssetUrl.sha256").Content
            $Match = [regex]::Match([string]$HashText, '[0-9a-fA-F]{64}')
            if (-not $Match.Success) {
                throw "The published SHA-256 file is invalid."
            }
            $Expected = $Match.Value.ToLowerInvariant()
            $Actual = Get-Sha256Hex $TempBinary
            if ($Expected -ne $Actual) {
                throw "Downloaded binary checksum verification failed."
            }
            Move-Item -LiteralPath $TempBinary -Destination $Binary -Force
        } catch {
            Write-Error ("Podotion Image runtime download failed: " + $_.Exception.Message)
            exit 1
        } finally {
            Remove-Item -LiteralPath $TempBinary -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $TempHash -Force -ErrorAction SilentlyContinue
            if ($OwnsLock) {
                Remove-Item -LiteralPath $Lock -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

& $Binary @args
if ($null -eq $LASTEXITCODE) {
    exit 1
}
exit $LASTEXITCODE