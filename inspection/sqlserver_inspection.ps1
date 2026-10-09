#requires -Version 5.1
<#
.SYNOPSIS
  SQL Server read-only inspection collector v1.3.
.DESCRIPTION
  Collects instance, database, backup, performance, security, SQL Agent and
  Always On metadata into a versioned JSON snapshot and ZIP package.
.PARAMETER Server
  SQL Server hostname or IP. Named instance: 'SERVER\INSTANCE'. Default prompt.
.PARAMETER Port
  TCP port. 0 = use default (1433) or instance name resolution.
.PARAMETER User
  SQL Login user. Blank = Windows Integrated Authentication.
.PARAMETER Password
  SQL Login password (SecureString). Prompt if omitted and -User provided.
.PARAMETER Help
  Show help text and exit.
.NOTES
  采集器契约
  ----------
  本脚本只做「采集事实 + 落 snapshot.json + 生成一份 Markdown + 打包」，不做判定、
  不评分、不改客户数据。snapshot.json 的唯一消费者是 analyze.py（plugins/sqlserver/**）；
  任何输出变化都必须同步 docs/sqlserver-collector-interface.md，并说明是否需要刷新基线。

  bit 列语义（v1.3.0 起）
  -----------------------
  Convert-DbValue 用 `-is [System.DBNull]` 判空。**collector_version < 1.3.0 的采集包里，
  所有"值为真"的 bit 列都是 null**（PS 的 -eq 会把 DBNull 转成 bool $true）。
  消费这些字段时不能把 null 当成 false —— 历史包里的 null 是"未知"，不是"否"。

  elapsed_ms 语义（v1.3.1 起）
  ---------------------------
  collection.module_status[].elapsed_ms 是**该模块自己的执行毫秒数**。
  v1.3.0 及更早：只有通用采集引擎与 OS 采集块写真实值，四个专用块
  （实例基础信息 / TempDB / 安全与权限 / 性能采样）与 Add-ModuleStatus 一律写 0 ——
  那个 0 是"没计时"，不是"耗时为 0"，会让人把最慢的块读成最快的块。
  v1.3.1 起专用块各自计时；**未计时的模块写 null**，下游不得把 null 折算成 0
  （与 bit 列同一条纪律：未采集 ≠ 0）。

  执行模式（新增 OS 层采集前必读）
  -------------------------------
  -Server 指向目标实例，但脚本不一定跑在目标机上。SQL 侧 DMV（sys.dm_os_*、
  sys.dm_hadr_*）永远是目标实例的事实来源；OS 侧计数器（Get-CimInstance /
  Get-Counter / 物理磁盘）永远属于「运行本脚本的那台机器」。混用会把 DBA 笔记本的
  CPU/内存写进客户报告。因此 OS 模块只在目标就是本机时采集，其余情况登记为
  skipped 并写明原因 —— 未采集不等于 0。

  语句归属数据库的口径（v1.3.2 起）
  ---------------------------------
  要给一条缓存语句标"属于哪个库"，**不能**用 sys.dm_exec_sql_text.dbid —— 它对
  ad-hoc（即席）计划恒为 NULL，只有存储过程/预编译批次才有值。用它会让四个 TOP SQL 的
  「数据库」列整列空、CPU按数据库分布整表 0 行（v1.3.1 及更早的实测缺陷）。
  正确口径是 sys.dm_exec_plan_attributes(plan_handle) 里 attribute=N'dbid' 的属性值，
  它对 ad-hoc 计划同样有效；无库上下文时该值为 32767（ResourceDB），须单独标名或排除。

  模块状态取值（collection.module_status[].status）
  ------------------------------------------------
  success          SQL 执行成功。row_count=0 只代表"结果为空"，不代表"无风险"
  failed           异常被捕获，原因写入 collection.limitations
  not_applicable   该实例/版本不具备此特性（例：未启用 HADR、VLF 需 2017+）
  skipped          有意不采集（例：远程目标下的 OS 计数器）
  未采集、无权限、不支持、空结果，一律不得折算成 0。

  退出码
  ----
  0 = 无 failed 模块；2 = 至少一个模块 failed（采集包照常产出，reason 见 limitations）。
.EXAMPLE
  .\sqlserver_inspection.ps1                              (interactive)
  .\sqlserver_inspection.ps1 -Server 10.0.0.10 -Port 1433
  .\sqlserver_inspection.ps1 -Server DB01\PROD -IncludeDeepChecks
  .\sqlserver_inspection.ps1 -Server 10.0.0.10 -User sa -Password (Read-Host -AsSecureString)
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$false)][string]$Server = '.',
    [int]$Port = 0,
    [string]$Database = 'master',
    [string]$User = '',
    [Security.SecureString]$Password,
    # 默认值必须与脚本自身位置无关（AGENTS.md：采集脚本不得依赖自身所在目录），
    # 否则脚本在仓库 inspection\ 下被直接执行时，采集包会落进仓库。
    [string]$OutputDir = '',
    [ValidateRange(1,120)][int]$SampleCount = 6,
    [ValidateRange(1,300)][int]$SampleIntervalSeconds = 5,
    [ValidateRange(0,3650)][int]$QueryStatsWindowDays = 7,
    [switch]$ExcludeSqlText,
    [switch]$IncludeDeepChecks,
    [switch]$NoPackage,
    [switch]$Help
)

if ($Help) {
    Write-Host @'
SQL Server 只读巡检采集器 v1.3.5
===================================
用法:
  .\sqlserver_inspection.ps1 [参数]

参数:
  -Server      SQL Server 地址 (默认: 交互输入)
  -Port        端口 (默认: 0 = 自动检测)
  -User        SQL 登录名 (默认: '' = Windows 集成认证)
  -Password    SQL 密码 (SecureString, 默认: 交互输入)
  -Database    初始数据库 (默认: master)
  -OutputDir   输出目录 (默认: %TEMP%\sqlserver_inspection
               该路径在 RDP/多会话机器上会带 \2\ \3\ 段，现场翻起来费劲；
               不想找就显式指定一个顺手的位置，见下方示例)
  -SampleCount 性能采样点数 (1-120, 默认: 6)
  -SampleIntervalSeconds 采样间隔秒 (1-300, 默认: 5)
  -QueryStatsWindowDays   慢SQL时间窗口天数 (0-3650, 默认: 7; 0=不限)
  -ExcludeSqlText         隐藏 SQL 文本
  -IncludeDeepChecks      启用深度检查 (统计信息/碎片/孤立用户)
  -NoPackage              只输出目录不打包 ZIP
  -Help                   显示此帮助

示例:
  .\sqlserver_inspection.ps1                              (交互式输入)
  .\sqlserver_inspection.ps1 -Server 10.0.0.10 -Port 1433
  .\sqlserver_inspection.ps1 -Server . -IncludeDeepChecks
  .\sqlserver_inspection.ps1 -Server 10.0.0.10 -User sa
  .\sqlserver_inspection.ps1 -Server . -OutputDir "$env:USERPROFILE\Desktop"
  ↑ 最后一条把采集包直接放到桌面（-OutputDir 可指向任意可写目录）
'@
    exit 0
}

# ⚠ 本脚本开着 Set-StrictMode -Version Latest。实测（PowerShell 5.1）在该模式下：
#     哈希表缺键 / 对象缺属性 / 对 $null 取属性  →  一律抛 PropertyNotFoundException
#     （FullyQualifiedErrorId = PropertyNotFoundStrict）。
#   而采集包里“模块没采到 / 空结果 / 该版本没这一项 / 某个性能计数器库不可用”都是常态，
#   所以**任何可能缺键的取属性都必须走 Get-ObjProp（写用 Set-ObjProp）**，不要写 $obj.key。
#   ⚠ 但**数组型字段（要对结果取 .Count 或按数组遍历）必须用 Get-ObjArray**：
#     Get-ObjProp 的 return 会走管道，把**单元素数组拆包成标量**；而 .Count 对 PSCustomObject
#     会抛 PropertyNotFoundStrict → 机器上"恰好只有 1 块盘 / 1 张网卡"就会把整篇报告打挂。
#   这条不是洁癖：2026-10-08 真机首跑就是因为对 $snapshot 里 instance 的 connection_ip 裸取点号属性，
#   在“实例查询失败（instance 为空对象）”时把整脚本打死、一个包都产不出来。
#   守门测试：tests/test_sqlserver_collector_strict_safety.py
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$collectorVersion = '1.3.5'

# 默认输出目录取系统临时目录下的固定子目录，与脚本所在位置无关（ISupports 在 ISE / dot-source 下同样成立）。
if (-not $OutputDir -or $OutputDir -eq '') {
    $OutputDir = Join-Path ([System.IO.Path]::GetTempPath()) 'sqlserver_inspection'
}
if (-not (Test-Path -LiteralPath $OutputDir)) {
    try { [void](New-Item -ItemType Directory -Path $OutputDir -Force) }
    catch { Write-Warning "OutputDir '$OutputDir' 不可用，使用当前目录"; $OutputDir = Get-Location }
}
$startedAt = Get-Date
$stamp = $startedAt.ToString('yyyyMMdd_HHmmss')

# 先用临时名创建目录，采集实例信息后重命名
$tempRoot = Join-Path $OutputDir "sqlserver_inspection_temp_$stamp"
$null = New-Item -ItemType Directory -Path $tempRoot -Force
$logPath = Join-Path $tempRoot 'collector.log'

function Write-CollectorLog {
    param([string]$Message, [string]$Level='INFO')
    $line = '[{0}][{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    Write-Host $line
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Convert-SecurePassword {
    param([Security.SecureString]$Value)
    if ($null -eq $Value) { return '' }
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function New-ConnectionString {
    $builder = New-Object System.Data.SqlClient.SqlConnectionStringBuilder
    $source = $Server
    if ($Port -gt 0 -and $Server -notmatch '\\') { $source = "${Server},${Port}" }
    $builder['Data Source'] = $source
    $builder['Initial Catalog'] = $Database
    $builder['Connect Timeout'] = 20
    $builder['Application Name'] = 'SQLServer-Inspection-Collector'
    $builder['Encrypt'] = $true
    $builder['TrustServerCertificate'] = $true
    if ([string]::IsNullOrWhiteSpace($User)) {
        $builder['Integrated Security'] = $true
    } else {
        $builder['User ID'] = $User
        $builder['Password'] = Convert-SecurePassword $Password
    }
    return $builder.ConnectionString
}

$script:ConnectionString = New-ConnectionString

function Convert-DbValue {
    param($Value)
    # ⚠ 这里**不能**写 `$Value -eq [DBNull]::Value`。
    # PowerShell 的 -eq 会先把右侧转成左侧的类型再比较，而 [DBNull]::Value 转成 bool 是 $true，
    # 于是 `$true -eq [DBNull]::Value` 结果为 True —— 后果是**所有值为真的 bit 列都被写成 null**。
    # 实测（PS 5.1，真实 DataTable 端到端）：bit=1 -> null，bit=0 -> $false。
    # 现场包里的证据：is_auto_create_stats_on / is_auto_update_stats_on 该列在 sys.databases 里
    # 是 bit NOT NULL，但整包 3/3 全是 null；is_db_chaining_on 2 行为 null、1 行为 false。
    # 后果不是"少一列"，是**静默的假阴性**：DB.CHAINING / DB.TRUSTWORTHY 这类判据在真实包上永不触发，
    # 报告里"是否启用"那一格显示 "-"（看起来像未采集，实际是"是"）。
    # 判类型用 -is，判值用 -eq，两者不要混。
    if ($null -eq $Value -or $Value -is [System.DBNull]) { return $null }
    if ($Value -is [DateTime]) { return $Value.ToString('o') }
    if ($Value -is [bool]) { return [bool]$Value }
    return $Value
}

function Convert-Table {
    param([System.Data.DataTable]$Table)
    $items = @()
    if ($null -eq $Table) { return $items }
    foreach ($row in $Table.Rows) {
        $item = [ordered]@{}
        foreach ($column in $Table.Columns) {
            $item[$column.ColumnName] = Convert-DbValue $row[$column.ColumnName]
        }
        $items += [pscustomobject]$item
    }
    return $items
}

function Get-ObjProp {
    # StrictMode 安全的属性读取。本脚本开了 Set-StrictMode -Version Latest，
    # 此时对【哈希表的键】或【PSCustomObject 的属性】做点取访问，只要键不存在就抛
    # PropertyNotFoundException（FullyQualifiedErrorId = PropertyNotFoundStrict）。
    # 而采集包里"某个模块没采到 / 空结果 / 该版本没有这一项"极其常见，
    # 所以**任何可能缺键的取属性都必须走这里**，不要写 $obj.key。
    # 用法：Get-ObjProp $obj 'key'  （第三个参数是缺键时的默认值，默认 $null）
    param($Object, [string]$Name, $Default = $null)
    if ($null -eq $Object) { return $Default }
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) { return $Object[$Name] }
        return $Default
    }
    $prop = $Object.PSObject.Properties[$Name]
    if ($null -eq $prop) { return $Default }
    return $prop.Value
}

function Set-ObjProp {
    # 与 Get-ObjProp 配套的写入：哈希表走键赋值，PSCustomObject 走 Add-Member -Force。
    # （PSCustomObject 不支持 $obj['k'] = v 这种下标赋值，所以不能只用一种写法。）
    param($Object, [string]$Name, $Value)
    if ($null -eq $Object) { return }
    if ($Object -is [System.Collections.IDictionary]) { $Object[$Name] = $Value; return }
    [void]($Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value -Force)
}

function Get-ObjArray {
    # 取"数组型"字段专用的读取器。恒返回数组，缺键 / $null 都返回空数组。
    #
    # 为什么不能直接用 Get-ObjProp：
    #   ① PowerShell 的 `return $value` 会走管道，**单元素数组会被拆包成标量**；
    #   ② 而 Set-StrictMode -Version Latest 下对 PSCustomObject 取 .Count 会抛
    #      PropertyNotFoundException（FullyQualifiedErrorId = PropertyNotFoundStrict）。
    #   两者相乘的后果不是"少一行"而是"整脚本崩"：机器上恰好只有 1 块盘 / 1 张网卡时，
    #   $lastDisks.Count 就会抛。所以凡是要对结果取 .Count / 按数组遍历的，一律走这里。
    #   （@() 会重新包一层，标量进来也变成单元素数组。）
    param($Object, [string]$Name)
    $v = Get-ObjProp $Object $Name
    if ($null -eq $v) { return , @() }
    return , @($v | Where-Object { $null -ne $_ })
}

function Invoke-InspectionQuery {
    param([Parameter(Mandatory=$true)][string]$Query, [int]$TimeoutSeconds=90)
    $connection = New-Object System.Data.SqlClient.SqlConnection($script:ConnectionString)
    try {
        $connection.Open()
        $command = $connection.CreateCommand()
        $command.CommandText = $Query
        $command.CommandTimeout = $TimeoutSeconds
        $adapter = New-Object System.Data.SqlClient.SqlDataAdapter($command)
        $table = New-Object System.Data.DataTable
        [void]$adapter.Fill($table)
        return @(Convert-Table $table)
    } finally {
        if ($connection.State -ne [System.Data.ConnectionState]::Closed) { $connection.Close() }
        $connection.Dispose()
    }
}

$moduleStatus = New-Object System.Collections.ArrayList
$limitations = New-Object System.Collections.ArrayList
$snapshot = [ordered]@{
    schema_version = '1.1'
    collector_version = $collectorVersion
    target = [ordered]@{ server=$Server; port=$Port; database=$Database; authentication=$(if ($User) {'SQL'} else {'Windows'}) }
    collection = [ordered]@{ started_at=$startedAt.ToString('o'); finished_at=$null; module_status=$moduleStatus; limitations=$limitations }
}

function Invoke-CollectionModule {
    param([string]$Name, [string]$TargetKey, [string]$Query, [int]$TimeoutSeconds=90)
    $begin = Get-Date
    Write-CollectorLog "Collecting: $Name"
    try {
        $snapshot[$TargetKey] = @(Invoke-InspectionQuery -Query $Query -TimeoutSeconds $TimeoutSeconds)
        $rowCount = $snapshot[$TargetKey].Count
        # 状态只描述"采集发生了什么"：success=有行，success_no_data=SQL 跑通但结果为空。
        # 两者都不代表"无风险" —— 风险判定在分析层，且遵循 AGENTS.md 的"未采集 ≠ 0"。
        $status = $(if ($rowCount -gt 0) { 'success' } else { 'success_no_data' })
        [void]$moduleStatus.Add([ordered]@{ name=$Name; status=$status; row_count=$rowCount; elapsed_ms=[int]((Get-Date)-$begin).TotalMilliseconds; error=$null })
    } catch {
        $snapshot[$TargetKey] = @()
        $message = $_.Exception.Message.Split("`n")[0]
        [void]$moduleStatus.Add([ordered]@{ name=$Name; status='failed'; row_count=0; elapsed_ms=[int]((Get-Date)-$begin).TotalMilliseconds; error=$message })
        [void]$limitations.Add("${Name}: ${message}")
        Write-CollectorLog "${Name} failed: $message" 'WARN'
    }
}

# 登记一个"非 SQL 执行类"的模块状态：OS 采集、版本不适用、有意跳过。
# 参数名用 $ErrorMessage，避免覆盖 PowerShell 自动变量 $Error（只读，赋值会抛错）。
function Add-ModuleStatus {
    param([string]$Name,[string]$Status,[int]$RowCount=0,[string]$ErrorMessage=$null,[string]$Reason=$null,[datetime]$Begin=$null)
    # 未计时的调用落 $null 而不是 0：「没计时」写成 0 毫秒，会让报告的「采集执行明细」
    # 把这一行读成"耗时为 0"，而它恰恰可能是最慢的块。要耗时就必须显式传 -Begin。
    $elapsed = if ($Begin) { [int]((Get-Date) - $Begin).TotalMilliseconds } else { $null }
    [void]$moduleStatus.Add([ordered]@{ name=$Name; status=$Status; row_count=$RowCount; elapsed_ms=$elapsed; error=$ErrorMessage })
    if ($Reason) { [void]$limitations.Add("${Name}: ${Reason}") }
    Write-CollectorLog ("Module: {0} -> {1}{2}" -f $Name, $Status, $(if ($Reason) { " ($Reason)" } else { '' }))
}

# ===== 阶段 ②：连接与认证（失败即 throw，不产出任何采集包） =====
Write-CollectorLog "Collector $collectorVersion started for $Server"
try {
    $test = New-Object System.Data.SqlClient.SqlConnection($script:ConnectionString)
    $test.Open(); $test.Close(); $test.Dispose()
} catch {
    Write-CollectorLog "Connection failed: $($_.Exception.Message)" 'ERROR'
    throw
}

# ===== 阶段 ③：实例基础信息 =====
$instanceSql = @"
SELECT
  CAST(SERVERPROPERTY('ServerName') AS nvarchar(256)) AS server_name,
  CAST(SERVERPROPERTY('MachineName') AS nvarchar(256)) AS machine_name,
  ISNULL(CAST(SERVERPROPERTY('InstanceName') AS nvarchar(256)), 'MSSQLSERVER') AS instance_name,
  CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)) AS product_version,
  CAST(SERVERPROPERTY('ProductLevel') AS nvarchar(128)) AS product_level,
  CAST(SERVERPROPERTY('ProductUpdateLevel') AS nvarchar(128)) AS product_update_level,
  CAST(SERVERPROPERTY('Edition') AS nvarchar(256)) AS edition,
  CAST(SERVERPROPERTY('EngineEdition') AS int) AS engine_edition,
  CAST(SERVERPROPERTY('Collation') AS nvarchar(256)) AS collation,
  CAST(SERVERPROPERTY('IsClustered') AS bit) AS is_clustered,
  CAST(SERVERPROPERTY('IsHadrEnabled') AS bit) AS is_hadr_enabled,
  CAST(SERVERPROPERTY('IsIntegratedSecurityOnly') AS bit) AS is_integrated_security_only,
  CAST(SERVERPROPERTY('InstanceDefaultDataPath') AS nvarchar(512)) AS instance_default_data_path,
  CAST(SERVERPROPERTY('InstanceDefaultLogPath') AS nvarchar(512)) AS instance_default_log_path,
  CAST(SERVERPROPERTY('InstanceDefaultBackupPath') AS nvarchar(512)) AS instance_default_backup_path,
  osi.cpu_count, osi.scheduler_count, osi.socket_count, osi.cores_per_socket,
  osi.numa_node_count, osi.physical_memory_kb / 1024 AS physical_memory_mb,
  osi.affinity_type_desc, osi.virtual_machine_type_desc,
  osi.sqlserver_start_time,
  CAST(@@VERSION AS nvarchar(4000)) AS version_banner,
  -- 操作系统版本：必须走 SQL 侧 DMV。用 Get-CimInstance 取到的是【执行机】的 OS，
  -- 远程连库时会把它写进报告（曾据此把 DBA 笔记本的 Windows 10 写进客户 2.1 节）。
  -- ⚠ 列名以官方文档为准：sys.dm_os_windows_info 自 2008 R2 SP1 起就只有
  -- windows_release / windows_service_pack_level / windows_sku / os_language_version 四列
  -- （https://learn.microsoft.com/sql/relational-databases/system-dynamic-management-views/sys-dm-os-windows-info-transact-sql）。
  -- v1.3.2 之前这里写的是 windows_version / language / os_language ——【三个列名都不存在】，
  -- 后果是整条实例查询报 "Invalid column name"（被下方 catch 吞掉）→ 包内 instance 为空
  -- → 脚本随后崩在连接 IP 那段。凡改这里，先核官方文档列名，别再凭记忆写。
  -- windows_release 只给版本号（如 '10.0'）；产品名（Windows Server 2019 Standard）需要
  -- build × sku 映射（属分析层 G-18），构建号 17763 可从 version_banner 取。
  CAST(w.windows_release AS nvarchar(256)) AS os_version,
  CAST(w.windows_service_pack_level AS nvarchar(256)) AS os_service_pack_level,
  CAST(w.windows_sku AS int) AS os_sku,
  (SELECT TOP (1) local_net_address FROM sys.dm_exec_connections WHERE session_id=@@SPID AND local_net_address IS NOT NULL) AS connection_ip,
  (SELECT TOP (1) local_tcp_port FROM sys.dm_exec_connections WHERE session_id=@@SPID AND local_tcp_port IS NOT NULL) AS connection_port
FROM sys.dm_os_sys_info AS osi
CROSS JOIN sys.dm_os_windows_info AS w;
"@
$instanceBegin = Get-Date
try {
    $rows = @(Invoke-InspectionQuery $instanceSql)
    $snapshot.instance = if ($rows.Count) { $rows[0] } else { [ordered]@{} }
    [void]$moduleStatus.Add([ordered]@{name='实例基础信息';status='success';row_count=$rows.Count;elapsed_ms=[int]((Get-Date)-$instanceBegin).TotalMilliseconds;error=$null})
} catch {
    $snapshot.instance = [ordered]@{}
    $instanceError = $_.Exception.Message.Split("`n")[0]
    [void]$moduleStatus.Add([ordered]@{name='实例基础信息';status='failed';row_count=0;elapsed_ms=[int]((Get-Date)-$instanceBegin).TotalMilliseconds;error=$instanceError})
    [void]$limitations.Add("实例基础信息: $instanceError")
    # ⚠ 这个 catch 原先【不写日志】：实例查询失败在控制台完全没有痕迹，
    # 只有包里的 limitations 才看得到，而脚本又会在下游崩掉、包根本产不出来 ——
    # 于是"哪条 SQL 错了"永远查不出来。失败必须留痕。
    Write-CollectorLog "实例基础信息 failed: $instanceError" 'ERROR'
}

# 本地连接时 sys.dm_exec_connections 的 local_net_address 可能为 NULL，补用 PowerShell 探测本机 IPv4
#
# ⚠ 从这一行起，凡是对 $snapshot.instance 取属性一律走 Get-ObjProp：
#   实例查询失败时 $snapshot.instance 是空 [ordered]@{}，而 Set-StrictMode -Version Latest 下
#   对不存在的键做点取会直接抛 PropertyNotFoundException（FullyQualifiedErrorId=PropertyNotFoundStrict），
#   把"某个模块没采到"升级成"整脚本崩溃、一个包都产不出来"。
$instanceObj = $snapshot.instance
if (-not (Get-ObjProp $instanceObj 'connection_ip')) {
    try {
        $localIps = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notmatch '^127\.' -and $_.IPAddress -notmatch '^169\.254\.' -and $_.IPAddress -ne '0.0.0.0' } |
            Sort-Object InterfaceMetric | Select-Object -ExpandProperty IPAddress)
        if ($localIps.Count -gt 0) { Set-ObjProp $instanceObj 'connection_ip' $localIps[0] }
    } catch { }
}
if (-not (Get-ObjProp $instanceObj 'connection_port')) {
    Set-ObjProp $instanceObj 'connection_port' $(if ($Port -gt 0) { $Port } else { 1433 })
}
$snapshot.instance = $instanceObj

# 用实际采集的实例名 + IP + 端口重命名输出目录
$realServer = Get-ObjProp $instanceObj 'server_name'
if ($realServer) {
    $safeTarget = ($realServer -replace '[^A-Za-z0-9_.-]', '_')
    $ip = Get-ObjProp $instanceObj 'connection_ip'
    $realPort = Get-ObjProp $instanceObj 'connection_port'
    if (-not $realPort -and $Port -gt 0) { $realPort = $Port }
    if ($ip) { $safeTarget = "${ip}_${safeTarget}" }
    if ($realPort -gt 0 -and $Server -notmatch '\\') { $safeTarget = "${safeTarget}_${realPort}" }
    $packageRoot = Join-Path $OutputDir "sqlserver_inspection_v1_${safeTarget}_${stamp}"
    if ($packageRoot -ne $tempRoot) {
        if (Test-Path -LiteralPath $packageRoot) {
            Remove-Item -LiteralPath $packageRoot -Recurse -Force
        }
        Rename-Item -LiteralPath $tempRoot -NewName (Split-Path $packageRoot -Leaf)
        $logPath = Join-Path $packageRoot 'collector.log'
        Write-CollectorLog "Output renamed to $safeTarget"
    }
} else {
    $safeTarget = ($Server -replace '[^A-Za-z0-9_.-]', '_')
    $packageRoot = $tempRoot
}

# ===== 阶段 ⑤：采集模块（逐项 Invoke-CollectionModule，单项失败不丢整包） =====
Invoke-CollectionModule '数据库清单与完整性' 'databases' @"
SELECT d.database_id, d.name, d.create_date, d.compatibility_level,
 d.collation_name, d.recovery_model_desc, d.state_desc, d.user_access_desc,
 d.page_verify_option_desc, d.log_reuse_wait_desc, d.is_read_only,
 d.is_auto_close_on, d.is_auto_shrink_on, d.is_auto_create_stats_on,
 d.is_auto_update_stats_on, d.is_auto_update_stats_async_on,
 CAST(NULL AS bit) is_query_store_on,d.containment_desc,d.is_trustworthy_on,d.is_db_chaining_on,d.is_encrypted,
 TRY_CONVERT(datetime, DATABASEPROPERTYEX(d.name, 'LastGoodCheckDbTime')) AS last_good_checkdb_time,
 CAST(SUM(CASE WHEN mf.type=0 THEN mf.size ELSE 0 END)*8.0/1024 AS decimal(18,2)) AS data_size_mb,
 CAST(SUM(CASE WHEN mf.type=1 THEN mf.size ELSE 0 END)*8.0/1024 AS decimal(18,2)) AS log_size_mb
FROM sys.databases d
LEFT JOIN sys.master_files mf ON d.database_id=mf.database_id
GROUP BY d.database_id,d.name,d.create_date,d.compatibility_level,d.collation_name,
 d.recovery_model_desc,d.state_desc,d.user_access_desc,d.page_verify_option_desc,
 d.log_reuse_wait_desc,d.is_read_only,d.is_auto_close_on,d.is_auto_shrink_on,
 d.is_auto_create_stats_on,d.is_auto_update_stats_on,d.is_auto_update_stats_async_on,
 d.containment_desc,d.is_trustworthy_on,d.is_db_chaining_on,d.is_encrypted
ORDER BY d.database_id;
"@

$productMajor = 0
try { $productMajor = [int]([string](Get-ObjProp $snapshot.instance 'product_version')).Split('.')[0] } catch { $productMajor = 0 }
if ($productMajor -ge 13) {
    try {
        $queryStoreRows = @(Invoke-InspectionQuery "SELECT name,is_query_store_on FROM sys.databases ORDER BY database_id;")
        foreach ($dbRow in $snapshot.databases) {
            $match = $queryStoreRows | Where-Object { $_.name -eq $dbRow.name } | Select-Object -First 1
            if ($null -ne $match) { $dbRow.is_query_store_on = $match.is_query_store_on }
        }
    } catch { [void]$limitations.Add("Query Store状态: $($_.Exception.Message)") }
} else {
    [void]$limitations.Add('当前 SQL Server 版本不支持 Query Store，未执行该项。')
}

Invoke-CollectionModule '备份状态' 'backups' @"
SELECT d.name AS database_name,d.recovery_model_desc,
 f.backup_finish_date last_full_backup,df.last_diff_backup,lg.last_log_backup,
 f.has_backup_checksums full_has_checksum,f.encryptor_type full_encryptor_type,
 CAST(f.backup_size/1048576.0 AS decimal(18,2)) full_backup_size_mb,
 CAST(f.compressed_backup_size/1048576.0 AS decimal(18,2)) full_compressed_size_mb
FROM sys.databases d
OUTER APPLY (SELECT TOP (1) backup_finish_date,has_backup_checksums,encryptor_type,backup_size,compressed_backup_size
 FROM msdb.dbo.backupset WITH (NOLOCK) WHERE database_name=d.name AND type='D' ORDER BY backup_finish_date DESC) f
OUTER APPLY (SELECT MAX(backup_finish_date) last_diff_backup FROM msdb.dbo.backupset WITH (NOLOCK) WHERE database_name=d.name AND type='I') df
OUTER APPLY (SELECT MAX(backup_finish_date) last_log_backup FROM msdb.dbo.backupset WITH (NOLOCK) WHERE database_name=d.name AND type='L') lg
WHERE d.name<>'tempdb' AND d.source_database_id IS NULL
ORDER BY d.name;
"@

Invoke-CollectionModule '备份历史（近7天）' 'backup_history' @"
-- 补三件旧版没有的东西：备份耗时（start->finish）、备份文件落点（backupmediafamily）、
-- LSN 链（判断日志备份链是否连续，缺链会导致恢复窗口断裂）。
-- 介质只取 physical_device_name：它供卷发现消费（解析盘符并入 volumes，对应备份盘盲区）；
-- 备份介质枚举是裸数字且下游无人读，按「采了没人读就移除」不采。
SET NOCOUNT ON;
SELECT TOP (300) bs.database_name,bs.type,
 bs.backup_start_date,bs.backup_finish_date,
 DATEDIFF(SECOND,bs.backup_start_date,bs.backup_finish_date) duration_seconds,
 CAST(bs.backup_size/1048576.0 AS decimal(18,2)) backup_size_mb,
 CAST(bs.compressed_backup_size/1048576.0 AS decimal(18,2)) compressed_size_mb,
 CASE WHEN bs.is_copy_only=1 THEN 'YES' ELSE 'NO' END is_copy_only,
 bs.has_backup_checksums,bs.encryptor_type,
 bs.is_snapshot,bs.database_backup_lsn,bs.first_lsn,bs.last_lsn,
 mf.physical_device_name
FROM msdb.dbo.backupset bs
OUTER APPLY (SELECT TOP (1) physical_device_name
             FROM msdb.dbo.backupmediafamily
             WHERE media_set_id=bs.media_set_id ORDER BY family_sequence_number) mf
WHERE bs.backup_finish_date>=DATEADD(DAY,-7,GETDATE()) AND bs.type<>'F'
ORDER BY bs.backup_finish_date DESC;
"@

Invoke-CollectionModule '无备份数据库' 'no_backup_databases' @"
SELECT d.name database_name,d.recovery_model_desc,d.create_date,
 DATEDIFF(DAY,d.create_date,GETDATE()) days_since_created
FROM sys.databases d
WHERE d.name NOT IN ('tempdb') AND d.source_database_id IS NULL AND d.state_desc='ONLINE'
 AND d.name NOT IN (SELECT DISTINCT database_name FROM msdb.dbo.backupset WHERE type='D')
ORDER BY d.name;
"@

# 还原验证（v1.3.2 新增）：backupset 只能证明"备份文件存在"，证明不了"备份真的还原过"。
# msdb.dbo.restorehistory 记录本实例的每次 RESTORE 操作，含 RESTORE VERIFYONLY（restore_type='V'）。
# 按目标库聚合出「最近一次真正还原」与「最近一次仅验证」—— 这是备份章节唯一的可恢复性证据。
# 未做过任何还原的库不会出现在这里，不等于"备份有问题"，报告侧按"未采集到验证记录"表述。
Invoke-CollectionModule '还原验证历史' 'restore_history' @"
SELECT rh.destination_database_name database_name,
 COUNT(*) restore_count,
 MAX(rh.restore_date) last_restore_date,
 MAX(CASE WHEN rh.restore_type IN ('D','I','L','F','G') THEN rh.restore_date END) last_applied_restore_date,
 MAX(CASE WHEN rh.restore_type='V' THEN rh.restore_date END) last_verify_only_date
FROM msdb.dbo.restorehistory rh
WHERE rh.destination_database_name IS NOT NULL
GROUP BY rh.destination_database_name
ORDER BY last_restore_date DESC;
"@

Invoke-CollectionModule '数据库RCSI状态' 'database_rcsi' @"
SELECT name database_name,
 is_read_committed_snapshot_on RCSI_enabled,
 snapshot_isolation_state_desc SI_state
FROM sys.databases WHERE database_id>4 ORDER BY name;
"@

Invoke-CollectionModule '数据库大小汇总' 'database_summary' @"
-- user_db_count 旧写法是 COUNT(*) OVER sys.master_files = 文件数（tempdb 8 个数据文件、
-- 任何多数据文件库都被重复计数）。库数只能数 sys.databases。
-- log_file_count 的口径：非系统库 + 排除 tempdb 的事务日志文件个数，对应报告 2.2「日志组数目」。
SELECT
 (SELECT COUNT(*) FROM sys.databases WHERE name NOT IN ('master','model','msdb','tempdb')) user_db_count,
 (SELECT COUNT(*) FROM sys.databases) database_count_all,
 (SELECT COUNT(*) FROM sys.master_files WHERE type=1 AND DB_NAME(database_id)<>'tempdb') log_file_count,
 CAST(SUM(CASE WHEN type=0 THEN size*8.0/1024 ELSE 0 END) AS decimal(18,2)) total_data_mb,
 CAST(SUM(CASE WHEN type=1 THEN size*8.0/1024 ELSE 0 END) AS decimal(18,2)) total_log_mb,
 CAST(SUM(size*8.0/1024) AS decimal(18,2)) total_size_mb
FROM sys.master_files WHERE DB_NAME(database_id) NOT IN ('master','model','msdb','tempdb');
"@

Invoke-CollectionModule '数据库文件与IO' 'database_files' @"
SELECT DB_NAME(mf.database_id) database_name,mf.name logical_name,mf.physical_name,
 mf.type_desc,CAST(mf.size*8.0/1024 AS decimal(18,2)) size_mb,
 mf.is_percent_growth,
 CASE WHEN mf.is_percent_growth=1 THEN CAST(mf.growth AS varchar(30))+'%'
      ELSE CAST(CAST(mf.growth*8.0/1024 AS decimal(18,2)) AS varchar(30))+' MB' END growth_value,
 CASE WHEN mf.max_size=-1 THEN NULL ELSE CAST(mf.max_size*8.0/1024 AS decimal(18,2)) END max_size_mb,
 vfs.num_of_reads,vfs.num_of_writes,
 CAST(vfs.io_stall_read_ms/NULLIF(vfs.num_of_reads,0.0) AS decimal(18,2)) avg_read_latency_ms,
 CAST(vfs.io_stall_write_ms/NULLIF(vfs.num_of_writes,0.0) AS decimal(18,2)) avg_write_latency_ms,
 CAST(vfs.size_on_disk_bytes/1048576.0 AS decimal(18,2)) size_on_disk_mb
FROM sys.master_files mf
LEFT JOIN sys.dm_io_virtual_file_stats(NULL,NULL) vfs
 ON mf.database_id=vfs.database_id AND mf.file_id=vfs.file_id
ORDER BY DB_NAME(mf.database_id),mf.file_id;
"@

Invoke-CollectionModule '存储卷容量' 'volumes' @"
-- 旧写法用 sys.master_files CROSS APPLY dm_os_volume_stats -> 只覆盖「有数据/日志文件」的卷，
-- 备份盘（例：E:\DBbackup）不会出现，报告 3.1「硬盘可用情况」因此看不到备份盘写满。
-- 三层合并：A 数据库文件所在卷（有总量/文件系统）；B xp_fixeddrives 兜底其余固定盘；
--           C 备份介质表里的盘符（标记哪些卷承载备份）。
SET NOCOUNT ON; SET XACT_ABORT OFF;
IF OBJECT_ID('tempdb..#vol') IS NOT NULL DROP TABLE #vol;
CREATE TABLE #vol(volume_mount_point nvarchar(512),logical_volume_name nvarchar(512),
 file_system_type nvarchar(64),total_mb decimal(18,2),available_mb decimal(18,2),
 free_pct decimal(10,2),source nvarchar(40));
INSERT #vol(volume_mount_point,logical_volume_name,file_system_type,total_mb,available_mb,free_pct,source)
SELECT vs.volume_mount_point,MIN(vs.logical_volume_name),MIN(vs.file_system_type),
 CAST(MAX(vs.total_bytes)/1048576.0 AS decimal(18,2)),
 CAST(MAX(vs.available_bytes)/1048576.0 AS decimal(18,2)),
 CAST(MAX(vs.available_bytes)*100.0/NULLIF(MAX(vs.total_bytes),0) AS decimal(10,2)),'DATABASE_FILE'
FROM sys.master_files mf CROSS APPLY sys.dm_os_volume_stats(mf.database_id,mf.file_id) vs
GROUP BY vs.volume_mount_point;
CREATE TABLE #fd(drive nvarchar(20),free_mb bigint);
BEGIN TRY
 EXEC(N'INSERT #fd EXEC master.dbo.xp_fixeddrives;');
 INSERT #vol(volume_mount_point,total_mb,available_mb,free_pct,source)
 SELECT CASE WHEN fd.drive LIKE '%:' THEN fd.drive ELSE fd.drive+':' END+N'\',
  NULL,CAST(fd.free_mb AS decimal(18,2)),NULL,'XP_FIXEDDRIVES'
 FROM #fd fd
 WHERE NOT EXISTS (SELECT 1 FROM #vol v
   WHERE v.volume_mount_point LIKE LEFT(CASE WHEN fd.drive LIKE '%:' THEN fd.drive ELSE fd.drive+':' END,2)+'%');
END TRY
BEGIN CATCH
 INSERT #vol(volume_mount_point,source) VALUES(N'(xp_fixeddrives 采集失败: '+LEFT(ERROR_MESSAGE(),200)+N')','ERROR');
END CATCH;
CREATE TABLE #bk(drive nvarchar(20));
BEGIN TRY
 EXEC(N'INSERT #bk SELECT DISTINCT LEFT(physical_device_name,PATINDEX(''%:%'',physical_device_name)) FROM msdb.dbo.backupmediafamily WHERE physical_device_name LIKE ''[A-Za-z]:%'';');
 INSERT #vol(volume_mount_point,logical_volume_name,source)
 SELECT b.drive+N'\',N'备份介质表','BACKUP_MEDIA' FROM #bk b
 WHERE b.drive IS NOT NULL AND b.drive<>''
  AND NOT EXISTS (SELECT 1 FROM #vol v WHERE v.volume_mount_point=b.drive+N'\');
END TRY
BEGIN CATCH
 INSERT #vol(volume_mount_point,source) VALUES(N'(备份介质表 采集失败: '+LEFT(ERROR_MESSAGE(),200)+N')','ERROR');
END CATCH;
SELECT volume_mount_point,logical_volume_name,file_system_type,total_mb,available_mb,free_pct,source
FROM #vol ORDER BY volume_mount_point;
"@

Invoke-CollectionModule '事务日志空间' 'log_space' @"
CREATE TABLE #logspace(database_name sysname,log_size_mb float,log_used_pct float,status int);
INSERT #logspace EXEC('DBCC SQLPERF(LOGSPACE) WITH NO_INFOMSGS');
SELECT database_name,CAST(log_size_mb AS decimal(18,2)) log_size_mb,
 CAST(log_used_pct AS decimal(10,2)) log_used_pct,status FROM #logspace ORDER BY log_used_pct DESC;
"@

# VLF 明细（sys.dm_db_log_info）需要 SQL Server 2017（14.x）及以上。
# 低版本登记 not_applicable 并写明原因，不再用一份 CAST(NULL) 占位列去伪造"列存在但无数据"。
if ($productMajor -gt 0 -and $productMajor -lt 14) {
    $snapshot.vlf_summary = @()
    Add-ModuleStatus -Name 'VLF摘要' -Status 'not_applicable' -Reason "sys.dm_db_log_info 需要 SQL Server 2017 及以上，本实例为 $productMajor.x"
} else {
    Invoke-CollectionModule 'VLF摘要' 'vlf_summary' @"
SET NOCOUNT ON; SET XACT_ABORT OFF;
IF OBJECT_ID('tempdb..#vlf') IS NOT NULL DROP TABLE #vlf;
CREATE TABLE #vlf(database_name sysname,vlf_count int,active_vlf_count int);
DECLARE @db sysname,@sql nvarchar(max);
DECLARE c CURSOR LOCAL FAST_FORWARD FOR SELECT name FROM sys.databases WHERE state=0 AND source_database_id IS NULL;
OPEN c; FETCH NEXT FROM c INTO @db;
WHILE @@FETCH_STATUS=0
BEGIN
 SET @sql=N'USE '+QUOTENAME(@db)+N'; INSERT #vlf SELECT DB_NAME(),COUNT(*),SUM(CASE WHEN vlf_active=1 THEN 1 ELSE 0 END) FROM sys.dm_db_log_info(DB_ID());';
 BEGIN TRY
  EXEC sys.sp_executesql @sql;
 END TRY
 BEGIN CATCH
  -- 单个库读不到就把该库登记为空值，不拖垮整个模块
  INSERT #vlf(database_name) VALUES(@db);
 END CATCH;
 FETCH NEXT FROM c INTO @db;
END
CLOSE c; DEALLOCATE c;
-- 列结构由 #vlf 定义，只有一个 SELECT：不再维护第二份 CAST(NULL) 占位列表（极易漏改）
SELECT database_name,vlf_count,active_vlf_count FROM #vlf ORDER BY vlf_count DESC;
"@
}

Invoke-CollectionModule '可疑页面' 'suspect_pages' @"
SELECT DB_NAME(sp.database_id) database_name,sp.database_id,sp.file_id,sp.page_id,
 sp.event_type,sp.error_count,sp.last_update_date
FROM msdb.dbo.suspect_pages sp
WHERE sp.event_type IN (1,2,3,4,5) ORDER BY sp.last_update_date DESC;
"@

Invoke-CollectionModule '等待统计' 'wait_stats' @"
SELECT TOP (30) wait_type,waiting_tasks_count,wait_time_ms,signal_wait_time_ms,
 CAST(wait_time_ms/NULLIF(waiting_tasks_count,0.0) AS decimal(18,2)) avg_wait_ms
FROM sys.dm_os_wait_stats WHERE wait_time_ms>0 ORDER BY wait_time_ms DESC;
"@

Invoke-CollectionModule '阻塞链' 'blocking' @"
SELECT r.session_id,r.blocking_session_id,r.wait_type,r.wait_resource,
 r.wait_time/1000.0 wait_seconds,DB_NAME(r.database_id) database_name,
 s.login_name,s.host_name,s.program_name,r.status,r.command,
 LEFT(REPLACE(REPLACE(t.text,CHAR(13),' '),CHAR(10),' '),2000) sql_text
FROM sys.dm_exec_requests r JOIN sys.dm_exec_sessions s ON r.session_id=s.session_id
OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t
WHERE r.blocking_session_id<>0 ORDER BY r.wait_time DESC;
"@

Invoke-CollectionModule '活动请求' 'active_requests' @"
SELECT TOP (50) r.session_id,DB_NAME(r.database_id) database_name,r.status,r.command,
 r.cpu_time,r.total_elapsed_time/1000.0 elapsed_seconds,r.reads,r.writes,r.logical_reads,
 r.wait_type,r.wait_time/1000.0 wait_seconds,r.blocking_session_id,
 s.login_name,s.host_name,s.program_name,
 LEFT(REPLACE(REPLACE(t.text,CHAR(13),' '),CHAR(10),' '),4000) sql_text
FROM sys.dm_exec_requests r JOIN sys.dm_exec_sessions s ON r.session_id=s.session_id
OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t
WHERE r.session_id<>@@SPID ORDER BY r.total_elapsed_time DESC;
"@

Invoke-CollectionModule '连接数统计' 'connection_stats' @"
SELECT ISNULL(ec.client_net_address,'Unknown') client_ip,
 ISNULL(es.host_name,'Unknown') host_name,
 ISNULL(es.program_name,'Unknown') program_name,
 ISNULL(es.login_name,'Unknown') login_name,
 COUNT(es.session_id) connection_count
FROM sys.dm_exec_sessions es
LEFT JOIN sys.dm_exec_connections ec ON es.session_id=ec.session_id
WHERE es.session_id>50 AND es.is_user_process=1
GROUP BY ec.client_net_address,es.host_name,es.program_name,es.login_name
ORDER BY connection_count DESC;
"@

# 会话资源占用：报告 1.9「活跃的用户进程及数量」问的是"谁在耗资源、什么状态"，
# 而 connection_stats 只回答"谁连着"——维度不同（status x login_name），不能互相替代。
# 现场报告里 1265 个会话绝大多数 sleeping，真正要看的是按账号累计的 CPU 与物理 IO；
# 累计值随会话存活时间增长，所以必须同时给出 earliest_login_time，才能区分
# "这个账号一直重" 和 "刚连上还没来得及重"。
Invoke-CollectionModule '会话资源占用' 'session_activity' @"
SELECT ISNULL(es.login_name,'Unknown') login_name,
 ISNULL(es.status,'unknown') status,
 COUNT(*) session_count,
 SUM(es.cpu_time) total_cpu_ms,
 SUM(es.reads) total_physical_reads,
 SUM(es.logical_reads) total_logical_reads,
 SUM(es.writes) total_writes,
 MIN(es.login_time) earliest_login_time,
 MAX(es.last_request_start_time) last_request_time
FROM sys.dm_exec_sessions es
WHERE es.is_user_process=1 AND es.session_id<>@@SPID
GROUP BY es.login_name,es.status
ORDER BY SUM(es.cpu_time) DESC;
"@

# 慢 SQL 观察窗口：默认 7 天（计划缓存自实例启动起累积，不设窗口会把久远历史算进总量）。
# 0 = 不限。列集合四个块统一：max/min 耗时、max 逻辑/物理读、spills、query_hash、
# query_plan_hash —— 报告 5.1 的「最大时间」「最小时间」两列旧版一个都取不到。
# 两个 hash 在 SQL 侧就转成十六进制：它们是 binary(8)，直出 JSON 会变成数字数组。
$queryStatsFilter = if ($QueryStatsWindowDays -gt 0) { " WHERE qs.last_execution_time >= DATEADD(DAY,-$QueryStatsWindowDays,GETDATE())" } else { "" }
# 四个 TOP SQL 的「数据库」列统一取 sys.dm_exec_plan_attributes 的 dbid（v1.3.2 修）：
# sys.dm_exec_sql_text.dbid 对 ad-hoc 计划恒为 NULL，此前用它导致数据库列 30/30 全空。
# 用 OUTER APPLY 而不是 CROSS APPLY —— 个别计划没有 dbid 属性时不能把整行丢掉；
# 32767 = ResourceDB（无库上下文），单独标名而不是留空。

Invoke-CollectionModule '高消耗SQL' 'top_queries' @"
SELECT TOP (30) CASE WHEN pa.dbid=32767 THEN 'ResourceDB' ELSE DB_NAME(pa.dbid) END database_name,qs.execution_count,
 qs.total_worker_time/1000.0 total_cpu_ms,qs.total_worker_time/NULLIF(qs.execution_count,0)/1000.0 avg_cpu_ms,
 qs.max_worker_time/1000.0 max_cpu_ms,
 qs.total_logical_reads,qs.total_logical_reads/NULLIF(qs.execution_count,0) avg_logical_reads,qs.max_logical_reads,
 qs.total_physical_reads,qs.max_physical_reads,
 qs.total_elapsed_time/1000.0 total_elapsed_ms,qs.max_elapsed_time/1000.0 max_elapsed_ms,
 qs.min_elapsed_time/1000.0 min_elapsed_ms,
 qs.total_elapsed_time/NULLIF(qs.execution_count,0)/1000.0 avg_elapsed_ms,
 qs.total_spills,CONVERT(varchar(18),qs.query_hash,1) query_hash,
 CONVERT(varchar(18),qs.query_plan_hash,1) query_plan_hash,qs.plan_generation_num,
 qs.creation_time,qs.last_execution_time,
 LEFT(REPLACE(REPLACE(st.text,CHAR(13),' '),CHAR(10),' '),4000) sql_text
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
OUTER APPLY (SELECT CONVERT(int,value) dbid FROM sys.dm_exec_plan_attributes(qs.plan_handle) WHERE attribute=N'dbid') pa$queryStatsFilter
ORDER BY qs.total_worker_time DESC;
"@

Invoke-CollectionModule '高逻辑读SQL' 'top_queries_logical_reads' @"
SELECT TOP (30) CASE WHEN pa.dbid=32767 THEN 'ResourceDB' ELSE DB_NAME(pa.dbid) END database_name,qs.execution_count,
 qs.total_worker_time/1000.0 total_cpu_ms,qs.total_worker_time/NULLIF(qs.execution_count,0)/1000.0 avg_cpu_ms,
 qs.max_worker_time/1000.0 max_cpu_ms,
 qs.total_logical_reads,qs.total_logical_reads/NULLIF(qs.execution_count,0) avg_logical_reads,qs.max_logical_reads,
 qs.total_physical_reads,qs.max_physical_reads,
 qs.total_elapsed_time/1000.0 total_elapsed_ms,qs.max_elapsed_time/1000.0 max_elapsed_ms,
 qs.min_elapsed_time/1000.0 min_elapsed_ms,
 qs.total_elapsed_time/NULLIF(qs.execution_count,0)/1000.0 avg_elapsed_ms,
 qs.total_spills,CONVERT(varchar(18),qs.query_hash,1) query_hash,
 CONVERT(varchar(18),qs.query_plan_hash,1) query_plan_hash,qs.plan_generation_num,
 qs.creation_time,qs.last_execution_time,
 LEFT(REPLACE(REPLACE(st.text,CHAR(13),' '),CHAR(10),' '),4000) sql_text
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
OUTER APPLY (SELECT CONVERT(int,value) dbid FROM sys.dm_exec_plan_attributes(qs.plan_handle) WHERE attribute=N'dbid') pa$queryStatsFilter
ORDER BY qs.total_logical_reads DESC;
"@

Invoke-CollectionModule '高执行次数SQL' 'top_queries_executions' @"
SELECT TOP (30) CASE WHEN pa.dbid=32767 THEN 'ResourceDB' ELSE DB_NAME(pa.dbid) END database_name,qs.execution_count,
 qs.total_worker_time/1000.0 total_cpu_ms,qs.total_worker_time/NULLIF(qs.execution_count,0)/1000.0 avg_cpu_ms,
 qs.max_worker_time/1000.0 max_cpu_ms,
 qs.total_logical_reads,qs.total_logical_reads/NULLIF(qs.execution_count,0) avg_logical_reads,qs.max_logical_reads,
 qs.total_physical_reads,qs.max_physical_reads,
 qs.total_elapsed_time/1000.0 total_elapsed_ms,qs.max_elapsed_time/1000.0 max_elapsed_ms,
 qs.min_elapsed_time/1000.0 min_elapsed_ms,
 qs.total_elapsed_time/NULLIF(qs.execution_count,0)/1000.0 avg_elapsed_ms,
 qs.total_spills,CONVERT(varchar(18),qs.query_hash,1) query_hash,
 CONVERT(varchar(18),qs.query_plan_hash,1) query_plan_hash,qs.plan_generation_num,
 qs.creation_time,qs.last_execution_time,
 LEFT(REPLACE(REPLACE(st.text,CHAR(13),' '),CHAR(10),' '),4000) sql_text
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
OUTER APPLY (SELECT CONVERT(int,value) dbid FROM sys.dm_exec_plan_attributes(qs.plan_handle) WHERE attribute=N'dbid') pa$queryStatsFilter
ORDER BY qs.execution_count DESC;
"@

Invoke-CollectionModule '高物理读SQL' 'top_queries_physical_reads' @"
SELECT TOP (30) CASE WHEN pa.dbid=32767 THEN 'ResourceDB' ELSE DB_NAME(pa.dbid) END database_name,qs.execution_count,
 qs.total_worker_time/1000.0 total_cpu_ms,qs.total_worker_time/NULLIF(qs.execution_count,0)/1000.0 avg_cpu_ms,
 qs.max_worker_time/1000.0 max_cpu_ms,
 qs.total_logical_reads,qs.total_logical_reads/NULLIF(qs.execution_count,0) avg_logical_reads,qs.max_logical_reads,
 qs.total_physical_reads,qs.max_physical_reads,
 qs.total_elapsed_time/1000.0 total_elapsed_ms,qs.max_elapsed_time/1000.0 max_elapsed_ms,
 qs.min_elapsed_time/1000.0 min_elapsed_ms,
 qs.total_elapsed_time/NULLIF(qs.execution_count,0)/1000.0 avg_elapsed_ms,
 qs.total_spills,CONVERT(varchar(18),qs.query_hash,1) query_hash,
 CONVERT(varchar(18),qs.query_plan_hash,1) query_plan_hash,qs.plan_generation_num,
 qs.creation_time,qs.last_execution_time,
 LEFT(REPLACE(REPLACE(st.text,CHAR(13),' '),CHAR(10),' '),4000) sql_text
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
OUTER APPLY (SELECT CONVERT(int,value) dbid FROM sys.dm_exec_plan_attributes(qs.plan_handle) WHERE attribute=N'dbid') pa$queryStatsFilter
ORDER BY qs.total_physical_reads DESC;
"@

Invoke-CollectionModule '缺失索引建议' 'missing_indexes' @"
SELECT TOP (30) DB_NAME(mid.database_id) database_name,OBJECT_SCHEMA_NAME(mid.object_id,mid.database_id) schema_name,
 OBJECT_NAME(mid.object_id,mid.database_id) table_name,migs.user_seeks,migs.user_scans,
 CAST(migs.avg_total_user_cost*migs.avg_user_impact*(migs.user_seeks+migs.user_scans) AS decimal(28,2)) improvement_score,
 mid.equality_columns,mid.inequality_columns,mid.included_columns
FROM sys.dm_db_missing_index_group_stats migs
JOIN sys.dm_db_missing_index_groups mig ON migs.group_handle=mig.index_group_handle
JOIN sys.dm_db_missing_index_details mid ON mig.index_handle=mid.index_handle
ORDER BY improvement_score DESC;
"@

Invoke-CollectionModule '未使用索引' 'unused_indexes' @"
SELECT TOP (30) DB_NAME(ius.database_id) database_name,OBJECT_SCHEMA_NAME(i.object_id,ius.database_id) schema_name,
 OBJECT_NAME(i.object_id,ius.database_id) table_name,i.name index_name,i.type_desc,
 ius.user_seeks,ius.user_scans,ius.user_lookups,ius.user_updates
FROM sys.dm_db_index_usage_stats ius
JOIN sys.indexes i ON ius.object_id=i.object_id AND ius.index_id=i.index_id
WHERE ius.database_id>4 AND i.index_id>1 AND i.is_primary_key=0 AND i.is_unique_constraint=0
 AND ius.user_seeks+ius.user_scans+ius.user_lookups=0 AND ius.user_updates>0
ORDER BY ius.user_updates DESC;
"@

Invoke-CollectionModule '实例配置' 'configurations' @"
SELECT name,value,value_in_use,minimum,maximum,is_dynamic,is_advanced
FROM sys.configurations
-- blocked process threshold (s)：报告 2.2/3.2 明确列为待优化启动参数（0 -> 30 秒），
-- 旧白名单漏了它，采集端整行不存在，分析层连"没配"都判不出来。
WHERE name IN ('max server memory (MB)','min server memory (MB)','max degree of parallelism',
 'cost threshold for parallelism','optimize for ad hoc workloads','backup compression default',
 'blocked process threshold (s)',
 'remote admin connections','xp_cmdshell','Ole Automation Procedures','contained database authentication',
 'clr enabled','Database Mail XPs','ad hoc distributed queries',
 'fill factor (%)','scan for startup procs','show advanced options')
ORDER BY name;
"@

# 服务与启动参数：报告 2.2「软件目录 …\MSSQL15.MSSQLSERVER\MSSQL\Binn\」只能从这里拿
# （dm_server_services.filename 即 sqlservr.exe 全路径）；startup_type_desc 顺带覆盖
# 「服务启动类型不是 AUTOMATIC → 重启后数据库起不来」这一常规检查项。
Invoke-CollectionModule 'SQL Server 服务与启动参数' 'server_services' @"
SELECT servicename,service_account,startup_type_desc,status_desc,process_id,
 last_startup_time,is_clustered,cluster_nodename,filename,
 instant_file_initialization_enabled
FROM sys.dm_server_services;
"@

Invoke-CollectionModule '缓冲池使用分布' 'buffer_pool' @"
SELECT CASE WHEN DB_NAME(database_id) IS NULL THEN 'ResourceDB' ELSE DB_NAME(database_id) END database_name,
 CAST(COUNT_BIG(*)*8.0/1024 AS decimal(18,2)) cache_size_mb,
 CAST(COUNT_BIG(*)*100.0/NULLIF(SUM(COUNT_BIG(*)) OVER(),0) AS decimal(10,2)) cache_pct
FROM sys.dm_os_buffer_descriptors WHERE database_id<>32767
GROUP BY DB_NAME(database_id) ORDER BY cache_size_mb DESC;
"@

Invoke-CollectionModule '进程内存状态' 'memory_status' @"
SELECT physical_memory_in_use_kb/1024 physical_memory_used_mb,
 locked_page_allocations_kb/1024 locked_page_mb,
 virtual_address_space_committed_kb/1024 vas_committed_mb,
 available_commit_limit_kb/1024 available_commit_limit_mb,
 memory_utilization_percentage,
 process_physical_memory_low,
 process_virtual_memory_low,
 CAST(total_virtual_address_space_kb/1048576.0 AS decimal(18,2)) total_vas_gb
FROM sys.dm_os_process_memory;
"@

# 内存组件明细：报告 4.5 那张 20 行 dm_os_memory_clerks 表在旧版没有任何来源。
# 64 位实例上不要再采 AWE 列（已废弃概念，旧报告里那一列恒 0）。
Invoke-CollectionModule '内存组件分布' 'memory_clerks' @"
SET NOCOUNT ON;
SELECT TOP (40) type,
 CAST(SUM(pages_kb)/1024.0 AS decimal(18,2)) pages_mb,
 CAST(SUM(virtual_memory_reserved_kb)/1024.0 AS decimal(18,2)) reserved_mb,
 CAST(SUM(virtual_memory_committed_kb)/1024.0 AS decimal(18,2)) committed_mb
FROM sys.dm_os_memory_clerks
GROUP BY type HAVING SUM(pages_kb)>0
ORDER BY SUM(pages_kb) DESC;
"@

# 系统内存状态：判断"实例吃了多少 vs 操作系统还剩多少"，替代只看进程内的 memory_status。
Invoke-CollectionModule '系统内存状态' 'system_memory' @"
SELECT
 CAST(total_physical_memory_kb/1024.0 AS decimal(18,2)) total_physical_memory_mb,
 CAST(available_physical_memory_kb/1024.0 AS decimal(18,2)) available_physical_memory_mb,
 CAST(total_page_file_kb/1024.0 AS decimal(18,2)) total_page_file_mb,
 CAST(available_page_file_kb/1024.0 AS decimal(18,2)) available_page_file_mb,
 CAST(system_cache_kb/1024.0 AS decimal(18,2)) system_cache_mb,
 CAST(kernel_paged_pool_kb/1024.0 AS decimal(18,2)) kernel_paged_pool_mb,
 CAST(kernel_nonpaged_pool_kb/1024.0 AS decimal(18,2)) kernel_nonpaged_pool_mb,
 system_memory_state_desc
FROM sys.dm_os_sys_memory;
"@

# CPU 按数据库分布（v1.3.2 修口径）：此前用 DB_NAME(st.dbid) 并自带 IS NOT NULL 过滤，
# 等于把所有 ad-hoc 计划的执行统计全部丢掉 —— 基线包里整表 0 行、报告 5.x 没有这张表。
# 改为 dm_exec_plan_attributes 取 dbid（对 ad-hoc 同样有效），并排除 32767=ResourceDB。
Invoke-CollectionModule 'CPU按数据库分布' 'cpu_by_database' @"
SELECT TOP (15) DB_NAME(pa.dbid) database_name,
 SUM(qs.total_worker_time)/1000 total_cpu_ms,
 SUM(qs.execution_count) execution_count,
 CAST(SUM(qs.total_worker_time)*1.0/NULLIF(SUM(qs.execution_count)*1000,0) AS decimal(18,2)) avg_cpu_ms,
 SUM(qs.total_logical_reads) total_logical_reads,
 SUM(qs.total_physical_reads) total_physical_reads,
 SUM(qs.total_elapsed_time)/1000 total_elapsed_ms
FROM sys.dm_exec_query_stats qs
CROSS APPLY (SELECT CONVERT(int,value) dbid FROM sys.dm_exec_plan_attributes(qs.plan_handle) WHERE attribute=N'dbid') pa
WHERE pa.dbid<>32767
GROUP BY pa.dbid ORDER BY total_cpu_ms DESC;
"@

$tempdbSql = @"
SELECT CAST(SUM(total_page_count)*8.0/1024 AS decimal(18,2)) total_mb,
 CAST(SUM(allocated_extent_page_count)*8.0/1024 AS decimal(18,2)) used_mb,
 CAST(SUM(unallocated_extent_page_count)*8.0/1024 AS decimal(18,2)) free_mb,
 CAST(SUM(allocated_extent_page_count)*100.0/NULLIF(SUM(total_page_count),0) AS decimal(10,2)) used_pct
FROM tempdb.sys.dm_db_file_space_usage;
"@
$tempdbFilesSql = @"
SELECT name logical_name,type_desc,CAST(size*8.0/1024 AS decimal(18,2)) size_mb,is_percent_growth,
 CASE WHEN is_percent_growth=1 THEN CAST(growth AS varchar(30))+'%'
 ELSE CAST(CAST(growth*8.0/1024 AS decimal(18,2)) AS varchar(30))+' MB' END growth_value,physical_name
FROM tempdb.sys.database_files ORDER BY file_id;
"@
$tempdbBegin = Get-Date
try {
    $t = @(Invoke-InspectionQuery $tempdbSql); $tf = @(Invoke-InspectionQuery $tempdbFilesSql)
    $snapshot.tempdb = if ($t.Count) { $t[0] } else { [ordered]@{} }
    $snapshot.tempdb | Add-Member -NotePropertyName files -NotePropertyValue $tf -Force
    [void]$moduleStatus.Add([ordered]@{name='TempDB';status='success';row_count=$tf.Count;elapsed_ms=[int]((Get-Date)-$tempdbBegin).TotalMilliseconds;error=$null})
} catch {
    $snapshot.tempdb=[ordered]@{files=@()}; [void]$limitations.Add("TempDB: $($_.Exception.Message)")
    [void]$moduleStatus.Add([ordered]@{name='TempDB';status='failed';row_count=0;elapsed_ms=[int]((Get-Date)-$tempdbBegin).TotalMilliseconds;error=$_.Exception.Message})
}

Invoke-CollectionModule -Name '无主键表' -TargetKey 'tables_without_pk' -TimeoutSeconds 300 -Query @"
-- 模块名去掉了「（深度）」后缀：它默认就采（只有 -IncludeDeepChecks 管的三项才叫深度），
-- 名字里的"深度"会让下游和运维误判成"默认没有这一项"。
SET NOCOUNT ON;
IF OBJECT_ID('tempdb..#nopk') IS NOT NULL DROP TABLE #nopk;
CREATE TABLE #nopk(database_name sysname,schema_name sysname,table_name sysname);
DECLARE @dbnpk sysname,@sqlnpk nvarchar(max);
DECLARE cnpk CURSOR LOCAL FAST_FORWARD FOR SELECT name FROM sys.databases WHERE state=0 AND name NOT IN ('master','model','msdb','tempdb');
OPEN cnpk; FETCH NEXT FROM cnpk INTO @dbnpk;
WHILE @@FETCH_STATUS=0
BEGIN
 SET @sqlnpk=N'USE '+QUOTENAME(@dbnpk)+N'; INSERT #nopk SELECT DB_NAME(),t.TABLE_SCHEMA,t.TABLE_NAME FROM INFORMATION_SCHEMA.TABLES t WHERE TABLE_TYPE=''BASE TABLE'' AND t.TABLE_SCHEMA=''dbo'' EXCEPT SELECT DB_NAME(),t.TABLE_SCHEMA,t.TABLE_NAME FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS c JOIN INFORMATION_SCHEMA.TABLES t ON c.TABLE_NAME=t.TABLE_NAME AND c.TABLE_SCHEMA=t.TABLE_SCHEMA AND c.TABLE_CATALOG=t.TABLE_CATALOG WHERE c.CONSTRAINT_TYPE=''PRIMARY KEY'' AND t.TABLE_SCHEMA=''dbo'';';
 EXEC sys.sp_executesql @sqlnpk; FETCH NEXT FROM cnpk INTO @dbnpk;
END
CLOSE cnpk; DEALLOCATE cnpk;
SELECT database_name,COUNT(*) table_count FROM #nopk GROUP BY database_name ORDER BY table_count DESC;
"@

Invoke-CollectionModule -Name '大表统计' -TargetKey 'large_tables' -TimeoutSeconds 300 -Query @"
-- 两处口径修正：
--   ① 排序/截断键。旧版逐库 TOP50 按空间取、全局 TOP30 却按 row_count 排序截断 ——
--      "空间大但行数少"的表会被直接挤出结果（实测某库 1927MB/12.5 万行的表排在第 7 位，
--      而 112MB/同 12.5 万行的表排在它前面）。现在内外都按空间降序。
--   ② 行数重复累加。旧版 JOIN indexes/partitions/allocation_units 后 GROUP BY p.rows，
--      非聚集索引每个分区都带同一份 p.rows，SUM 后行数被按索引数放大。
--      正确口径：行数只取堆/聚集索引（index_id IN (0,1)），空间才 SUM 全部索引。
SET NOCOUNT ON;
IF OBJECT_ID('tempdb..#lg') IS NOT NULL DROP TABLE #lg;
CREATE TABLE #lg(database_name sysname,schema_name sysname,table_name sysname,
 row_count bigint,total_mb decimal(18,2),used_mb decimal(18,2));
DECLARE @dblg sysname,@sqllg nvarchar(max);
DECLARE clg CURSOR LOCAL FAST_FORWARD FOR SELECT name FROM sys.databases WHERE state=0 AND name NOT IN ('master','model','msdb','tempdb');
OPEN clg; FETCH NEXT FROM clg INTO @dblg;
WHILE @@FETCH_STATUS=0
BEGIN
 SET @sqllg=N'USE '+QUOTENAME(@dblg)+N'; INSERT #lg SELECT TOP (50) DB_NAME(),SCHEMA_NAME(o.schema_id),o.name,SUM(CASE WHEN i.index_id IN (0,1) THEN p.rows ELSE 0 END),CAST(SUM(a.total_pages)*8.0/1024 AS decimal(18,2)),CAST(SUM(a.used_pages)*8.0/1024 AS decimal(18,2)) FROM sys.tables o JOIN sys.indexes i ON o.object_id=i.object_id JOIN sys.partitions p ON i.object_id=p.object_id AND i.index_id=p.index_id JOIN sys.allocation_units a ON p.partition_id=a.container_id GROUP BY o.schema_id,o.name ORDER BY SUM(a.total_pages) DESC;';
 EXEC sys.sp_executesql @sqllg; FETCH NEXT FROM clg INTO @dblg;
END
CLOSE clg; DEALLOCATE clg;
SELECT TOP (30) database_name,schema_name,table_name,row_count,total_mb,used_mb FROM #lg ORDER BY total_mb DESC;
"@

$sqlLoginsSql = "SELECT name,type_desc,is_disabled,create_date,modify_date,default_database_name,is_policy_checked,is_expiration_checked FROM sys.sql_logins WHERE name NOT LIKE '##%' ORDER BY name;"
$sysadminSql = @"
SELECT m.name,m.type_desc,CAST(CASE WHEN sl.is_disabled=1 THEN 1 ELSE 0 END AS bit) is_disabled
FROM sys.server_role_members rm JOIN sys.server_principals r ON rm.role_principal_id=r.principal_id
JOIN sys.server_principals m ON rm.member_principal_id=m.principal_id
LEFT JOIN sys.sql_logins sl ON m.principal_id=sl.principal_id WHERE r.name='sysadmin' ORDER BY m.name;
"@
$linkedServersSql = @"
SELECT name,product,provider,data_source,is_linked,is_data_access_enabled,
 is_rpc_out_enabled,is_remote_login_enabled,modify_date
FROM sys.servers WHERE is_linked=1 ORDER BY name;
"@
$securityChecksSql = @"
SELECT 'sa_account_disabled' check_name,
 CASE WHEN sl.is_disabled=1 THEN 'PASS' ELSE 'FAIL' END result,
 CASE WHEN sl.is_disabled=1 THEN 'sa 账号已禁用' ELSE 'sa 账号未禁用' END detail
FROM sys.sql_logins sl WHERE sl.name='sa'
UNION ALL
SELECT 'xp_cmdshell_enabled',
 CASE WHEN c.value_in_use=0 THEN 'PASS' ELSE 'FAIL' END,
 CASE WHEN c.value_in_use=0 THEN 'xp_cmdshell 已禁用' ELSE 'xp_cmdshell 已启用' END
FROM sys.configurations c WHERE c.name='xp_cmdshell'
UNION ALL
SELECT 'ole_automation_enabled',
 CASE WHEN c.value_in_use=0 THEN 'PASS' ELSE 'FAIL' END,
 CASE WHEN c.value_in_use=0 THEN 'Ole Automation 已禁用' ELSE 'Ole Automation 已启用' END
FROM sys.configurations c WHERE c.name='Ole Automation Procedures'
UNION ALL
SELECT 'empty_password_logins',
 CASE WHEN COUNT(*)=0 THEN 'PASS' ELSE 'FAIL' END,
 CASE WHEN COUNT(*)=0 THEN '无空密码登录' ELSE CAST(COUNT(*) AS varchar(10))+' 个空密码登录' END
FROM sys.sql_logins WHERE PWDCOMPARE('', password_hash)=1
UNION ALL
SELECT 'authentication_mode',
 CASE WHEN SERVERPROPERTY('IsIntegratedSecurityOnly')=1 THEN 'PASS' ELSE 'PASS' END,
 CASE WHEN SERVERPROPERTY('IsIntegratedSecurityOnly')=1 THEN '仅Windows认证' ELSE '混合模式认证' END;
"@
$securityBegin = Get-Date
try {
    $snapshot.security=[ordered]@{
        sql_logins=@(Invoke-InspectionQuery $sqlLoginsSql)
        sysadmin_members=@(Invoke-InspectionQuery $sysadminSql)
        linked_servers=@(Invoke-InspectionQuery $linkedServersSql)
        security_checks=@(Invoke-InspectionQuery $securityChecksSql)
    }
    [void]$moduleStatus.Add([ordered]@{name='安全与权限';status='success';row_count=@(Get-ObjProp $snapshot.security 'sql_logins' @()).Count;elapsed_ms=[int]((Get-Date)-$securityBegin).TotalMilliseconds;error=$null})
} catch {
    $snapshot.security=[ordered]@{sql_logins=@();sysadmin_members=@();linked_servers=@();security_checks=@()}; [void]$limitations.Add("安全与权限: $($_.Exception.Message)")
    [void]$moduleStatus.Add([ordered]@{name='安全与权限';status='failed';row_count=0;elapsed_ms=[int]((Get-Date)-$securityBegin).TotalMilliseconds;error=$_.Exception.Message})
}

Invoke-CollectionModule 'SQL Agent失败作业' 'failed_jobs' @"
SELECT j.name job_name,msdb.dbo.agent_datetime(h.run_date,h.run_time) run_datetime,h.run_duration,h.message
FROM msdb.dbo.sysjobhistory h JOIN msdb.dbo.sysjobs j ON h.job_id=j.job_id
WHERE h.step_id=0 AND h.run_status=0 AND msdb.dbo.agent_datetime(h.run_date,h.run_time)>=DATEADD(HOUR,-24,GETDATE())
ORDER BY run_datetime DESC;
"@

Invoke-CollectionModule 'SQL Agent作业清单' 'agent_jobs' @"
SELECT j.name job_name,j.enabled,SUSER_SNAME(j.owner_sid) owner_name,j.date_modified,
 CASE h.run_status WHEN 0 THEN 'FAILED' WHEN 1 THEN 'SUCCEEDED' WHEN 2 THEN 'RETRY' WHEN 3 THEN 'CANCELED' WHEN 4 THEN 'IN_PROGRESS' ELSE 'NEVER' END last_run_status_desc,
 CASE WHEN h.run_date>0 THEN msdb.dbo.agent_datetime(h.run_date,h.run_time) END last_run_datetime,
 CASE WHEN js.next_run_date>0 THEN msdb.dbo.agent_datetime(js.next_run_date,js.next_run_time) END next_run_datetime,
 h.run_duration last_run_duration,h.message last_message
FROM msdb.dbo.sysjobs j
LEFT JOIN msdb.dbo.sysjobschedules js ON j.job_id=js.job_id
OUTER APPLY (SELECT TOP (1) run_status,run_date,run_time,run_duration,message
 FROM msdb.dbo.sysjobhistory x WHERE x.job_id=j.job_id AND x.step_id=0 ORDER BY instance_id DESC) h
ORDER BY j.name;
"@

# 作业步骤明细（v1.3.2 新增）：agent_jobs 只有作业名 + 整体历史结果，"失败在哪一步"答不了。
# last_run_outcome: 0=失败 1=成功 2=重试 3=取消 5=未知 —— 排序把"最近失败的那一步"置顶。
# command 只取前 400 字符（步骤命令常含明文口令/连接串），并纳入 -ExcludeSqlText 的抹除范围。
Invoke-CollectionModule 'SQL Agent作业步骤' 'job_steps' @"
SELECT TOP (300) j.name job_name,s.step_id,s.step_name,s.subsystem,s.database_name,
 s.on_success_action,s.on_fail_action,s.retry_attempts,s.retry_interval,
 s.last_run_date,s.last_run_time,s.last_run_duration,s.last_run_retries,
 CASE s.last_run_outcome WHEN 0 THEN 'FAILED' WHEN 1 THEN 'SUCCEEDED' WHEN 2 THEN 'RETRY'
  WHEN 3 THEN 'CANCELED' WHEN 5 THEN 'UNKNOWN' ELSE 'NEVER' END last_run_outcome_desc,
 LEFT(REPLACE(REPLACE(s.command,CHAR(13),' '),CHAR(10),' '),400) command_preview
FROM msdb.dbo.sysjobsteps s JOIN msdb.dbo.sysjobs j ON s.job_id=j.job_id
ORDER BY CASE WHEN s.last_run_outcome=0 THEN 0 ELSE 1 END,j.name,s.step_id;
"@

# 高可用总览：先给一行"到底有没有启用 AG / 有几个 AG"，避免"0 行 = 未启用"与
# "0 行 = 采集写错"无法区分（旧写法 AG 未启用时返回的是一个 CAST(NULL) 占位结果集）。
Invoke-CollectionModule '高可用状态' 'ha_status' @"
SELECT CAST(SERVERPROPERTY('IsHadrEnabled') AS int) hadr_enabled,
 CAST(SERVERPROPERTY('IsClustered') AS int) is_clustered,
 (SELECT COUNT(*) FROM sys.availability_groups) availability_group_count,
 (SELECT COUNT(*) FROM sys.availability_replicas) availability_replica_count,
 (SELECT COUNT(*) FROM sys.availability_databases_cluster) ag_database_count;
"@

Invoke-CollectionModule 'Always On' 'availability_replicas' @"
-- 旧版缺三样报告 4.1 直接要用的东西：
--   ① drs.last_redone_time（报告有「最后重做时间」这一列，旧脚本给不出）
--   ② ar.availability_mode_desc —— 没有它无法判断 SYNCHRONIZING 是否正常：
--      异步提交下 SYNCHRONIZING 是常态，同步提交下才是真延迟（报告 C-2）
--   ③ 副本级连接错误（last_connect_error_*），AG 断连排查的第一手证据
-- 另外补 drs.secondary_lag_seconds（引擎自己算的"落后多少秒"），替代
-- DATEDIFF(last_redone_time,last_commit_time) 这种主副本两列全 NULL、辅助副本恒 0 的假延迟。
-- WHERE 用 HADR 开关代替旧版的 IF/ELSE + 一整份 CAST(NULL) 占位列，列形状只维护一处。
SELECT ag.name availability_group,ar.replica_server_name,DB_NAME(drs.database_id) database_name,
 ar.availability_mode_desc,ar.failover_mode_desc,ar.seeding_mode_desc,ar.backup_priority,
 ars.role_desc,ars.operational_state_desc,ars.connected_state_desc,
 ars.recovery_health_desc,ars.synchronization_health_desc AS replica_sync_health_desc,
 ars.last_connect_error_number,ars.last_connect_error_description,ars.last_connect_error_timestamp,
 drs.synchronization_state_desc,drs.synchronization_health_desc,
 drs.is_local,drs.is_suspended,drs.suspend_reason_desc,
 CAST(drs.log_send_queue_size/1024.0 AS decimal(18,2)) log_send_queue_mb,
 CAST(drs.redo_queue_size/1024.0 AS decimal(18,2)) redo_queue_mb,
 drs.log_send_rate,drs.redo_rate,drs.secondary_lag_seconds,
 drs.last_sent_time,drs.last_received_time,drs.last_hardened_time,drs.last_redone_time,drs.last_commit_time,
 DATEDIFF(SECOND,drs.last_commit_time,
   MAX(drs.last_commit_time) OVER (PARTITION BY drs.group_id,drs.database_id)) AS replica_lag_seconds
FROM sys.dm_hadr_database_replica_states drs
JOIN sys.availability_replicas ar ON drs.replica_id=ar.replica_id
JOIN sys.availability_groups ag ON ar.group_id=ag.group_id
LEFT JOIN sys.dm_hadr_availability_replica_states ars
  ON ar.replica_id=ars.replica_id AND drs.group_id=ars.group_id
WHERE CAST(SERVERPROPERTY('IsHadrEnabled') AS int)=1;
"@

Invoke-CollectionModule '数据库镜像' 'database_mirroring' @"
SELECT DB_NAME(database_id) database_name,mirroring_role_desc,mirroring_state_desc,
 mirroring_safety_level_desc,mirroring_partner_name,mirroring_witness_name,mirroring_witness_state_desc
FROM sys.database_mirroring WHERE mirroring_guid IS NOT NULL ORDER BY database_name;
"@

Invoke-CollectionModule '日志传送' 'log_shipping' @"
SELECT 'PRIMARY' monitor_role,primary_database database_name,
 DATEDIFF(MINUTE,last_backup_date,GETDATE()) minutes_since_last_action,
 CASE WHEN last_backup_date IS NULL THEN 'UNKNOWN' ELSE 'MONITORED' END status,last_backup_date last_action_time
FROM msdb.dbo.log_shipping_monitor_primary
UNION ALL
SELECT 'SECONDARY',secondary_database,DATEDIFF(MINUTE,last_restored_date,GETDATE()),
 CASE WHEN last_restored_date IS NULL THEN 'UNKNOWN' ELSE 'MONITORED' END,last_restored_date
FROM msdb.dbo.log_shipping_monitor_secondary;
"@

Invoke-CollectionModule '复制拓扑' 'replication' @"
-- 旧版这一项在任何实例上都返回 0 行且状态记为 success，两个原因：
--   ① 判断条件写错库：MSpublications / MSarticles / MSsubscriptions / MSrepl_errors 都建在
--      【分发库】（默认 distribution），msdb 里没有这些表 ->
--      IF OBJECT_ID('msdb.dbo.MSpublications') IS NOT NULL 恒为 NULL -> 永远走 ELSE 空分支。
--   ② 一个批处理里写了两条 SELECT（发布 + 订阅），而采集端 SqlDataAdapter.Fill 只取第一个
--      结果集，订阅那条无论对错都会被丢弃。
-- 现在：按复制角色探测分发库/发布库；所有记录压成单一结果集，用 record_type 区分种类。
-- 每块都包在 TRY/CATCH 里 —— 列名一旦与真实分发库不符，会落一条 (采集失败) 记录并带上
-- 原始 ERROR_MESSAGE，而不是静默变空（这几块需在真实分发库上实测校正，见接口文档 §9）。
SET NOCOUNT ON; SET XACT_ABORT OFF;
IF OBJECT_ID('tempdb..#repl') IS NOT NULL DROP TABLE #repl;
CREATE TABLE #repl(record_type nvarchar(40),name nvarchar(512),source_db sysname,
 target_server nvarchar(512),target_db sysname,status_desc nvarchar(200),
 last_action_time datetime,error_id int,error_count int,
 error_message nvarchar(2000),detail nvarchar(2000));

-- A) 复制角色：只读 sys.databases 的复制标志位，任何实例上都能安全执行。
--    0 行 = 确实没有配置复制（旧版本的 0 行无法这么解释）。
INSERT #repl(record_type,name,source_db,status_desc,detail)
SELECT 'REPL_ROLE',d.name,d.name,
 LTRIM(RTRIM(CASE WHEN d.is_distributor=1 THEN 'DISTRIBUTOR ' ELSE '' END
  +CASE WHEN d.is_published=1 THEN 'PUBLISHER ' ELSE '' END
  +CASE WHEN d.is_subscribed=1 THEN 'SUBSCRIBER ' ELSE '' END
  +CASE WHEN d.is_merge_published=1 THEN 'MERGE_PUBLISHER' ELSE '' END)),
 'recovery_model='+d.recovery_model_desc
FROM sys.databases d
WHERE d.name='distribution' OR d.is_distributor=1 OR d.is_published=1
   OR d.is_subscribed=1 OR d.is_merge_published=1;

-- ⚠ 表变量必须单独写一条 DECLARE：T-SQL 不允许把普通变量和表变量合并声明。
--   DECLARE @a sysname, @b table(...) 会报「关键字 'table' 附近有语法错误」——
--   真机首跑即此错（v1.3.2 引入，v1.3.4 修）。见 interface 文档 §9-⑱。
DECLARE @dist sysname;
DECLARE @blocks table(record_type nvarchar(40), body nvarchar(max));
SET @dist=(SELECT TOP (1) name FROM sys.databases WHERE is_distributor=1);
IF @dist IS NOT NULL
BEGIN
 INSERT @blocks(record_type,body) VALUES
 ('PUBLICATION', N'INSERT #repl(record_type,name,source_db,status_desc,detail)
   SELECT ''PUBLICATION'',p.publication,p.publisher_db,
    CASE p.publication_type WHEN 0 THEN ''TRANSACTIONAL'' WHEN 1 THEN ''SNAPSHOT'' WHEN 2 THEN ''MERGE'' ELSE CONVERT(nvarchar(20),p.publication_type) END,
    CAST(p.description AS nvarchar(2000))
   FROM '+QUOTENAME(@dist)+N'.dbo.MSpublications p;'),
 ('SUBSCRIPTION', N'INSERT #repl(record_type,name,source_db,target_db,status_desc,detail)
   SELECT ''SUBSCRIPTION'',CONVERT(nvarchar(20),s.subscriber_id),s.subscriber_db,s.subscriber_db,
    CONVERT(nvarchar(20),s.status),''subscription_type=''+CONVERT(nvarchar(20),s.subscription_type)
   FROM '+QUOTENAME(@dist)+N'.dbo.MSsubscriptions s;'),
 ('REPL_ERROR', N'INSERT #repl(record_type,name,last_action_time,error_id,error_count,error_message,detail)
   SELECT ''REPL_ERROR'',
    CONVERT(nvarchar(20),e.source_type_id)+N''|''+ISNULL(CAST(e.source_name AS nvarchar(200)),N''?'')+N''|error=''+CONVERT(nvarchar(20),e.error_code),
    MAX(e.[time]),e.error_code,COUNT(*),LEFT(MIN(CONVERT(nvarchar(2000),e.error_text)),2000),
    ''first_seen=''+CONVERT(nvarchar(30),MIN(e.[time]),121)
   FROM '+QUOTENAME(@dist)+N'.dbo.MSrepl_errors e
   GROUP BY e.source_type_id,e.source_name,e.error_code;'),
 ('LOGREADER_HISTORY', N'INSERT #repl(record_type,name,last_action_time,status_desc,error_count,error_message)
   SELECT ''LOGREADER_HISTORY'',CONVERT(nvarchar(20),h.agent_id),MAX(h.[time]),
    CASE h.runstatus WHEN 0 THEN ''FAILED'' WHEN 1 THEN ''SUCCEEDED'' WHEN 2 THEN ''RETRY'' WHEN 3 THEN ''CANCELLED'' WHEN 4 THEN ''INPROGRESS'' ELSE CONVERT(nvarchar(20),h.runstatus) END,
    COUNT(*),LEFT(MIN(CONVERT(nvarchar(2000),h.comments)),2000)
   FROM '+QUOTENAME(@dist)+N'.dbo.MSlogreader_history h
   WHERE h.[time]>=DATEADD(DAY,-7,GETDATE())
   GROUP BY h.agent_id,h.runstatus;'),
 ('DISTRIBUTION_HISTORY', N'INSERT #repl(record_type,name,last_action_time,status_desc,error_count,error_message)
   SELECT ''DISTRIBUTION_HISTORY'',CONVERT(nvarchar(20),h.agent_id),MAX(h.[time]),
    CASE h.runstatus WHEN 0 THEN ''FAILED'' WHEN 1 THEN ''SUCCEEDED'' WHEN 2 THEN ''RETRY'' WHEN 3 THEN ''CANCELLED'' WHEN 4 THEN ''INPROGRESS'' ELSE CONVERT(nvarchar(20),h.runstatus) END,
    COUNT(*),LEFT(MIN(CONVERT(nvarchar(2000),h.comments)),2000)
   FROM '+QUOTENAME(@dist)+N'.dbo.MSdistribution_history h
   WHERE h.[time]>=DATEADD(DAY,-7,GETDATE())
   GROUP BY h.agent_id,h.runstatus;');

 DECLARE @rt nvarchar(40),@body nvarchar(max),@err nvarchar(2000);
 DECLARE cb CURSOR LOCAL FAST_FORWARD FOR SELECT record_type,body FROM @blocks ORDER BY record_type;
 OPEN cb; FETCH NEXT FROM cb INTO @rt,@body;
 WHILE @@FETCH_STATUS=0
 BEGIN
  BEGIN TRY
   EXEC sys.sp_executesql @body;
  END TRY
  BEGIN CATCH
   SET @err=ERROR_MESSAGE();
   INSERT #repl(record_type,name,status_desc,detail)
   VALUES(@rt,N'(采集失败)',N'见 detail',LEFT(@err,2000));
  END CATCH;
  FETCH NEXT FROM cb INTO @rt,@body;
 END
 CLOSE cb; DEALLOCATE cb;
END
ELSE
BEGIN
 INSERT #repl(record_type,name,status_desc,detail)
 SELECT 'DISTRIBUTOR_MISSING',d.name,N'本实例不是分发服务器',
  N'该库带发布标志但没有本地分发库：发布元数据在远端分发服务器上，需在分发服务器上采集'
 FROM sys.databases d WHERE d.is_published=1;
END

SELECT record_type,name,source_db,target_server,target_db,status_desc,last_action_time,
 error_id,error_count,error_message,detail
FROM #repl ORDER BY record_type,name;
"@

# ===== 阶段 ⑤-深度：仅 -IncludeDeepChecks 时执行的三项（统计信息/碎片/孤立用户） =====
if ($IncludeDeepChecks) {
    Invoke-CollectionModule -Name '陈旧统计信息（深度）' -TargetKey 'stale_statistics' -TimeoutSeconds 300 -Query @"
CREATE TABLE #stats(database_name sysname,schema_name sysname,table_name sysname,stats_name sysname,last_updated datetime,row_count bigint,modification_counter bigint);
DECLARE @dbs sysname,@sqls nvarchar(max);
DECLARE cs CURSOR LOCAL FAST_FORWARD FOR SELECT name FROM sys.databases WHERE database_id>4 AND state=0 AND is_read_only=0;
OPEN cs; FETCH NEXT FROM cs INTO @dbs;
WHILE @@FETCH_STATUS=0
BEGIN
 SET @sqls=N'USE '+QUOTENAME(@dbs)+N'; INSERT #stats SELECT TOP (50) DB_NAME(),SCHEMA_NAME(o.schema_id),o.name,s.name,p.last_updated,p.rows,p.modification_counter FROM sys.stats s JOIN sys.objects o ON s.object_id=o.object_id CROSS APPLY sys.dm_db_stats_properties(s.object_id,s.stats_id) p WHERE o.type=''U'' AND p.modification_counter>0 ORDER BY CASE WHEN p.rows>0 THEN p.modification_counter*1.0/p.rows ELSE 0 END DESC;';
 EXEC sys.sp_executesql @sqls; FETCH NEXT FROM cs INTO @dbs;
END
CLOSE cs; DEALLOCATE cs;
SELECT * FROM #stats ORDER BY modification_counter DESC;
"@

    Invoke-CollectionModule -Name '索引碎片（深度）' -TargetKey 'fragmented_indexes' -TimeoutSeconds 600 -Query @"
CREATE TABLE #frag(database_name sysname,schema_name sysname,table_name sysname,index_name sysname,index_type_desc nvarchar(60),avg_fragmentation_in_percent float,page_count bigint);
DECLARE @dbf sysname,@sqlf nvarchar(max);
DECLARE cf CURSOR LOCAL FAST_FORWARD FOR SELECT name FROM sys.databases WHERE database_id>4 AND state=0 AND is_read_only=0;
OPEN cf; FETCH NEXT FROM cf INTO @dbf;
WHILE @@FETCH_STATUS=0
BEGIN
 SET @sqlf=N'USE '+QUOTENAME(@dbf)+N'; INSERT #frag SELECT TOP (50) DB_NAME(),SCHEMA_NAME(o.schema_id),o.name,i.name,ps.index_type_desc,ps.avg_fragmentation_in_percent,ps.page_count FROM sys.dm_db_index_physical_stats(DB_ID(),NULL,NULL,NULL,''LIMITED'') ps JOIN sys.indexes i ON ps.object_id=i.object_id AND ps.index_id=i.index_id JOIN sys.objects o ON i.object_id=o.object_id WHERE ps.index_id>0 AND ps.page_count>=1000 ORDER BY ps.avg_fragmentation_in_percent DESC;';
 EXEC sys.sp_executesql @sqlf; FETCH NEXT FROM cf INTO @dbf;
END
CLOSE cf; DEALLOCATE cf;
SELECT * FROM #frag ORDER BY avg_fragmentation_in_percent DESC;
"@

    Invoke-CollectionModule -Name '孤立用户（深度）' -TargetKey 'orphaned_users' -TimeoutSeconds 300 -Query @"
CREATE TABLE #orph(database_name sysname,user_name sysname,user_type_desc nvarchar(60),authentication_type_desc nvarchar(60));
DECLARE @dbo sysname,@sqlo nvarchar(max);
DECLARE co CURSOR LOCAL FAST_FORWARD FOR SELECT name FROM sys.databases WHERE database_id>4 AND state=0;
OPEN co; FETCH NEXT FROM co INTO @dbo;
WHILE @@FETCH_STATUS=0
BEGIN
 SET @sqlo=N'USE '+QUOTENAME(@dbo)+N'; INSERT #orph SELECT DB_NAME(),dp.name,dp.type_desc,dp.authentication_type_desc FROM sys.database_principals dp LEFT JOIN master.sys.server_principals sp ON dp.sid=sp.sid WHERE dp.authentication_type=1 AND dp.principal_id>4 AND sp.sid IS NULL AND dp.name NOT IN (''guest'',''INFORMATION_SCHEMA'',''sys'');';
 EXEC sys.sp_executesql @sqlo; FETCH NEXT FROM co INTO @dbo;
END
CLOSE co; DEALLOCATE co;
SELECT * FROM #orph ORDER BY database_name,user_name;
"@
} else {
    $snapshot.stale_statistics=@(); $snapshot.fragmented_indexes=@(); $snapshot.orphaned_users=@()
    [void]$limitations.Add('深度检查未启用：统计信息、索引碎片和孤立用户未扫描；使用 -IncludeDeepChecks 启用。')
}

Invoke-CollectionModule '错误日志摘要' 'error_log_summary' -TimeoutSeconds 300 @"
-- 旧版在中文实例上会静默返回 0 行，三个独立缺陷：
--   ① 只认英文：筛选 [Text] LIKE N'%Error%'、严重性 CHARINDEX('Severity:',[Text])。
--      中文实例写的是「错误: 14151，严重性: 18，状态: 1。」—— 整段日志里没有任何一行含
--      Error 或 Severity，于是每 60 秒触发一次的 14151 一条都抓不到，severity 恒 NULL。
--   ② 只读当前日志（xp_readerrorlog 0,1 即 Log #0），7 天窗口内的内容可能已轮转。
--   ③ 聚合键只有（日期, severity）：把"备份成功"这类 INFO 和真错误混在一张表里数。
-- 现在：语言无关筛选 + 剔除 informational/备份成功 + 顺序读取 #0..#9 直到超出窗口 +
--      错误号独立成列并参与聚合（同类错误单独成行，能看到频次）。
SET NOCOUNT ON; SET XACT_ABORT OFF;
DECLARE @since datetime=DATEADD(DAY,-7,GETDATE());
DECLARE @log table(LogDate datetime,ProcessInfo nvarchar(100),[Text] nvarchar(max));
DECLARE @chunk table(LogDate datetime,ProcessInfo nvarchar(100),[Text] nvarchar(max));
DECLARE @i int=0,@more bit=1;
WHILE @i<10 AND @more=1
BEGIN
 DELETE FROM @chunk;
 BEGIN TRY
  INSERT @chunk EXEC master.dbo.xp_readerrorlog @i,1;
 END TRY
 BEGIN CATCH
  SET @more=0;   -- 文件不存在或无权限读该文件：停止翻更旧的文件
 END CATCH;
 INSERT @log SELECT LogDate,ProcessInfo,[Text] FROM @chunk;
 IF NOT EXISTS (SELECT 1 FROM @chunk WHERE LogDate>=@since) SET @more=0;
 SET @i=@i+1;
END;
;WITH src AS (
 SELECT LogDate,[Text],
  CASE WHEN CHARINDEX(N'Severity:',[Text])>0 THEN SUBSTRING([Text],CHARINDEX(N'Severity:',[Text])+9,20)
       WHEN CHARINDEX(N'严重性',[Text])>0 THEN SUBSTRING([Text],CHARINDEX(N'严重性',[Text])+3,20)
       ELSE NULL END AS sev_raw,
  CASE WHEN CHARINDEX(N'错误',[Text])>0 THEN SUBSTRING([Text],CHARINDEX(N'错误',[Text])+2,12)
       WHEN CHARINDEX(N'Error:',[Text])>0 THEN SUBSTRING([Text],CHARINDEX(N'Error:',[Text])+6,12)
       WHEN PATINDEX('%[0-9][0-9][0-9][0-9][0-9]%',[Text])>0
        THEN SUBSTRING([Text],PATINDEX('%[0-9][0-9][0-9][0-9][0-9]%',[Text]),12)
       ELSE NULL END AS err_raw
 FROM @log
 WHERE LogDate>=@since
  AND ([Text] LIKE N'%Error%' OR [Text] LIKE N'%错误%' OR [Text] LIKE N'%failed%' OR [Text] LIKE N'%失败%')
  AND [Text] NOT LIKE N'%This is an informational message only%'
  AND [Text] NOT LIKE N'%Log was backed up%'
),
digits AS (
 SELECT LogDate,[Text],
  SUBSTRING(sev_raw,PATINDEX('%[0-9]%',sev_raw),12) AS sev_d,
  SUBSTRING(err_raw,PATINDEX('%[0-9]%',err_raw),12) AS err_d
 FROM src
),
norm AS (
 SELECT LogDate,[Text],
  -- 取"第一个数字串"：中英两式、全角/半角冒号都能命中；NULLIF(...,0) 让解析不到时留 NULL 而不是 0
  NULLIF(TRY_CONVERT(int,LEFT(sev_d,NULLIF(PATINDEX('%[^0-9]%',sev_d+N'x'),0)-1)),0) AS severity,
  TRY_CONVERT(int,LEFT(err_d,NULLIF(PATINDEX('%[^0-9]%',err_d+N'x'),0)-1)) AS error_number
 FROM digits
)
SELECT TOP (50) CONVERT(date,LogDate) event_date,severity,error_number,
 COUNT(*) event_count,LEFT(MIN([Text]),1000) sample_message
FROM norm
GROUP BY CONVERT(date,LogDate),severity,error_number
ORDER BY event_date DESC,event_count DESC;
"@

# ===================== 阶段 ⑥：OS 指标（仅当目标就是本机） =====================
# 为什么加这一层：报告 3.1–3.5（磁盘/CPU/内存/IO/网络）与 2.1 的主机硬件配置全部来自 OS 层，
# 而旧脚本 Get-Counter / Get-CimInstance / Get-NetAdapter 零命中 —— 那五节只能人工填表。
# 为什么必须判"是否本机"：下面取到的一切都属于【运行本脚本的机器】。远程连库时那是 DBA
# 笔记本的数据，写进客户报告等于伪造证据。远程一律 skipped 并写明原因（未采集 ≠ 0）。

function Test-LocalTarget {
    # 目标实例是否就是运行本脚本的机器。三条独立证据取"或"：
    #   ① 连接串字面量是 . / (local) / localhost
    #   ② SQL 侧报告的 machine_name（ServerName 的机器部分）与本机名一致
    #   ③ 连接 IP 落在本机网卡上
    if ([string]::IsNullOrWhiteSpace($Server)) { return $true }
    $target = $Server.Split('\')[0]
    if ($target -in @('.','(local)','localhost','127.0.0.1','::1','')) { return $true }
    $localNames = New-Object System.Collections.ArrayList
    foreach ($candidate in @($env:COMPUTERNAME, ([System.Net.Dns]::GetHostName()))) {
        if ($candidate) { [void]$localNames.Add([string]$candidate.ToLower()) }
    }
    if ($snapshot.instance) {
        $machineName = Get-ObjProp $snapshot.instance 'machine_name'
        if ($machineName) { [void]$localNames.Add([string]$machineName.ToLower()) }
    }
    if ($localNames -contains $target.ToLower()) { return $true }
    $targetIp = Get-ObjProp $snapshot.instance 'connection_ip'
    if ($targetIp) {
        try {
            $addresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | Select-Object -ExpandProperty IPAddress)
            if ($addresses -contains [string]$targetIp) { return $true }
        } catch { }
    }
    return $false
}

function Get-OsMetrics {
    # 取 OS 身份 + 硬件 + 物理盘介质类型 + N 个采样点的 CPU/内存/磁盘/网络计数器。
    # 计数器一律走 WMI 已格式化类（属性名是英文，不受中文 Windows 计数器名本地化影响）；
    # 格式化类是按"两次采样间隔"计算的，第一个点可能为 0/空 —— 因此整段序列都保留，
    # 由分析层决定取哪个点，而不是只留最后一点。
    $os = $null; $cs = $null
    try { $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop } catch { }
    try { $cs = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop } catch { }
    $identity = [ordered]@{
        computer_name = $(if ($cs) { $cs.Name } else { $env:COMPUTERNAME })
        os_caption = $(if ($os) { $os.Caption } else { $null })
        os_version = $(if ($os) { $os.Version } else { $null })
        os_build = $(if ($os) { $os.BuildNumber } else { $null })
        os_architecture = $(if ($os) { $os.OSArchitecture } else { $null })
        os_install_date = $(if ($os -and $os.InstallDate) { $os.InstallDate.ToString('o') } else { $null })
        last_boot_up_time = $(if ($os -and $os.LastBootUpTime) { $os.LastBootUpTime.ToString('o') } else { $null })
        manufacturer = $(if ($cs) { $cs.Manufacturer } else { $null })
        model = $(if ($cs) { $cs.Model } else { $null })
        physical_processors = $(if ($cs) { $cs.NumberOfProcessors } else { $null })
        logical_processors = $(if ($cs) { $cs.NumberOfLogicalProcessors } else { $null })
        total_physical_memory_mb = $(if ($cs) { [math]::Round($cs.TotalPhysicalMemory/1MB,0) } else { $null })
        total_visible_memory_mb = $(if ($os) { [math]::Round($os.TotalVisibleMemorySize/1KB,0) } else { $null })
        free_physical_memory_mb = $(if ($os) { [math]::Round($os.FreePhysicalMemory/1KB,0) } else { $null })
        page_file_total_mb = $(if ($os) { [math]::Round($os.SizeStoredInPagingFiles/1KB,0) } else { $null })
        page_file_free_mb = $(if ($os) { [math]::Round($os.FreeSpaceInPagingFiles/1KB,0) } else { $null })
        memory_used_pct = $null
    }
    if ($identity.total_visible_memory_mb -and $identity.free_physical_memory_mb) {
        $identity['memory_used_pct'] = [math]::Round(
            ($identity.total_visible_memory_mb - $identity.free_physical_memory_mb) * 100.0 / $identity.total_visible_memory_mb, 2)
    }
    $samples = New-Object System.Collections.ArrayList
    for ($n=0; $n -lt $SampleCount; $n++) {
        $point = [ordered]@{ collected_at=(Get-Date).ToString('o') }
        try {
            $p = Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -ErrorAction Stop |
                 Where-Object { $_.Name -eq '_Total' } | Select-Object -First 1
            if ($p) { $point['cpu_pct'] = [int]$p.PercentProcessorTime; $point['cpu_idle_pct'] = [int]$p.PercentIdleTime }
        } catch { }
        try {
            $s = Get-CimInstance Win32_PerfFormattedData_PerfOS_System -ErrorAction Stop | Select-Object -First 1
            if ($s) {
                $point['processor_queue_length'] = [int]$s.ProcessorQueueLength
                $point['context_switches_per_sec'] = [int]$s.ContextSwitchesPerSec
                $point['system_calls_per_sec'] = [int]$s.SystemCallsPerSec
            }
        } catch { }
        try {
            $mem = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory -ErrorAction Stop | Select-Object -First 1
            if ($mem) {
                $point['available_mb'] = [int]$mem.AvailableMBytes
                $point['pages_per_sec'] = [int]$mem.PagesPerSec
                $point['page_faults_per_sec'] = [int]$mem.PageFaultsPerSec
                $point['committed_mb'] = [int][math]::Round([double]$mem.CommittedBytes/1MB,0)
                $point['pool_paged_mb'] = [int][math]::Round([double]$mem.PoolPagedBytes/1MB,0)
                $point['pool_nonpaged_mb'] = [int][math]::Round([double]$mem.PoolNonpagedBytes/1MB,0)
            }
        } catch { }
        try {
            $point['disks'] = @(Get-CimInstance Win32_PerfFormattedData_PerfDisk_LogicalDisk -ErrorAction Stop | ForEach-Object {
                [pscustomobject][ordered]@{
                    name=$_.Name; pct_disk_time=[int]$_.PercentDiskTime; pct_idle_time=[int]$_.PercentIdleTime
                    avg_disk_queue_length=[double]$_.AvgDiskQueueLength
                    reads_per_sec=[int]$_.DiskReadsPerSec; writes_per_sec=[int]$_.DiskWritesPerSec
                    read_bytes_per_sec=[int]$_.DiskReadBytesPerSec; write_bytes_per_sec=[int]$_.DiskWriteBytesPerSec
                } })
        } catch { }
        try {
            $point['network'] = @(Get-CimInstance Win32_PerfFormattedData_Tcpip_NetworkInterface -ErrorAction Stop | ForEach-Object {
                [pscustomobject][ordered]@{
                    name=$_.Name; bytes_total_per_sec=[int]$_.BytesTotalPerSec
                    bytes_received_per_sec=[int]$_.BytesReceivedPerSec; bytes_sent_per_sec=[int]$_.BytesSentPerSec
                    packets_received_errors=[int]$_.PacketsReceivedErrors
                    packets_outbound_errors=[int]$_.PacketsOutboundErrors
                    output_queue_length=[int]$_.OutputQueueLength
                } })
        } catch { }
        [void]$samples.Add([pscustomobject]$point)
        if ($n -lt $SampleCount-1) { Start-Sleep -Seconds $SampleIntervalSeconds }
    }
    # 物理盘介质类型（HDD/SSD）：报告 3.4「D 盘机械硬盘的 I/O 瓶颈」这类结论靠它才能复现
    $disks = @()
    try {
        $disks = @(Get-PhysicalDisk -ErrorAction Stop | ForEach-Object {
            [pscustomobject][ordered]@{
                friendly_name=$_.FriendlyName; media_type=[string]$_.MediaType; bus_type=[string]$_.BusType
                size_gb=$(if ($_.Size) { [math]::Round($_.Size/1GB,2) } else { $null })
                health_status=[string]$_.HealthStatus; operational_status=[string]$_.OperationalStatus
                spindle_speed=$_.SpindleSpeed
            } })
    } catch { }
    # 盘符 -> 物理盘号，用于把卷和介质类型关联起来
    $diskMap = @()
    try {
        $diskMap = @(Get-Partition -ErrorAction Stop | Where-Object { $_.DriveLetter } | ForEach-Object {
            [pscustomobject][ordered]@{
                drive_letter=[string]$_.DriveLetter; disk_number=$_.DiskNumber
                size_gb=$(if ($_.Size) { [math]::Round($_.Size/1GB,2) } else { $null })
            } })
    } catch { }
    return [pscustomobject][ordered]@{
        collected_at=(Get-Date).ToString('o')
        computer_name=$identity.computer_name
        identity=[pscustomobject]$identity
        physical_disks=$disks
        disk_map=$diskMap
        samples=$samples
    }
}

$osModuleName = 'OS 指标'
if (Test-LocalTarget) {
    $osBegin = Get-Date
    Write-CollectorLog "Collecting: $osModuleName"
    try {
        $osMetrics = Get-OsMetrics
        $snapshot.os_metrics = $osMetrics
        [void]$moduleStatus.Add([ordered]@{name=$osModuleName;status='success';row_count=$osMetrics.samples.Count;elapsed_ms=[int]((Get-Date)-$osBegin).TotalMilliseconds;error=$null})
    } catch {
        $snapshot.os_metrics = [ordered]@{ identity=$null; physical_disks=@(); disk_map=@(); samples=@() }
        $message = $_.Exception.Message.Split("`n")[0]
        [void]$moduleStatus.Add([ordered]@{name=$osModuleName;status='failed';row_count=0;elapsed_ms=[int]((Get-Date)-$osBegin).TotalMilliseconds;error=$message})
        [void]$limitations.Add("${osModuleName}: ${message}")
        Write-CollectorLog "${osModuleName} failed: $message" 'WARN'
    }
} else {
    $snapshot.os_metrics = [ordered]@{ identity=$null; physical_disks=@(); disk_map=@(); samples=@() }
    Add-ModuleStatus -Name $osModuleName -Status 'skipped' -Reason "目标 '$Server' 不是本机：OS 计数器属于执行机，远程采集会把 DBA 机器的 CPU/内存/磁盘写进客户报告。需要 OS 章节时请在目标机上本地执行（-Server .）"
}

function Get-PerformancePoint {
    # 采样点同时带 CPU 调度压力（v1.3.2 新增）：runnable_tasks_count / work_queue_count /
    # pending_disk_io_count 是 CPU 压力最直接的现场证据 —— 此前采样只有 Batch/TPS/PLE/Grants/
    # Connections 五个计数器，没有任何 CPU 时间序列，"CPU 到底忙不忙"只能靠 OS 层（且仅本机可采）。
    # scheduler_id < 255 排除 DAC（255）；这些是瞬时值，不进差分计算。
    $query = @"
SELECT GETDATE() timestamp,
 MAX(CASE WHEN counter_name='Batch Requests/sec' THEN cntr_value END) batch_requests_raw,
 MAX(CASE WHEN counter_name='Transactions/sec' AND instance_name='_Total' THEN cntr_value END) transactions_raw,
 MAX(CASE WHEN counter_name='Page life expectancy' AND object_name LIKE '%Buffer Manager%' THEN cntr_value END) page_life_expectancy,
 MAX(CASE WHEN counter_name='Memory Grants Pending' THEN cntr_value END) memory_grants_pending,
 MAX(CASE WHEN counter_name='User Connections' THEN cntr_value END) user_connections,
 (SELECT COUNT(*) FROM sys.dm_exec_requests WHERE blocking_session_id<>0) blocked_session_count,
 (SELECT SUM(runnable_tasks_count) FROM sys.dm_os_schedulers WHERE scheduler_id<255) runnable_tasks_count,
 (SELECT MAX(runnable_tasks_count) FROM sys.dm_os_schedulers WHERE scheduler_id<255) runnable_tasks_max,
 (SELECT SUM(work_queue_count) FROM sys.dm_os_schedulers WHERE scheduler_id<255) work_queue_count,
 (SELECT SUM(pending_disk_io_count) FROM sys.dm_os_schedulers WHERE scheduler_id<255) pending_disk_io_count,
 (SELECT SUM(current_tasks_count) FROM sys.dm_os_schedulers WHERE scheduler_id<255) current_tasks_count
FROM sys.dm_os_performance_counters
WHERE counter_name IN ('Batch Requests/sec','Transactions/sec','Page life expectancy','Memory Grants Pending','User Connections');
"@
    $r=@(Invoke-InspectionQuery $query); if ($r.Count) { return $r[0] }; return $null
}

# ===== 阶段 ⑥-后置：性能采样（计数器 × N 个采样点，差分算 Batch/TPS） =====
$samples = @(); $previous=$null; $perfBegin = Get-Date
try {
    for ($n=0; $n -lt $SampleCount; $n++) {
        $point=Get-PerformancePoint
        if ($null -ne $point) {
            $point | Add-Member -NotePropertyName batch_requests_per_sec -NotePropertyValue $null -Force
            $point | Add-Member -NotePropertyName transactions_per_sec -NotePropertyValue $null -Force
            if ($null -ne $previous) {
                $seconds=([datetime]$point.timestamp-[datetime]$previous.timestamp).TotalSeconds
                if ($seconds -gt 0) {
                    $point.batch_requests_per_sec=[math]::Round(([double]$point.batch_requests_raw-[double]$previous.batch_requests_raw)/$seconds,2)
                    $point.transactions_per_sec=[math]::Round(([double]$point.transactions_raw-[double]$previous.transactions_raw)/$seconds,2)
                }
            }
            $samples += $point; $previous=$point
        }
        if ($n -lt $SampleCount-1) { Start-Sleep -Seconds $SampleIntervalSeconds }
    }
    $snapshot.performance_samples=$samples
    [void]$moduleStatus.Add([ordered]@{name='性能采样';status='success';row_count=$samples.Count;elapsed_ms=[int]((Get-Date)-$perfBegin).TotalMilliseconds;error=$null})
} catch {
    $snapshot.performance_samples=$samples; [void]$limitations.Add("性能采样: $($_.Exception.Message)")
    [void]$moduleStatus.Add([ordered]@{name='性能采样';status='failed';row_count=$samples.Count;elapsed_ms=[int]((Get-Date)-$perfBegin).TotalMilliseconds;error=$_.Exception.Message})
}

if ($ExcludeSqlText) {
    # 键 -> 需要抹除的字段名。job_steps.command_preview 与 sql_text 同级敏感：
    # 作业步骤命令里常见明文口令 / 连接串 / 业务逻辑，必须与 sql_text 一起抹（v1.3.2 起）。
    $redactions = @(
        @{key='blocking';field='sql_text'}, @{key='active_requests';field='sql_text'},
        @{key='top_queries';field='sql_text'}, @{key='top_queries_logical_reads';field='sql_text'},
        @{key='top_queries_executions';field='sql_text'}, @{key='top_queries_physical_reads';field='sql_text'},
        @{key='job_steps';field='command_preview'}
    )
    foreach ($r in $redactions) {
        foreach ($row in $snapshot[$r.key]) { if ($row.PSObject.Properties[$r.field]) { $row.($r.field)='[excluded]' } }
    }
}

# ===== 阶段 ⑦：写快照与清单 =====
$snapshot.collection.finished_at=(Get-Date).ToString('o')
$jsonPath=Join-Path $packageRoot 'snapshot.json'
$snapshot | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $jsonPath -Encoding UTF8
$manifest=[ordered]@{schema_version='1.1';collector_version=$collectorVersion;created_at=$snapshot.collection.finished_at;files=@('snapshot.json','collector.log')}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $packageRoot 'manifest.json') -Encoding UTF8
Write-CollectorLog "Snapshot written: $jsonPath"

# ===== 阶段 ⑧：Markdown 报告（脚本自带简报，Python 侧不使用） =====
Write-CollectorLog "Generating Markdown health report"
$reportPath = Join-Path $packageRoot 'inspection_report.md'
$report = New-Object System.Text.StringBuilder
function Add-MdLine($text='') { [void]$report.AppendLine($text) }
function Add-MdH1($t) { Add-MdLine "# $t"; Add-MdLine }
function Add-MdH2($t) { Add-MdLine; Add-MdLine "## $t"; Add-MdLine }
function Add-MdH3($t) { Add-MdLine; Add-MdLine "### $t"; Add-MdLine }

Add-MdH1 "SQL Server 巡检报告"
Add-MdLine "**采集时间**: $($snapshot.collection.started_at)  ~  $($snapshot.collection.finished_at)"
Add-MdLine "**采集器版本**: $collectorVersion  |  **Schema**: $($snapshot.schema_version)"
$inst = $snapshot.instance
Add-MdLine "**目标**: $($snapshot.target.server):$($snapshot.target.port)  |  **认证**: $($snapshot.target.authentication)"
# ⚠ $inst 是 $snapshot.instance：实例查询失败时它是空 [ordered]@{}，裸取键会抛 PropertyNotFoundStrict。
#   这一段在模块循环之外、没有 try/catch —— 一旦抛，包已写出但 zip 产不出来。
$instName = Get-ObjProp $inst 'server_name'
$instVer  = Get-ObjProp $inst 'product_version'
$instLvl  = Get-ObjProp $inst 'product_level'
$instEd   = Get-ObjProp $inst 'edition'
$instMem  = Get-ObjProp $inst 'physical_memory_mb'
if ($instName -or $instVer) {
    Add-MdLine "**实例**: $instName  |  **版本**: $instVer $instLvl  |  **Edition**: $instEd"
}
if ($instMem) { Add-MdLine "**内存**: $([math]::Round($instMem/1024)) GB  |  **CPU**: $(Get-ObjProp $inst 'cpu_count') cores ($(Get-ObjProp $inst 'socket_count') socket x $(Get-ObjProp $inst 'cores_per_socket'))  |  **启动时间**: $(Get-ObjProp $inst 'sqlserver_start_time')" }
Add-MdLine

# 1. Collection Status
Add-MdH2 "采集状态摘要"
$passCount = @($moduleStatus | Where-Object {$_.status -in @('success','success_no_data')}).Count
$failCount = @($moduleStatus | Where-Object {$_.status -eq 'failed'}).Count
$skipCount = @($moduleStatus | Where-Object {$_.status -eq 'skipped'}).Count
$naCount = @($moduleStatus | Where-Object {$_.status -eq 'not_applicable'}).Count
Add-MdLine "**模块**: 有数据 $passCount / 失败 $failCount / 跳过 $skipCount / 不适用 $naCount / 总计 $($moduleStatus.Count)"
if ($limitations.Count -gt 0) {
    Add-MdLine; Add-MdH3 "限制说明"
    foreach ($lim in $limitations) { Add-MdLine "- $lim" }
}

# 2. Databases
Add-MdH2 "数据库概况"
$dbs = $snapshot.databases
if ($null -ne $dbs -and $dbs.Count -gt 0) {
    Add-MdLine "| 数据库 | 兼容级别 | 恢复模式 | 状态 | 数据(MB) | 日志(MB) | 最后DBCC |"
    Add-MdLine "|--------|----------|----------|------|----------|----------|----------|"
    foreach ($db in $dbs) {
        $dbcc = if ($db.last_good_checkdb_time) { ([datetime]$db.last_good_checkdb_time).ToString('yyyy-MM-dd') } else { 'N/A' }
        Add-MdLine "| $($db.name) | $($db.compatibility_level) | $($db.recovery_model_desc) | $($db.state_desc) | $($db.data_size_mb) | $($db.log_size_mb) | $dbcc |"
    }
}

# 3. Backups
Add-MdH2 "备份状态"
$bk = $snapshot.backups
if ($null -ne $bk -and $bk.Count -gt 0) {
    Add-MdLine "| 数据库 | 恢复模式 | 最后完整备份 | 最后差异备份 | 最后日志备份 | 完整大小(MB) |"
    Add-MdLine "|--------|----------|--------------|--------------|--------------|--------------|"
    foreach ($b in $bk) {
        $fb = if ($b.last_full_backup) { ([datetime]$b.last_full_backup).ToString('yyyy-MM-dd HH:mm') } else { '**无备份**' }
        $db = if ($b.last_diff_backup) { ([datetime]$b.last_diff_backup).ToString('yyyy-MM-dd HH:mm') } else { '-' }
        $lb = if ($b.last_log_backup) { ([datetime]$b.last_log_backup).ToString('yyyy-MM-dd HH:mm') } else { '-' }
        Add-MdLine "| $($b.database_name) | $($b.recovery_model_desc) | $fb | $db | $lb | $($b.full_backup_size_mb) |"
    }
}
$nobk = $snapshot.no_backup_databases
if ($null -ne $nobk -and $nobk.Count -gt 0) {
    Add-MdLine; Add-MdH3 "**无备份数据库**"
    foreach ($n in $nobk) { Add-MdLine "- **$($n.database_name)** ($($n.recovery_model_desc), 创建于 $($n.create_date))" }
}

# 4. Performance
Add-MdH2 "性能概览"
$ws = $snapshot.wait_stats
if ($null -ne $ws -and $ws.Count -gt 0) {
    Add-MdH3 "TOP 10 等待类型"
    Add-MdLine "| 等待类型 | 等待时间(ms) | 平均等待(ms) | 等待次数 |"
    Add-MdLine "|----------|--------------|--------------|----------|"
    $wc = 0; foreach ($w in $ws) { if ($wc -ge 10) { break }; Add-MdLine "| $($w.wait_type) | $($w.wait_time_ms) | $($w.avg_wait_ms) | $($w.waiting_tasks_count) |"; $wc++ }
}

$ps = $snapshot.performance_samples
if ($null -ne $ps -and $ps.Count -gt 0) {
    $last = $ps[-1]
    Add-MdH3 "性能采样快照"
    Add-MdLine "| 指标 | 当前值 |"
    Add-MdLine "|------|--------|"
    if ($last.batch_requests_per_sec) { Add-MdLine "| Batch Requests/sec | $($last.batch_requests_per_sec) |" }
    if ($last.page_life_expectancy) { Add-MdLine "| Page Life Expectancy | $($last.page_life_expectancy) |" }
    if ($last.memory_grants_pending) { Add-MdLine "| Memory Grants Pending | $($last.memory_grants_pending) |" }
    if ($last.user_connections) { Add-MdLine "| User Connections | $($last.user_connections) |" }
    if ($null -ne $last.blocked_session_count) { Add-MdLine "| Blocked Sessions | $($last.blocked_session_count) |" }
}

$block = $snapshot.blocking
if ($null -ne $block -and $block.Count -gt 0) {
    Add-MdH3 "当前阻塞 ($($block.Count) 条)"
    Add-MdLine "| SPID | 阻塞源 | 等待类型 | 等待秒 | 数据库 | 登录名 | 程序 |"
    Add-MdLine "|------|--------|----------|--------|--------|--------|------|"
    foreach ($b in $block) {
        Add-MdLine "| $($b.session_id) | $($b.blocking_session_id) | $($b.wait_type) | $($b.wait_seconds) | $($b.database_name) | $($b.login_name) | $($b.program_name) |"
    }
}

# 5. Memory
Add-MdH2 "内存状态"
$bp = $snapshot.buffer_pool
if ($null -ne $bp -and $bp.Count -gt 0) {
    Add-MdH3 "缓冲池分布 TOP 10"
    Add-MdLine "| 数据库 | 缓存(MB) | 占比 |"
    Add-MdLine "|--------|----------|------|"
    [int]$bpi = 0; foreach ($b in $bp) { if ($bpi -ge 10) { break }; Add-MdLine "| $($b.database_name) | $($b.cache_size_mb) | $($b.cache_pct)% |"; $bpi++ }
}
$mem = $snapshot.memory_status
if ($null -ne $mem -and $mem.Count -gt 0) {
    $m = $mem[0]; Add-MdH3 "进程内存"
    Add-MdLine "- 物理内存使用: $($m.physical_memory_used_mb) MB"
    Add-MdLine "- 锁定页分配: $($m.locked_page_mb) MB"
    Add-MdLine "- 内存利用率: $($m.memory_utilization_percentage)%"
    Add-MdLine "- 可用提交限制: $($m.available_commit_limit_mb) MB"
}

$mcl = $snapshot.memory_clerks
if ($null -ne $mcl -and $mcl.Count -gt 0) {
    Add-MdH3 "内存组件 TOP 10"
    Add-MdLine "| 组件 | 占用(MB) | 保留虚拟(MB) | 提交虚拟(MB) |"
    Add-MdLine "|------|----------|--------------|--------------|"
    [int]$mci = 0; foreach ($c in $mcl) { if ($mci -ge 10) { break }; Add-MdLine "| $($c.type) | $($c.pages_mb) | $($c.reserved_mb) | $($c.committed_mb) |"; $mci++ }
}
$sm = $snapshot.system_memory
if ($null -ne $sm -and $sm.Count -gt 0) {
    $smr = $sm[0]
    Add-MdLine "- 操作系统物理内存: 总 $($smr.total_physical_memory_mb) MB / 可用 $($smr.available_physical_memory_mb) MB / 状态 $($smr.system_memory_state_desc)"
}

# 6. TempDB
$tdb = $snapshot.tempdb
if ($tdb -and $tdb.PSObject.Properties['total_mb']) {
    Add-MdH2 "TempDB"
    Add-MdLine "| 总量(MB) | 已用(MB) | 空闲(MB) | 使用率 |"
    Add-MdLine "|----------|----------|----------|--------|"
    Add-MdLine "| $($tdb.total_mb) | $($tdb.used_mb) | $($tdb.free_mb) | $($tdb.used_pct)% |"
}

# 7. Volumes
$vol = $snapshot.volumes
if ($null -ne $vol -and $vol.Count -gt 0) {
    Add-MdH2 "存储卷"
    Add-MdLine "| 挂载点 | 卷名 | 文件系统 | 总量(MB) | 可用(MB) | 空闲 | 来源 |"
    Add-MdLine "|--------|------|----------|----------|----------|------|------|"
    foreach ($v in $vol) {
        # free_pct 可能为 $null（xp_fixeddrives / 备份盘推导出来的卷只有剩余 MB，没有可用率）
        $vFree = Get-ObjProp $v 'free_pct'
        $state = if ($null -ne $vFree -and $vFree -lt 10) { "**`$($vFree)%**" } else { "$($vFree)%" }
        Add-MdLine "| $($v.volume_mount_point) | $($v.logical_volume_name) | $($v.file_system_type) | $($v.total_mb) | $($v.available_mb) | $state | $($v.source) |"
    }
}

# OS 指标（仅本机目标时才有内容）
$os = $snapshot.os_metrics
if ($os -and $os.samples -and $os.samples.Count -gt 0) {
    Add-MdH2 "OS 指标（本机采集）"
    # ⚠ 采样点里的 cpu_pct / available_mb / processor_queue_length / disks / network 都是
    #   【每个性能计数器各自 try/catch 后条件写入】的：某项计数器库不可用（WMI/PerfDisk 常出问题）
    #   就没有这个键。这里必须走 Get-ObjProp，否则"某计数器取不到"会把整段报告打挂。
    $osi = Get-ObjProp $os 'identity'
    if ($osi) {
        Add-MdLine "- 操作系统: $(Get-ObjProp $osi 'os_caption') $(Get-ObjProp $osi 'os_version') (Build $(Get-ObjProp $osi 'os_build'), $(Get-ObjProp $osi 'os_architecture'))"
        Add-MdLine "- 主机: $(Get-ObjProp $osi 'manufacturer') $(Get-ObjProp $osi 'model') | CPU: $(Get-ObjProp $osi 'logical_processors') 逻辑处理器 ($(Get-ObjProp $osi 'physical_processors') 物理) | 内存: $(Get-ObjProp $osi 'total_visible_memory_mb') MB（已用 $(Get-ObjProp $osi 'memory_used_pct')%）"
    }
    $last = $os.samples[-1]
    $lastCpu   = Get-ObjProp $last 'cpu_pct'
    $lastAvail = Get-ObjProp $last 'available_mb'
    $lastQueue = Get-ObjProp $last 'processor_queue_length'
    $lastDisks = Get-ObjArray $last 'disks'
    $lastNet   = Get-ObjArray $last 'network'
    Add-MdLine "- 最近采样: CPU $lastCpu% | 可用内存 $lastAvail MB | 处理器队列 $lastQueue"
    $osDisks = Get-ObjArray $os 'physical_disks'
    if ($osDisks -and $osDisks.Count -gt 0) {
        Add-MdLine
        Add-MdLine "| 物理盘 | 介质类型 | 总线 | 容量(GB) | 健康 |"
        Add-MdLine "|--------|----------|------|----------|------|"
        foreach ($pd in $osDisks) {
            Add-MdLine "| $($pd.friendly_name) | $($pd.media_type) | $($pd.bus_type) | $($pd.size_gb) | $($pd.health_status) |"
        }
    }
    if ($lastDisks -and $lastDisks.Count -gt 0) {
        Add-MdLine
        Add-MdLine "| 卷 | 磁盘时间% | 平均队列 | 读/秒 | 写/秒 |"
        Add-MdLine "|-----|-----------|----------|-------|-------|"
        foreach ($d in $lastDisks) { Add-MdLine "| $($d.name) | $($d.pct_disk_time) | $($d.avg_disk_queue_length) | $($d.reads_per_sec) | $($d.writes_per_sec) |" }
    }
    if ($lastNet -and $lastNet.Count -gt 0) {
        Add-MdLine
        Add-MdLine "| 网卡 | 总字节/秒 | 收字节/秒 | 发字节/秒 | 收包错误 |"
        Add-MdLine "|------|-----------|-----------|-----------|----------|"
        foreach ($nc in $lastNet) { Add-MdLine "| $($nc.name) | $($nc.bytes_total_per_sec) | $($nc.bytes_received_per_sec) | $($nc.bytes_sent_per_sec) | $($nc.packets_received_errors) |" }
    }
}
$ss = $snapshot.server_services
if ($null -ne $ss -and $ss.Count -gt 0) {
    Add-MdH2 "服务与安装路径"
    Add-MdLine "| 服务 | 账户 | 启动类型 | 状态 | 可执行文件 |"
    Add-MdLine "|------|------|----------|------|------------|"
    foreach ($s in $ss) { Add-MdLine "| $($s.servicename) | $($s.service_account) | $($s.startup_type_desc) | $($s.status_desc) | $($s.filename) |" }
}

# 8. High Availability
Add-MdH2 "高可用概览"
$ag = $snapshot.availability_replicas
if ($null -ne $ag -and $ag.Count -gt 0) {
    Add-MdH3 "Always On 可用性组"
    Add-MdLine "| AG名称 | 副本服务器 | 数据库 | 角色 | 同步状态 | 同步健康 | 日志发送队列(MB) | 重做队列(MB) |"
    Add-MdLine "|--------|------------|--------|------|----------|----------|-------------------|--------------|"
    foreach ($a in $ag) { Add-MdLine "| $($a.availability_group) | $($a.replica_server_name) | $($a.database_name) | $($a.role_desc) | $($a.synchronization_state_desc) | $($a.synchronization_health_desc) | $($a.log_send_queue_mb) | $($a.redo_queue_mb) |" }
}
$dm = $snapshot.database_mirroring
if ($null -ne $dm -and $dm.Count -gt 0) {
    Add-MdH3 "数据库镜像"
    foreach ($m in $dm) { Add-MdLine "- **$($m.database_name)**: $($m.mirroring_role_desc) / $($m.mirroring_state_desc) / $($m.mirroring_safety_level_desc)" }
}

# 9. Security
$sec = $snapshot.security
if ($null -ne $sec) {
    Add-MdH2 "安全"
    $sc = $sec.security_checks
    if ($null -ne $sc -and $sc.Count -gt 0) {
        Add-MdLine "| 检查项 | 结果 | 详情 |"
        Add-MdLine "|--------|------|------|"
        foreach ($c in $sc) { $icon = if ($c.result -eq 'PASS') { 'OK' } else { '**WARN**' }; Add-MdLine "| $($c.check_name) | $icon | $($c.detail) |" }
    }
    $sysadm = $sec.sysadmin_members
    if ($null -ne $sysadm -and $sysadm.Count -gt 0) {
        Add-MdH3 "Sysadmin 成员"
        foreach ($sa in $sysadm) { $d = if ($sa.is_disabled) { ' [已禁用]' } else { '' }; Add-MdLine "- $($sa.name) ($($sa.type_desc))$d" }
    }
}

# 10. Agent Jobs
Add-MdH2 "SQL Agent 作业"
$fjob = $snapshot.failed_jobs
if ($null -ne $fjob -and $fjob.Count -gt 0) {
    Add-MdH3 "**失败作业（近24小时）**"
    Add-MdLine "| 作业名 | 运行时间 | 消息 |"
    Add-MdLine "|--------|----------|------|"
    foreach ($j in $fjob) { Add-MdLine "| $($j.job_name) | $($j.run_datetime) | $(([string]$j.message).Substring(0,[Math]::Min(200,([string]$j.message).Length))) |" }
} else { Add-MdLine "近24小时内无失败作业。" }

# 11. Error Log
Add-MdH2 "错误日志摘要（近7天）"
$el = $snapshot.error_log_summary
if ($null -ne $el -and $el.Count -gt 0) {
    Add-MdLine "| 日期 | 严重性 | 错误号 | 次数 | 示例 |"
    Add-MdLine "|------|--------|--------|------|------|"
    [int]$eli = 0; foreach ($e in $el) { if ($eli -ge 15) { break }; Add-MdLine "| $($e.event_date) | $($e.severity) | $($e.error_number) | $($e.event_count) | $(([string]$e.sample_message).Substring(0,[Math]::Min(80,([string]$e.sample_message).Length))) |"; $eli++ }
}

# Footer
Add-MdLine; Add-MdLine "---"
Add-MdLine "*报告由 SQL Server Inspection Collector v$collectorVersion 自动生成*"

$reportContent = $report.ToString()
Set-Content -LiteralPath $reportPath -Value $reportContent -Encoding UTF8
Write-CollectorLog "Markdown report written: $reportPath"
$manifest.files += @('inspection_report.md')
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $packageRoot 'manifest.json') -Encoding UTF8

if ($NoPackage) {
    Write-Host "OUTPUT_DIR=$packageRoot"
} else {
    $zipPath="$packageRoot.zip"
    Compress-Archive -LiteralPath $packageRoot -DestinationPath $zipPath -Force
    Write-CollectorLog "Package written: $zipPath"
    Write-Host "OUTPUT_PACKAGE=$zipPath"
}

# 人类可读回显：默认目录在 %TEMP% 下，RDP / 多会话机器上还会多一层 \2\ \3\，
# 现场去 AppData 里翻很费劲，所以把最终产物路径单独打一遍。
# ⚠ 默认值**不得**改成"脚本自身所在目录"（AGENTS.md：采集脚本不得依赖自身所在目录，
#   否则脚本在仓库 inspection\ 下被直接执行时，采集包会落进仓库）——这里只做提示，不改默认值。
$finalArtifact = $packageRoot
if (-not $NoPackage) { $finalArtifact = $zipPath }
Write-Host ''
Write-Host '--- 采集产物路径 ---------------------------------------------------'
Write-Host "  $finalArtifact"
Write-Host "  默认落在 %TEMP% 下；要落到顺手的位置就加 -OutputDir，例如 -OutputDir `"`$env:USERPROFILE\Desktop`""
Write-Host '--------------------------------------------------------------------'

# ===== 阶段 ⑨：退出码 =====
# 契约：0 = 无 failed 模块；2 = 至少一个模块 failed（采集包照常产出）。
# 旧版无论多少模块失败都返回 0，流水线无法区分"全成功"与"成功但有 5 个模块失败"。
# 点源（. .\script.ps1）时不 exit，避免把宿主会话一起退掉。
$failedModules = @($moduleStatus | Where-Object { $_.status -eq 'failed' })
if ($failedModules.Count -gt 0) {
    Write-CollectorLog ("{0} module(s) failed: {1}" -f $failedModules.Count, (($failedModules | ForEach-Object { $_.name }) -join ', ')) 'WARN'
}
if ($MyInvocation.InvocationName -ne '.') {
    if ($failedModules.Count -gt 0) { exit 2 } else { exit 0 }
}



