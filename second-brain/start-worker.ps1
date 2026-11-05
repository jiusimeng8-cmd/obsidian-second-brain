$ErrorActionPreference = 'Stop'
$base = Split-Path -Parent $MyInvocation.MyCommand.Path

# Resolution order: SECOND_BRAIN_PYTHON env var, pythonw, python, py launcher.
$python = $env:SECOND_BRAIN_PYTHON
if (-not $python) {
  foreach ($name in @('pythonw', 'python')) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source; break }
  }
}
if (-not $python) {
  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) { $python = $py.Source }
}
if (-not $python) { throw 'Python 3 not found. Set SECOND_BRAIN_PYTHON to pythonw.exe/python.exe.' }

$argumentList = @('-X', 'utf8')
if ((Split-Path -Leaf $python) -eq 'py.exe') { $argumentList += '-3' }
$argumentList += ('"' + (Join-Path $base 'worker.py') + '"')

Start-Process -FilePath $python -ArgumentList $argumentList -WorkingDirectory $base -WindowStyle Hidden
