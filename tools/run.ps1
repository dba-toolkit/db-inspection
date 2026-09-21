<#
.SYNOPSIS
  Analyze a collection package and generate a Word report.
.EXAMPLE
  .\tools\run.ps1 -Package .\mysql_inspection_v1_xxx.tar.gz -Customer "Customer" -Target "Core DB"
#>
param(
    [Parameter(Mandatory = $true)][string]$Package,
    [string]$OutputDir = "",
    [string]$Customer = "",
    [string]$Target = "",
    [string]$Company = "",
    [string]$Author = "",
    [string]$Reviewer = "",
    [string]$Logo = "assets/logo.png",
    [ValidateSet("professional", "legacy")][string]$Layout = "professional"
)

$ErrorActionPreference = "Stop"

# 本脚本位于 tools/，项目根是其上一级。两个入口都按项目根定位，不再依赖调用者的 cwd。
$Root = Split-Path -Parent $PSScriptRoot

$python = "python"
$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path $venvPython) { $python = $venvPython }
elseif (Test-Path "D:\python\python.exe") { $python = "D:\python\python.exe" }

if (-not $OutputDir) {
    $name = [System.IO.Path]::GetFileNameWithoutExtension($Package)
    $OutputDir = Join-Path $Root "output\$name"
}

$AnalyzeEntry = Join-Path $Root "analyze.py"
$ReportEntry = Join-Path $Root "generate_report.py"

Write-Host "== analyze =="
& $python $AnalyzeEntry $Package -o $OutputDir
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "== generate report =="
$model = Join-Path $OutputDir "report_model.json"
$docx = Join-Path $OutputDir "report.docx"
$arguments = @($model, "--output", $docx, "--layout", $Layout, "--logo", $Logo)
if ($Author) { $arguments += @("--author", $Author) }
if ($Reviewer) { $arguments += @("--reviewer", $Reviewer) }
if ($Customer) { $arguments += @("--customer", $Customer) }
if ($Target) { $arguments += @("--target", $Target) }
if ($Company) { $arguments += @("--company", $Company) }

& $python $ReportEntry @arguments
exit $LASTEXITCODE
