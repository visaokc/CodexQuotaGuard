param([string]$Go = 'go')
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$target = Join-Path $root 'vendor\tsnet'
New-Item -ItemType Directory -Force -Path $target | Out-Null
$env:CGO_ENABLED = '0'
Push-Location (Join-Path $root 'native\tsnet')
try {
    & $Go test ./...
    if ($LASTEXITCODE -ne 0) { throw 'tsnet tests failed' }
    & $Go build -trimpath -ldflags='-s -w' -o (Join-Path $target 'cqg-tsnet.exe') .
    if ($LASTEXITCODE -ne 0) { throw 'tsnet build failed' }
    $module = (& $Go list -m -json tailscale.com | ConvertFrom-Json)
    Copy-Item -LiteralPath (Join-Path $module.Dir 'LICENSE') -Destination (Join-Path $target 'LICENSE.txt') -Force
    $manifest = @{version=$module.Version; sha256=(Get-FileHash (Join-Path $target 'cqg-tsnet.exe') -Algorithm SHA256).Hash.ToLower()}
    [IO.File]::WriteAllText((Join-Path $target 'manifest.json'), ($manifest | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    # Ship the notices from every module actually linked into the helper.
    $dirs = & $Go list -deps -f '{{if .Module}}{{.Module.Dir}}{{end}}' . | Sort-Object -Unique
    $notices = [Collections.Generic.List[string]]::new()
    foreach ($file in (Get-ChildItem -LiteralPath (Join-Path $module.Dir 'licenses') -File -Recurse -ErrorAction SilentlyContinue)) {
        $notices.Add("`n===== Tailscale upstream bundled notices / $($file.Name) =====`n")
        $notices.Add([IO.File]::ReadAllText($file.FullName))
    }
    foreach ($dir in $dirs) {
        if (-not $dir) { continue }
        foreach ($file in (Get-ChildItem -LiteralPath $dir -File | Where-Object { $_.Name -match '^(LICENSE|COPYING|NOTICE)(\.|$)' })) {
            $notices.Add("`n===== $(Split-Path $dir -Leaf) / $($file.Name) =====`n")
            $notices.Add([IO.File]::ReadAllText($file.FullName))
        }
    }
    [IO.File]::WriteAllText((Join-Path $target 'THIRD_PARTY_LICENSES.txt'), ($notices -join "`n"), [Text.UTF8Encoding]::new($false))
} finally { Pop-Location }
