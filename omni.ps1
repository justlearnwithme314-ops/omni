param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
python "$scriptDir\omni_main.py" @Args
