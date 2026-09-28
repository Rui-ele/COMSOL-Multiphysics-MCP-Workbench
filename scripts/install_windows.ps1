param(
    [string]$Python = "",
    [string]$Venv = ""
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Bundle = Join-Path $Repo "vendor\windows-cp314"
$Manifest = Get-Content -LiteralPath (Join-Path $Bundle "manifest.json") -Raw | ConvertFrom-Json
if (-not $Venv) { $Venv = Join-Path $Repo ".venv" }

# Restore a missing asset from its recorded official URL, without a pip network request.
function Ensure-BundleFile($Entry) {
    $Destination = Join-Path $Bundle $Entry.file
    if (-not (Test-Path -LiteralPath $Destination -PathType Leaf)) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
        Write-Host "Downloading $($Entry.file)"
        $Temporary = "$Destination.download"
        Invoke-WebRequest -UseBasicParsing -Uri $Entry.url -OutFile $Temporary
        if ((Get-FileHash -LiteralPath $Temporary -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Entry.sha256) {
            throw "Download checksum mismatch: $($Entry.file)"
        }
        Move-Item -LiteralPath $Temporary -Destination $Destination
    }
    if ((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Entry.sha256) {
        throw "Checksum mismatch: $($Entry.file). Restore the file from the repository."
    }
    return $Destination
}

function Test-BundlePython([string]$Candidate) {
    if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    try {
        & $Candidate -c "import platform, struct, sys, sysconfig, venv, ensurepip; sys.exit(not (sys.version_info[:2] == (3,14) and platform.python_implementation() == 'CPython' and platform.machine().lower() in ('amd64','x86_64') and struct.calcsize('P') == 8 and not sysconfig.get_config_var('Py_GIL_DISABLED')))" 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

if ($env:OS -ne "Windows_NT" -or -not [Environment]::Is64BitOperatingSystem) {
    throw "This bundle targets Windows x64. See README.md for other platforms."
}
if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64" -or $env:PROCESSOR_ARCHITEW6432 -eq "ARM64") {
    throw "This bundle targets x64 Windows. Windows ARM64 needs a matching package bundle."
}

$SelectedPython = ""
if ($Python) {
    if (-not (Test-BundlePython $Python)) { throw "-Python must point to standard CPython 3.14 x64 with venv and pip." }
    $SelectedPython = (Resolve-Path -LiteralPath $Python).Path
} else {
    $Candidates = @(
        (Join-Path $Venv "Scripts\python.exe"),
        (Join-Path $env:LOCALAPPDATA "COMSOL-MCP\Python314\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python314\python.exe")
    )
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try {
            $Found = & py -3.14 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0) { $Candidates += [string]$Found }
        } catch { }
    }
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($PythonCommand -and $PythonCommand.CommandType -eq "Application" -and $PythonCommand.Source -notlike "*WindowsApps*") {
        $Candidates += $PythonCommand.Source
    }
    foreach ($Candidate in $Candidates) {
        if (Test-BundlePython $Candidate) { $SelectedPython = $Candidate; break }
    }
}

if (-not $SelectedPython) {
    $Installer = Ensure-BundleFile $Manifest.python_installer
    $Target = Join-Path $env:LOCALAPPDATA "COMSOL-MCP\Python314"
    $InstallerArgs = "/quiet InstallAllUsers=0 TargetDir=`"$Target`" PrependPath=0 Include_pip=1 Include_launcher=0 InstallLauncherAllUsers=0 AssociateFiles=0 Shortcuts=0 Include_test=0 Include_doc=0 Include_tcltk=0"
    Write-Host "Installing bundled Python in $Target"
    $Process = Start-Process -FilePath $Installer -ArgumentList $InstallerArgs -Wait -PassThru
    if ($Process.ExitCode -notin @(0, 3010)) { throw "Python installer exited with code $($Process.ExitCode)." }
    $SelectedPython = Join-Path $Target "python.exe"
    if (-not (Test-BundlePython $SelectedPython)) { throw "Python installation needs attention. See docs/initialize-windows.md." }
}

foreach ($Package in $Manifest.packages) { Ensure-BundleFile $Package | Out-Null }
Write-Host "Using $SelectedPython"
& $SelectedPython (Join-Path $PSScriptRoot "bootstrap.py") --offline --venv $Venv
if ($LASTEXITCODE -ne 0) { throw "Initialization failed; keep the command output for diagnosis." }
