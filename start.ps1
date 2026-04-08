$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

$url = "http://127.0.0.1:8000"
Start-Job -ScriptBlock {
    param($targetUrl)
    Start-Sleep -Seconds 2
    Start-Process $targetUrl
} -ArgumentList $url | Out-Null

& $python -m uvicorn server:app --reload --host 127.0.0.1 --port 8000
