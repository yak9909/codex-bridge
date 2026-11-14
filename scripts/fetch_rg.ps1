# Download the official ripgrep Windows build into vendor/rg.exe.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$vendor = Join-Path $root "vendor"
New-Item -ItemType Directory -Force $vendor | Out-Null
if (Test-Path (Join-Path $vendor "rg.exe")) { Write-Host "vendor/rg.exe already exists"; exit 0 }

$rel = Invoke-RestMethod "https://api.github.com/repos/BurntSushi/ripgrep/releases/latest" -Headers @{ "User-Agent" = "codex-bridge-setup" }
$asset = $rel.assets | Where-Object { $_.name -like "*x86_64-pc-windows-msvc.zip" } | Select-Object -First 1
if (-not $asset) { throw "Windows build not found in release $($rel.tag_name)" }

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("rg-" + [guid]::NewGuid())
New-Item -ItemType Directory $tmp | Out-Null
try {
    $zip = Join-Path $tmp $asset.name
    Invoke-WebRequest $asset.browser_download_url -OutFile $zip -Headers @{ "User-Agent" = "codex-bridge-setup" }
    Expand-Archive $zip -DestinationPath $tmp
    $exe = Get-ChildItem $tmp -Recurse -Filter rg.exe | Select-Object -First 1
    Copy-Item $exe.FullName (Join-Path $vendor "rg.exe")
    Write-Host "installed ripgrep $($rel.tag_name) to vendor/rg.exe"
} finally {
    Remove-Item -Recurse -Force $tmp
}
