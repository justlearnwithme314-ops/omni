param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$env:PYTHONPATH = "$scriptDir;$env:PYTHONPATH"
python -m src.cli $Args
