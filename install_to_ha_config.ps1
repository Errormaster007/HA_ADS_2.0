param(
    [Parameter(Mandatory = $true)]
    [string]$HaConfigPath
)

$ErrorActionPreference = "Stop"

$source = Join-Path $PSScriptRoot "custom_components\ads"
$targetRoot = Join-Path $HaConfigPath "custom_components"
$target = Join-Path $targetRoot "ads"

if (-not (Test-Path $source)) {
    throw "Quelle nicht gefunden: $source"
}

if (-not (Test-Path $HaConfigPath)) {
    throw "HA Config-Verzeichnis nicht gefunden: $HaConfigPath"
}

New-Item -ItemType Directory -Force -Path $targetRoot | Out-Null

if (Test-Path $target) {
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $backup = "${target}_backup_$timestamp"
    Move-Item -Path $target -Destination $backup -Force
    Write-Host "Vorhandene ADS-Integration gesichert nach: $backup"
}

Copy-Item -Path $source -Destination $target -Recurse -Force
Write-Host "ADS Custom Component installiert nach: $target"
Write-Host "Bitte Home Assistant neu starten."
