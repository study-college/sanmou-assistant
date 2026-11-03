$ErrorActionPreference = 'Stop'
$releaseRoot = $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $releaseRoot 'release-manifest.json'))) {
    $releaseRoot = Split-Path -Parent $PSScriptRoot
}
$manifestPath = Join-Path $releaseRoot 'release-manifest.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$failed = @()
foreach ($entry in $manifest.files) {
    $path = [IO.Path]::GetFullPath((Join-Path $releaseRoot $entry.path))
    if (-not $path.StartsWith(([IO.Path]::GetFullPath($releaseRoot) + [IO.Path]::DirectorySeparatorChar), [StringComparison]::OrdinalIgnoreCase)) { throw 'Manifest path escaped the release directory' }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { $failed += $entry.path; continue }
    $file = Get-Item -LiteralPath $path
    if ($file.Length -ne $entry.bytes -or (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.sha256) { $failed += $entry.path }
}
if ($failed.Count) { Write-Output $failed; throw ('Integrity check failed: ' + $failed.Count + ' files') }
Write-Output ('OK: ' + $manifest.version + ', ' + $manifest.files.Count + ' files verified')
