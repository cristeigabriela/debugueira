# Puts the Windows SDK's cdb.exe on PATH for later steps, installing the SDK debuggers if they're missing.
# windows-latest (Server 2025) ships them since early 2026 (actions/runner-images#13465); the install is
# the fallback the runner-images maintainers gave there.
$debuggers = "${env:ProgramFiles(x86)}\Windows Kits\10\Debuggers\x64"
if (-not (Test-Path "$debuggers\cdb.exe")) {
    $sdk = Get-ItemProperty "HKLM:\Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*" |
        Where-Object { $_.DisplayName -like "Windows Software Development Kit*" -and $_.BundleCachePath } |
        Sort-Object DisplayVersion -Descending | Select-Object -First 1
    if (-not $sdk) { throw "no Windows SDK to add the debuggers to" }
    Start-Process -Wait -FilePath $sdk.BundleCachePath -ArgumentList "/features OptionId.WindowsDesktopDebuggers /q /norestart"
}
if (-not (Test-Path "$debuggers\cdb.exe")) { throw "cdb.exe still missing at $debuggers" }
Add-Content -Path $env:GITHUB_PATH -Value $debuggers
Write-Host "cdb: $debuggers\cdb.exe $((Get-Item "$debuggers\cdb.exe").VersionInfo.FileVersion)"
