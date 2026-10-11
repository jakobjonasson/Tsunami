<#
.SYNOPSIS
  Sign files with signtool (SHA-256) and verify them.
.DESCRIPTION
  Certificate: -Thumbprint (certificate in the user or machine store), or a PFX file via
  -PfxPath with the password in the environment variable named by -PfxPasswordEnv
  (default TSUNAMI_PFX_PASSWORD). The password never appears on a command line of ours.
  With no certificate configured: exit 1, or exit 0 when -Optional is given (CI without a cert).
  Environment fallbacks: TSUNAMI_SIGN_THUMBPRINT, TSUNAMI_PFX_PATH.
#>
param(
    [Parameter(Mandatory, Position = 0)][string[]]$Files,
    [string]$Thumbprint = $env:TSUNAMI_SIGN_THUMBPRINT,
    [string]$PfxPath = $env:TSUNAMI_PFX_PATH,
    [string]$PfxPasswordEnv = 'TSUNAMI_PFX_PASSWORD',
    [string]$TimestampUrl = 'http://timestamp.digicert.com',
    [switch]$Optional
)

$ErrorActionPreference = 'Stop'
$Files = @($Files | ForEach-Object { $_ -split "," })  # -File invocations pass a,b as one string

if (-not $Thumbprint -and -not $PfxPath) {
    $msg = 'sign.ps1: no code-signing certificate configured (-Thumbprint or -PfxPath).'
    if ($Optional) { Write-Host "$msg Skipping signing."; exit 0 }
    Write-Error $msg
    exit 1
}

function Find-SignTool {
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
    if (Test-Path $kits) {
        $found = Get-ChildItem $kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\x64\' } |
            Sort-Object FullName -Descending | Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return $null
}

$signtool = Find-SignTool
if (-not $signtool) {
    Write-Error 'sign.ps1: signtool.exe not found (install the Windows SDK).'
    exit 1
}

foreach ($file in $Files) {
    if (-not (Test-Path $file)) { Write-Error "sign.ps1: file not found: $file"; exit 1 }
}

$certArgs = @()
if ($Thumbprint) {
    $certArgs = @('/sha1', $Thumbprint)
} else {
    $password = [Environment]::GetEnvironmentVariable($PfxPasswordEnv)
    if (-not $password) { Write-Error "sign.ps1: environment variable $PfxPasswordEnv is not set."; exit 1 }
    $certArgs = @('/f', $PfxPath, '/p', $password)
}

foreach ($file in $Files) {
    Write-Host "Signing $file"
    & $signtool sign /fd SHA256 /td SHA256 /tr $TimestampUrl @certArgs $file
    if ($LASTEXITCODE -ne 0) { Write-Error "signtool sign failed for $file"; exit 1 }
    & $signtool verify /pa $file
    if ($LASTEXITCODE -ne 0) { Write-Error "signtool verify failed for $file"; exit 1 }
}
Write-Host 'Signed and verified.'
exit 0
