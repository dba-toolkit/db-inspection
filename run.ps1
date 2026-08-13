<#
.SYNOPSIS
  Analyze a collection package and generate a Word report.
.EXAMPLE
  .\run.ps1 -Package .\mysql_inspection_v1_xxx.tar.gz -Customer "Customer" -Target "Core DB"
#>
param(
    [Parameter(Mandatory = $true)][string]$Package,
    [string]$OutputDir = "",
    [string]$Customer = "",
    [string]$Target = "",
    [string]$Company = "",
    [string]$Author = "",
    [string]$Reviewer = "",
    [string]$Logo = "logo.png",
    [ValidateSet("professional", "legacy")][string]$Layout = "professional"
)

$ErrorActionPreference = "Stop"

$python = "python"
if (Test-Path ".\.venv\Scripts\python.exe") { $python = ".\.venv\Scripts\python.exe" }
elseif (Test-Path "D:\python\python.exe") { $python = "D:\python\python.exe" }

if (-not $OutputDir) {
    $name = [System.IO.Path]::GetFileNameWithoutExtension($Package)
    $OutputDir = Join-Path "output" $name
}

Write-Host "== analyze =="
& $python analyze.py $Package -o $OutputDir
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

& $python generate_report.py @arguments
exit $LASTEXITCODE
