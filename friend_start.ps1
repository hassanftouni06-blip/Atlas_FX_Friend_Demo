$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root

function Find-Python312 {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $version = & $launcher.Source -3.12 -c 'import sys; print(sys.version_info[:2] == (3, 12))' 2>$null
        if ($LASTEXITCODE -eq 0 -and $version -eq 'True') { return @($launcher.Source, '-3.12') }
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        $version = & $python.Source -c 'import sys; print(sys.version_info[:2] == (3, 12))' 2>$null
        if ($LASTEXITCODE -eq 0 -and $version -eq 'True') { return @($python.Source) }
    }
    throw 'Python 3.12 is required. Install it from https://www.python.org/downloads/ with the Python launcher enabled, then run Start Demo again.'
}

try {
    $venvPython = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        $python = Find-Python312
        Write-Host 'Preparing a private Python environment...'
        if ($python.Count -eq 2) { & $python[0] $python[1] -m venv (Join-Path $root '.venv') }
        else { & $python[0] -m venv (Join-Path $root '.venv') }
        if ($LASTEXITCODE -ne 0) { throw 'Could not prepare Python.' }
        & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $root 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check the internet connection and rerun.' }
    }

    $provider = (Get-Content -LiteralPath (Join-Path $root 'config.json') -Raw | ConvertFrom-Json).ai_provider
    if ($provider -eq 'openai') { $label = 'OpenAI'; $secretName = 'openai.dpapi'; $envName = 'OPENAI_API_KEY' }
    else { $label = 'Gemini'; $secretName = 'gemini.dpapi'; $envName = 'GEMINI_API_KEY' }
    $secretDir = Join-Path $root '.secrets'
    $secretFile = Join-Path $secretDir $secretName
    if (-not (Test-Path -LiteralPath $secretFile)) {
        New-Item -ItemType Directory -Path $secretDir -Force | Out-Null
        Write-Host "Paste your own $label API key. It will be encrypted for your Windows account."
        $secure = Read-Host "$label API key" -AsSecureString
        if ($secure.Length -lt 10) { throw 'The key appears empty or too short.' }
        $secure | ConvertFrom-SecureString | Set-Content -LiteralPath $secretFile -Encoding ASCII
    }
    # Set-Content appends a line ending; ConvertTo-SecureString rejects it.
    $encrypted = (Get-Content -LiteralPath $secretFile -Raw).Trim()
    $secure = ConvertTo-SecureString -String $encrypted
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { [Environment]::SetEnvironmentVariable($envName, [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer), 'Process') }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    if (-not [Environment]::GetEnvironmentVariable($envName, 'Process')) { throw "The saved $label key could not be read by this Windows account." }

    & $venvPython (Join-Path $root 'friend_preflight.py')
    if ($LASTEXITCODE -ne 0) { throw 'MT5 demo verification failed. No bot was started.' }

    $url = 'http://127.0.0.1:8781/'
    try { $existing = Invoke-RestMethod -Uri ($url + 'api/state') -TimeoutSec 2 }
    catch { $existing = $null }
    if ($existing) {
        Write-Host 'The control room is already running; asking it to shut down so the new settings and key load...'
        $page = (Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 5).Content
        $token = [regex]::Match($page, "const token='([^']+)'").Groups[1].Value
        try {
            Invoke-RestMethod -Method Post -Uri $url -ContentType 'application/json' -Headers @{ 'X-Atlas-Token' = $token } -Body '{"action":"shutdown"}' -TimeoutSec 10 | Out-Null
        } catch {
            if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 409) {
                throw 'A trade is still open. Wait until it closes, then run Start Demo again.'
            }
            throw 'The running control room is an older version without a shutdown button. Close its pythonw.exe processes in Task Manager (no trade open), then run Start Demo again.'
        }
        $stopped = $false
        for ($i = 0; $i -lt 100; $i++) {
            Start-Sleep -Seconds 1
            try { Invoke-RestMethod -Uri ($url + 'api/state') -TimeoutSec 2 | Out-Null } catch { $stopped = $true; break }
        }
        if (-not $stopped) { throw 'The old control room did not shut down in time.' }
        Write-Host 'Old control room stopped.'
    }
    $venvPythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
    Start-Process -FilePath $venvPythonw -ArgumentList 'controller.py' -WorkingDirectory $root -WindowStyle Hidden
    Start-Sleep -Seconds 2
    $ready = Invoke-RestMethod -Uri ($url + 'api/state') -TimeoutSec 5
    if (-not $ready.config -or $ready.config.execution_mode -ne 'DEMO_ONLY') { throw 'Controller did not pass the demo-only check.' }
    Start-Process $url
    Write-Host 'Control room opened. Press Start everything there to begin demo testing.'
} catch {
    Write-Host ('Setup stopped: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
} finally {
    Remove-Item Env:GEMINI_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
}
