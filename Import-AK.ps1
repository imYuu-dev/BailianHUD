param([switch]$Pause)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Net.Http
$handler = [Net.Http.HttpClientHandler]::new()
$handler.UseProxy = $false
$client = [Net.Http.HttpClient]::new($handler)
$client.Timeout = [TimeSpan]::FromSeconds(20)
$secretPointer = [IntPtr]::Zero
$exitCode = 0

function Invoke-Hud([int]$Port, [string]$Path, [string]$Token, $Body) {
    $method = if ($null -eq $Body) { [Net.Http.HttpMethod]::Get } else { [Net.Http.HttpMethod]::Post }
    $request = [Net.Http.HttpRequestMessage]::new($method, ('http://127.0.0.1:' + $Port + $Path))
    if ($Token) { $request.Headers.Add('X-HUD-Token', $Token) }
    if ($null -ne $Body) {
        $request.Content = [Net.Http.StringContent]::new(($Body | ConvertTo-Json -Compress), [Text.Encoding]::UTF8, 'application/json')
    }
    $response = $null
    $timeoutMs = if ($Path -eq '/api/ping') { 1000 } else { 20000 }
    $timeout = [Threading.CancellationTokenSource]::new($timeoutMs)
    try {
        $response = $client.SendAsync($request, $timeout.Token).GetAwaiter().GetResult()
        $value = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json
        if (-not $response.IsSuccessStatusCode) { throw $value.error }
        return $value
    }
    finally {
        if ($response) { $response.Dispose() }
        $timeout.Dispose()
        $request.Dispose()
    }
}

try {
    Write-Host '百炼 AK 账号'

    $dataDirectory = if ($env:BAILIAN_HUD_HOME) { $env:BAILIAN_HUD_HOME } else { Join-Path $env:LOCALAPPDATA 'BailianHUD' }
    $dataDirectory = [IO.Path]::GetFullPath($dataDirectory).TrimEnd('\')
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $dataId = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($dataDirectory.ToLowerInvariant()))).Replace('-', '').ToLowerInvariant().Substring(0, 24)
    }
    finally { $sha.Dispose() }
    $recordPath = Join-Path $dataDirectory '.hud_instance.json'
    $instance = $null
    $started = $false
    for ($attempt = 0; $attempt -lt 80; $attempt++) {
        try {
            $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
            if ($record.app -eq 'BailianHUD' -and $record.port -ge 1 -and $record.port -le 65535) {
                $ping = Invoke-Hud $record.port '/api/ping' '' $null
                if ($ping.app -eq 'BailianHUD' -and $ping.instance_id -eq $record.instance_id -and $ping.data_id -eq $dataId) {
                    $instance = $record
                    break
                }
            }
        }
        catch { }
        if (-not $started) {
            $engine = Join-Path $PSScriptRoot 'BailianHUD.exe'
            if (-not (Test-Path -LiteralPath $engine)) { throw '请完整解压 ZCode 程序包后，再运行 Import-AK.cmd' }
            Start-Process -FilePath $engine -ArgumentList '--background' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden | Out-Null
            $started = $true
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $instance) { throw '程序未能启动，请检查解压目录中的文件是否完整' }
    $prefs = Get-Content -LiteralPath (Join-Path $dataDirectory '.hud_prefs.json') -Raw | ConvertFrom-Json
    if (-not $prefs.api_token) { throw '程序尚未就绪，请稍后重试' }
    $result = Invoke-Hud $instance.port '/api/credentials' $prefs.api_token $null
    $accounts = @($result.accounts)
    Write-Host ('已导入 ' + $accounts.Count + ' 个账号')
    $choice = ''
    if ($accounts.Count -gt 0) {
        for ($index = 0; $index -lt $accounts.Count; $index++) {
            $account = $accounts[$index]
            $marker = if ($account.id -eq $result.activeAccountId) { '（当前）' } else { '' }
            Write-Host (($index + 1).ToString() + '. ' + $account.name + ' · ' + $account.maskedAkId + $marker)
        }
        $choice = (Read-Host '输入编号切换账号，按回车添加账号').Trim()
    }
    if ($choice) {
        $number = 0
        if (-not [int]::TryParse($choice, [ref]$number) -or $number -lt 1 -or $number -gt $accounts.Count) { throw '账号编号无效' }
        $result = Invoke-Hud $instance.port '/api/credentials' $prefs.api_token @{action='select'; accountId=$accounts[$number - 1].id}
    }
    else {
        $name = (Read-Host '账号名称（可选）').Trim()
        $akId = (Read-Host 'AccessKey ID').Trim()
        $secureSecret = Read-Host 'AccessKey Secret' -AsSecureString
        $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureSecret)
        $akSecret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer).Trim()
        if (-not $akId -or -not $akSecret) { throw '请填写 AccessKey ID 和 AccessKey Secret' }
        $result = Invoke-Hud $instance.port '/api/credentials' $prefs.api_token @{name=$name; aliyunAkId=$akId; aliyunAkSecret=$akSecret}
        $akSecret = $null
        Write-Host ('已导入 ' + $result.count + ' 个账号')
    }
    $selected = @($result.accounts | Where-Object {$_.id -eq $result.activeAccountId})[0]
    Write-Host ('当前显示 ' + $selected.name)
    for ($attempt = 0; $attempt -lt 32; $attempt++) {
        if ($result.sync.ok) { Write-Host '同步成功'; break }
        if ($result.sync.error) { Write-Host $result.sync.error; break }
        Start-Sleep -Milliseconds 500
        $result = Invoke-Hud $instance.port '/api/credentials' $prefs.api_token $null
    }
    if (-not $result.sync.ok -and -not $result.sync.error) {
        Write-Host '同步中，可在监控面板查看结果'
    }
}
catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    $exitCode = 1
}
finally {
    $akSecret = $null
    if ($secretPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer) }
    if ($secureSecret) { $secureSecret.Dispose() }
    $client.Dispose()
    $handler.Dispose()
    if ($Pause) { Read-Host '按回车关闭' | Out-Null }
}
exit $exitCode
