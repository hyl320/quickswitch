# Build a portable single-file release of QuickSwitch.
#
# Usage:
#   python -m pip install -r requirements-build.txt
#   .\build-release.ps1 -Version 1.1.0
#
# Output:
#   dist/QuickSwitch.exe
#   dist/QuickSwitch.exe.sha256
#Requires -Version 5.1
param(
    [string]$Version = "1.1.0",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "== QuickSwitch release build v$Version =="

python -m PyInstaller --version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed. Run: python -m pip install -r requirements-build.txt"
}

if (-not $SkipTests) {
    Write-Host "Running unit tests..."
    python -m pytest tests -q
    if ($LASTEXITCODE -ne 0) { throw "Unit tests failed; aborting release build." }
}

$parts = @($Version -replace '[^0-9.].*$', '' -split '\.')
while ($parts.Count -lt 4) { $parts += "0" }
$v1, $v2, $v3, $v4 = $parts

$buildDir = Join-Path $Root "build"
$distDir = Join-Path $Root "dist"
New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
$versionFile = Join-Path $buildDir "version_info.txt"

$versionInfo = @"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($v1, $v2, $v3, $v4),
    prodvers=($v1, $v2, $v3, $v4),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', 'hyl320'),
         StringStruct('FileDescription', 'QuickSwitch - Windows window/agent quick switcher'),
         StringStruct('FileVersion', '$Version'),
         StringStruct('InternalName', 'QuickSwitch'),
         StringStruct('LegalCopyright', 'Copyright (c) 2026 hyl320. MIT License.'),
         StringStruct('OriginalFilename', 'QuickSwitch.exe'),
         StringStruct('ProductName', 'QuickSwitch'),
         StringStruct('ProductVersion', '$Version')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@
Set-Content -Path $versionFile -Value $versionInfo -Encoding UTF8

Write-Host "Running PyInstaller..."
python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name QuickSwitch `
    --icon (Join-Path $Root "assets\quickswitch.ico") `
    --add-data ((Join-Path $Root "assets") + ";assets") `
    --hidden-import win32timezone `
    --exclude-module PIL `
    --exclude-module numpy `
    --exclude-module setuptools `
    --version-file $versionFile `
    --distpath $distDir `
    --workpath (Join-Path $buildDir "pyinstaller") `
    --specpath $buildDir `
    (Join-Path $Root "QuickSwitch.pyw")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$exe = Join-Path $distDir "QuickSwitch.exe"
if (-not (Test-Path $exe)) { throw "Build finished but $exe is missing." }

$hash = (Get-FileHash -Algorithm SHA256 $exe).Hash.ToLower()
$shaFile = Join-Path $distDir "QuickSwitch.exe.sha256"
Set-Content -Path $shaFile -Value ($hash + "  QuickSwitch.exe") -Encoding ASCII

Write-Host ""
Write-Host "Built:  $exe"
Write-Host "Size:   $([math]::Round((Get-Item $exe).Length / 1MB, 2)) MB"
Write-Host "SHA256: $hash"
