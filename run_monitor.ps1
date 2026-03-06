# Run the Bull Put Spread monitor with .env loaded. Used by Task Scheduler.
# Edit the path below if your project is elsewhere.
$ProjectRoot = "C:\Users\twluf\BullPutSpreadAnalyzer\bull-put-spread-analyzer"
Set-Location $ProjectRoot

# Load .env into environment (Task Scheduler often doesn't inherit user env or use python-dotenv)
$envPath = Join-Path $ProjectRoot ".env"
if (Test-Path $envPath) {
    Get-Content $envPath | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#")) {
            $eq = $line.IndexOf("=")
            if ($eq -gt 0) {
                $key = $line.Substring(0, $eq).Trim()
                $val = $line.Substring($eq + 1).Trim()
                # Remove surrounding quotes if present
                if ($val.Length -ge 2 -and $val.StartsWith('"') -and $val.EndsWith('"')) { $val = $val.Substring(1, $val.Length - 2) }
                if ($val.Length -ge 2 -and $val.StartsWith("'") -and $val.EndsWith("'")) { $val = $val.Substring(1, $val.Length - 2) }
                [Environment]::SetEnvironmentVariable($key, $val, "Process")
                Set-Item -Path "Env:$key" -Value $val
            }
        }
    }
}

# Use py if available, else python
$py = $null
if (Get-Command py -ErrorAction SilentlyContinue) { $py = "py" }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $py = "python" }
else { Write-Error "Python not found. Install Python and ensure py or python is on PATH."; exit 1 }

& $py -m mcps.monitor
