param(
    [string]$Root = $PSScriptRoot,
    [string]$DshCli,
    [string]$SeedDirectory
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path -LiteralPath $Root).Path
$source = if (Test-Path -LiteralPath (Join-Path $Root 'controller.mjs')) { $Root } else { Join-Path $Root 'dsh-plugin' }
if (-not (Test-Path -LiteralPath (Join-Path $source 'bin\windows-x64\BailianHUD.exe'))) { throw 'Build or extract the Windows plugin package first' }
if (-not $DshCli) {
    $command = Get-Command dsh.cmd -ErrorAction SilentlyContinue
    if ($command) { $DshCli = $command.Source }
    if (-not $DshCli) {
        foreach ($process in @(Get-Process -Name 'DeepSeek Harness' -ErrorAction SilentlyContinue)) {
            if (-not $process.Path) { continue }
            $candidate = Join-Path (Split-Path -Parent $process.Path) 'resources\runtime\cli\bin\dsh.cmd'
            if (Test-Path -LiteralPath $candidate) { $DshCli = $candidate; break }
        }
    }
    if (-not $DshCli) { throw 'Open DSH first, or pass -DshCli with the full dsh.cmd path.' }
}
$dshHome = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.dsh'
$destination = Join-Path $dshHome 'plugins\dsh-bailian-hud'
$dataDirectory = Join-Path $dshHome 'storages\bailian-hud'
$backup = Join-Path $Root ('_migration\plugin-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backup -Force | Out-Null

function Stop-Hud([string]$directory) {
    $recordPath = Join-Path $directory '.hud_instance.json'
    if (-not (Test-Path -LiteralPath $recordPath)) { return }
    $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
    if ($record.app -ne 'BailianHUD' -or $record.port -lt 1 -or $record.port -gt 65535) { throw 'Invalid HUD instance record' }
    $baseUrl = 'http://127.0.0.1:' + $record.port
    try { $ping = Invoke-RestMethod ($baseUrl + '/api/ping') -TimeoutSec 2 }
    catch { return }
    if ($ping.app -ne 'BailianHUD' -or $ping.instance_id -ne $record.instance_id) { throw 'HUD instance identity changed; installation stopped' }
    $prefs = Get-Content -LiteralPath (Join-Path $directory '.hud_prefs.json') -Raw | ConvertFrom-Json
    Invoke-RestMethod ($baseUrl + '/api/hud/quit') -Method Post -Headers @{'X-HUD-Token'=$prefs.api_token} -TimeoutSec 3 | Out-Null
    for ($i = 0; $i -lt 60; $i++) {
        if (-not (Test-Path -LiteralPath $recordPath)) { return }
        Start-Sleep -Milliseconds 100
    }
    throw 'HUD has not exited; installation stopped before replacing its files'
}

Stop-Hud $dataDirectory
if ($SeedDirectory) {
    $SeedDirectory = (Resolve-Path -LiteralPath $SeedDirectory).Path
    Stop-Hud $SeedDirectory
}
New-Item -ItemType Directory -Path $dataDirectory -Force | Out-Null
if ($SeedDirectory -and -not (Test-Path -LiteralPath (Join-Path $dataDirectory '.hud_state.json'))) {
    foreach ($name in @('.hud_prefs.json', '.hud_state.json', 'bailian_usage.jsonl')) {
        $path = Join-Path $SeedDirectory $name
        if (Test-Path -LiteralPath $path) { Copy-Item -LiteralPath $path -Destination (Join-Path $dataDirectory $name) }
    }
    $prefsPath = Join-Path $dataDirectory '.hud_prefs.json'
    if (Test-Path -LiteralPath $prefsPath) {
        $prefs = Get-Content -LiteralPath $prefsPath -Raw | ConvertFrom-Json
        $prefs.follow = 'dsh'
        [IO.File]::WriteAllText($prefsPath, ($prefs | ConvertTo-Json -Depth 10), [Text.UTF8Encoding]::new($false))
    }
}

if ([IO.Path]::GetFullPath($source) -ne [IO.Path]::GetFullPath($destination)) {
    $stage = Join-Path $dshHome ('plugins\.bailian-stage-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    # Stage only public files; replace the directory to avoid stale engines and local config.
    foreach ($name in @('package.json','index.js','controller.mjs','client.js','cordis.patch.yml','README.md','LICENSE','install.ps1','Install.cmd','bss-credentials.example.json')) {
        Copy-Item -LiteralPath (Join-Path $source $name) -Destination (Join-Path $stage $name)
    }
    foreach ($folder in @('bin','licenses','docs')) {
        Copy-Item -LiteralPath (Join-Path $source $folder) -Destination $stage -Recurse
    }
    if (Test-Path -LiteralPath $destination) {
        $resolved = (Resolve-Path -LiteralPath $destination).Path
        $expected = [IO.Path]::GetFullPath((Join-Path $dshHome 'plugins\dsh-bailian-hud'))
        if ($resolved -ne $expected) { throw 'Unexpected plugin installation path' }
        Move-Item -LiteralPath $resolved -Destination (Join-Path $backup 'previous-plugin')
    }
    Move-Item -LiteralPath $stage -Destination $destination
}
$profilePackage = Join-Path $dshHome 'profiles\desktop\package.json'
Copy-Item -LiteralPath $profilePackage -Destination (Join-Path $backup 'dsh-profile-package.json')
& $DshCli plugin --profile desktop add $destination --ignore-scripts --offline --config.strict-dep-builds=false
if ($LASTEXITCODE -ne 0) { throw "DSH dependency registration failed: $LASTEXITCODE" }
$before = [IO.File]::ReadAllText($profilePackage)
$profile = $before | ConvertFrom-Json
if ($profile.dsh.profile.bundles -notcontains 'dsh-bailian-hud') {
    $profile.dsh.profile.bundles = @($profile.dsh.profile.bundles) + 'dsh-bailian-hud'
    if ([IO.File]::ReadAllText($profilePackage) -ne $before) { throw 'DSH profile changed; please retry' }
    $temporary = $profilePackage + '.bailian.tmp'
    [IO.File]::WriteAllText($temporary, ($profile | ConvertTo-Json -Depth 30), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $profilePackage -Force
}

# Retire legacy shortcuts only during an explicitly requested data migration.
if ($SeedDirectory) {
foreach ($path in @((Join-Path $Root '百炼监控.lnk'), (Join-Path ([Environment]::GetFolderPath('Desktop')) '百炼监控.lnk'))) {
    if (Test-Path -LiteralPath $path) {
        $resolved = (Resolve-Path -LiteralPath $path).Path
        $allowed = @((Join-Path $Root '百炼监控.lnk'), (Join-Path ([Environment]::GetFolderPath('Desktop')) '百炼监控.lnk'))
        if ($resolved -notin $allowed) { throw 'Unexpected shortcut path' }
        Move-Item -LiteralPath $resolved -Destination (Join-Path $backup ([IO.Path]::GetFileName($path) + '-' + [guid]::NewGuid().ToString('N'))) 
    }
}
}
Write-Output "DSH plugin: $destination"
Write-Output "Plugin data: $dataDirectory"
Write-Output 'Refresh the DSH plugin page and enable dsh-bailian-hud if necessary.'
