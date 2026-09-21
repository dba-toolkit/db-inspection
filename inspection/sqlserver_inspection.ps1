#requires -Version 5.1
<#
.SYNOPSIS
  SQL Server read-only inspection collector v1.1.
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
    [string]$OutputDir = $PSScriptRoot,
    [ValidateRange(1,120)][int]$SampleCount = 6,
    [ValidateRange(1,300)][int]$SampleIntervalSeconds = 5,
    [switch]$ExcludeSqlText,
    [switch]$IncludeDeepChecks,
    [switch]$NoPackage,
    [switch]$Help
)

if ($Help) {
    Write-Host @"
SQL Server 只读巡检采集器 v1.1.0
===================================
用法:
  .\sqlserver_inspection.ps1 [参数]

参数:
  -Server      SQL Server 地址 (默认: 交互输入)
  -Port        端口 (默认: 0 = 自动检测)
  -User        SQL 登录名 (默认: '' = Windows 集成认证)
  -Password    SQL 密码 (SecureString, 默认: 交互输入)
  -Database    初始数据库 (默认: master)
  -OutputDir   输出目录 (默认: 脚本所在目录)
  -SampleCount 性能采样点数 (1-120, 默认: 6)
  -SampleIntervalSeconds 采样间隔秒 (1-300, 默认: 5)
  -ExcludeSqlText         隐藏 SQL 文本
  -IncludeDeepChecks      启用深度检查 (统计信息/碎片/孤立用户)
  -NoPackage              只输出目录不打包 ZIP
  -Help                   显示此帮助

示例:
  .\sqlserver_inspection.ps1                              (交互式输入)
  .\sqlserver_inspection.ps1 -Server 10.0.0.10 -Port 1433
  .\sqlserver_inspection.ps1 -Server . -IncludeDeepChecks
  .\sqlserver_inspection.ps1 -Server 10.0.0.10 -User sa
"@
    exit 0
}

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$collectorVersion = '1.1.0'

# PSScriptRoot may be empty in ISE or dot-sourced runs
if (-not $OutputDir -or $OutputDir -eq '') { $OutputDir = Get-Location }
if (-not (Test-Path -LiteralPath $OutputDir)) {
    Write-Warning "OutputDir '$OutputDir' 不存在，使用当前目录"
    $OutputDir = Get-Location
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
    if ($Value -eq [DBNull]::Value -or $null -eq $Value) { return $null }
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
    schema_version = '1.0'
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
        [void]$moduleStatus.Add([ordered]@{ name=$Name; status='success'; row_count=$snapshot[$TargetKey].Count; elapsed_ms=[int]((Get-Date)-$begin).TotalMilliseconds; error=$null })
    } catch {
        $snapshot[$TargetKey] = @()
        $message = $_.Exception.Message.Split("`n")[0]
        [void]$moduleStatus.Add([ordered]@{ name=$Name; status='failed'; row_count=0; elapsed_ms=[int]((Get-Date)-$begin).TotalMilliseconds; error=$message })
        [void]$limitations.Add("${Name}: ${message}")
        Write-CollectorLog "${Name} failed: $message" 'WARN'
    }
}

Write-CollectorLog "Collector $collectorVersion started for $Server"
try {
    $test = New-Object System.Data.SqlClient.SqlConnection($script:ConnectionString)
    $test.Open(); $test.Close(); $test.Dispose()
} catch {
    Write-CollectorLog "Connection failed: $($_.Exception.Message)" 'ERROR'
    throw
}

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
  osi.cpu_count, osi.scheduler_count, osi.socket_count, osi.cores_per_socket,
  osi.numa_node_count, osi.physical_memory_kb / 1024 AS physical_memory_mb,
  osi.sqlserver_start_time,
  CAST(@@VERSION AS nvarchar(4000)) AS version_banner,
  CAST(NULL AS nvarchar(4000)) AS os_version,
  (SELECT TOP (1) local_net_address FROM sys.dm_exec_connections WHERE session_id=@@SPID AND local_net_address IS NOT NULL) AS connection_ip,
  (SELECT TOP (1) local_tcp_port FROM sys.dm_exec_connections WHERE session_id=@@SPID AND local_tcp_port IS NOT NULL) AS connection_port
FROM sys.dm_os_sys_info AS osi;
"@
try {
    $rows = @(Invoke-InspectionQuery $instanceSql)
    $snapshot.instance = if ($rows.Count) { $rows[0] } else { [ordered]@{} }
    [void]$moduleStatus.Add([ordered]@{name='实例基础信息';status='success';row_count=$rows.Count;elapsed_ms=0;error=$null})
} catch {
    $snapshot.instance = [ordered]@{}
    [void]$moduleStatus.Add([ordered]@{name='实例基础信息';status='failed';row_count=0;elapsed_ms=0;error=$_.Exception.Message})
    [void]$limitations.Add("实例基础信息: $($_.Exception.Message)")
}

# 本地连接时 sys.dm_exec_connections 的 local_net_address 可能为 NULL，补用 PowerShell 探测本机 IPv4
if (-not $snapshot.instance.connection_ip) {
    try {
        $localIps = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notmatch '^127\.' -and $_.IPAddress -notmatch '^169\.254\.' -and $_.IPAddress -ne '0.0.0.0' } |
            Sort-Object InterfaceMetric | Select-Object -ExpandProperty IPAddress)
        if ($localIps.Count -gt 0) { $snapshot.instance.connection_ip = $localIps[0] }
    } catch { }
}
if (-not $snapshot.instance.connection_port) {
    $snapshot.instance.connection_port = if ($Port -gt 0) { $Port } else { 1433 }
}

# 用实际采集的实例名 + IP + 端口重命名输出目录
$realServer = $snapshot.instance.server_name
if ($realServer) {
    $safeTarget = ($realServer -replace '[^A-Za-z0-9_.-]', '_')
    $ip = $snapshot.instance.connection_ip
    $realPort = if ($snapshot.instance.connection_port) { $snapshot.instance.connection_port } else { $Port }
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
try { $productMajor = [int]([string]$snapshot.instance.product_version).Split('.')[0] } catch { $productMajor = 0 }
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
SELECT database_name,type,backup_finish_date,
 CAST(backup_size/1048576.0 AS decimal(18,2)) backup_size_mb,
 CAST(compressed_backup_size/1048576.0 AS decimal(18,2)) compressed_size_mb,
 CASE WHEN is_copy_only=1 THEN 'YES' ELSE 'NO' END is_copy_only,
 has_backup_checksums
FROM msdb.dbo.backupset
WHERE backup_finish_date>=DATEADD(DAY,-7,GETDATE()) AND type<>'F'
ORDER BY backup_finish_date DESC;
"@

Invoke-CollectionModule '无备份数据库' 'no_backup_databases' @"
SELECT d.name database_name,d.recovery_model_desc,d.create_date,
 DATEDIFF(DAY,d.create_date,GETDATE()) days_since_created
FROM sys.databases d
WHERE d.name NOT IN ('tempdb') AND d.source_database_id IS NULL AND d.state_desc='ONLINE'
 AND d.name NOT IN (SELECT DISTINCT database_name FROM msdb.dbo.backupset WHERE type='D')
ORDER BY d.name;
"@

Invoke-CollectionModule '数据库RCSI状态' 'database_rcsi' @"
SELECT name database_name,
 is_read_committed_snapshot_on RCSI_enabled,
 snapshot_isolation_state_desc SI_state
FROM sys.databases WHERE database_id>4 ORDER BY name;
"@

Invoke-CollectionModule '数据库大小汇总' 'database_summary' @"
SELECT COUNT(*) user_db_count,
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
SELECT DISTINCT vs.volume_mount_point,vs.logical_volume_name,vs.file_system_type,
 CAST(vs.total_bytes/1048576.0 AS decimal(18,2)) total_mb,
 CAST(vs.available_bytes/1048576.0 AS decimal(18,2)) available_mb,
 CAST(vs.available_bytes*100.0/NULLIF(vs.total_bytes,0) AS decimal(10,2)) free_pct
FROM sys.master_files mf CROSS APPLY sys.dm_os_volume_stats(mf.database_id,mf.file_id) vs
ORDER BY vs.volume_mount_point;
"@

Invoke-CollectionModule '事务日志空间' 'log_space' @"
CREATE TABLE #logspace(database_name sysname,log_size_mb float,log_used_pct float,status int);
INSERT #logspace EXEC('DBCC SQLPERF(LOGSPACE) WITH NO_INFOMSGS');
SELECT database_name,CAST(log_size_mb AS decimal(18,2)) log_size_mb,
 CAST(log_used_pct AS decimal(10,2)) log_used_pct,status FROM #logspace ORDER BY log_used_pct DESC;
"@

Invoke-CollectionModule 'VLF摘要' 'vlf_summary' @"
IF TRY_CONVERT(int,PARSENAME(CAST(SERVERPROPERTY('ProductVersion') AS varchar(50)),4))>=14
BEGIN
 CREATE TABLE #vlf(database_name sysname,vlf_count int,active_vlf_count int);
 DECLARE @db sysname,@sql nvarchar(max);
 DECLARE c CURSOR LOCAL FAST_FORWARD FOR SELECT name FROM sys.databases WHERE state=0 AND source_database_id IS NULL;
 OPEN c; FETCH NEXT FROM c INTO @db;
 WHILE @@FETCH_STATUS=0
 BEGIN
  SET @sql=N'USE '+QUOTENAME(@db)+N'; INSERT #vlf SELECT DB_NAME(),COUNT(*),SUM(CASE WHEN vlf_active=1 THEN 1 ELSE 0 END) FROM sys.dm_db_log_info(DB_ID());';
  EXEC sys.sp_executesql @sql; FETCH NEXT FROM c INTO @db;
 END
 CLOSE c; DEALLOCATE c;
 SELECT * FROM #vlf ORDER BY vlf_count DESC;
END
ELSE SELECT TOP (0) CAST(NULL AS sysname) database_name,CAST(NULL AS int) vlf_count,CAST(NULL AS int) active_vlf_count;
"@

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

Invoke-CollectionModule '高消耗SQL' 'top_queries' @"
SELECT TOP (30) DB_NAME(st.dbid) database_name,qs.execution_count,
 qs.total_worker_time/1000.0 total_cpu_ms,qs.total_worker_time/NULLIF(qs.execution_count,0)/1000.0 avg_cpu_ms,
 qs.total_logical_reads,qs.total_logical_reads/NULLIF(qs.execution_count,0) avg_logical_reads,
 qs.total_elapsed_time/1000.0 total_elapsed_ms,qs.last_execution_time,
 LEFT(REPLACE(REPLACE(st.text,CHAR(13),' '),CHAR(10),' '),4000) sql_text
FROM sys.dm_exec_query_stats qs CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
ORDER BY qs.total_worker_time DESC;
"@

Invoke-CollectionModule '高逻辑读SQL' 'top_queries_logical_reads' @"
SELECT TOP (30) DB_NAME(st.dbid) database_name,qs.execution_count,
 qs.total_logical_reads,qs.total_logical_reads/NULLIF(qs.execution_count,0) avg_logical_reads,
 qs.total_worker_time/1000.0 total_cpu_ms,qs.total_elapsed_time/1000.0 total_elapsed_ms,
 qs.last_execution_time,
 LEFT(REPLACE(REPLACE(st.text,CHAR(13),' '),CHAR(10),' '),4000) sql_text
FROM sys.dm_exec_query_stats qs CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
ORDER BY qs.total_logical_reads DESC;
"@

Invoke-CollectionModule '高执行次数SQL' 'top_queries_executions' @"
SELECT TOP (30) DB_NAME(st.dbid) database_name,qs.execution_count,
 qs.total_worker_time/1000.0 total_cpu_ms,qs.total_elapsed_time/1000.0 total_elapsed_ms,
 qs.total_logical_reads,qs.total_physical_reads,qs.last_execution_time,
 LEFT(REPLACE(REPLACE(st.text,CHAR(13),' '),CHAR(10),' '),4000) sql_text
FROM sys.dm_exec_query_stats qs CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
ORDER BY qs.execution_count DESC;
"@

Invoke-CollectionModule '高物理读SQL' 'top_queries_physical_reads' @"
SELECT TOP (30) DB_NAME(st.dbid) database_name,qs.execution_count,
 qs.total_physical_reads,qs.total_physical_reads/NULLIF(qs.execution_count,0) avg_physical_reads,
 qs.total_worker_time/1000.0 total_cpu_ms,qs.total_elapsed_time/1000.0 total_elapsed_ms,
 qs.last_execution_time,
 LEFT(REPLACE(REPLACE(st.text,CHAR(13),' '),CHAR(10),' '),4000) sql_text
FROM sys.dm_exec_query_stats qs CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
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
WHERE name IN ('max server memory (MB)','min server memory (MB)','max degree of parallelism',
 'cost threshold for parallelism','optimize for ad hoc workloads','backup compression default',
 'remote admin connections','xp_cmdshell','Ole Automation Procedures','contained database authentication',
 'clr enabled','Database Mail XPs','ad hoc distributed queries',
 'fill factor (%)','scan for startup procs','show advanced options')
ORDER BY name;
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

Invoke-CollectionModule 'CPU按数据库分布' 'cpu_by_database' @"
SELECT TOP (15) DB_NAME(st.dbid) database_name,
 SUM(qs.total_worker_time)/1000 total_cpu_ms,
 SUM(qs.execution_count) execution_count,
 CAST(SUM(qs.total_worker_time)*1.0/NULLIF(SUM(qs.execution_count)*1000,0) AS decimal(18,2)) avg_cpu_ms,
 SUM(qs.total_logical_reads) total_logical_reads,
 SUM(qs.total_physical_reads) total_physical_reads,
 SUM(qs.total_elapsed_time)/1000 total_elapsed_ms
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
WHERE st.dbid IS NOT NULL AND DB_NAME(st.dbid) IS NOT NULL
GROUP BY st.dbid ORDER BY total_cpu_ms DESC;
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
try {
    $t = @(Invoke-InspectionQuery $tempdbSql); $tf = @(Invoke-InspectionQuery $tempdbFilesSql)
    $snapshot.tempdb = if ($t.Count) { $t[0] } else { [ordered]@{} }
    $snapshot.tempdb | Add-Member -NotePropertyName files -NotePropertyValue $tf -Force
    [void]$moduleStatus.Add([ordered]@{name='TempDB';status='success';row_count=$tf.Count;elapsed_ms=0;error=$null})
} catch {
    $snapshot.tempdb=[ordered]@{files=@()}; [void]$limitations.Add("TempDB: $($_.Exception.Message)")
    [void]$moduleStatus.Add([ordered]@{name='TempDB';status='failed';row_count=0;elapsed_ms=0;error=$_.Exception.Message})
}

Invoke-CollectionModule -Name '无主键表（深度）' -TargetKey 'tables_without_pk' -TimeoutSeconds 300 -Query @"
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
DROP TABLE #nopk;
"@

Invoke-CollectionModule -Name '大表统计（深度）' -TargetKey 'large_tables' -TimeoutSeconds 300 -Query @"
CREATE TABLE #lg(database_name sysname,schema_name sysname,table_name sysname,row_count bigint,total_mb decimal(18,2),used_mb decimal(18,2));
DECLARE @dblg sysname,@sqllg nvarchar(max);
DECLARE clg CURSOR LOCAL FAST_FORWARD FOR SELECT name FROM sys.databases WHERE state=0 AND name NOT IN ('master','model','msdb','tempdb');
OPEN clg; FETCH NEXT FROM clg INTO @dblg;
WHILE @@FETCH_STATUS=0
BEGIN
 SET @sqllg=N'USE '+QUOTENAME(@dblg)+N'; INSERT #lg SELECT TOP (50) DB_NAME(),SCHEMA_NAME(o.schema_id),o.name,p.rows,CAST(SUM(a.total_pages)*8.0/1024 AS decimal(18,2)),CAST(SUM(a.used_pages)*8.0/1024 AS decimal(18,2)) FROM sys.tables o JOIN sys.indexes i ON o.object_id=i.object_id JOIN sys.partitions p ON i.object_id=p.object_id AND i.index_id=p.index_id JOIN sys.allocation_units a ON p.partition_id=a.container_id GROUP BY o.name,o.schema_id,p.rows ORDER BY SUM(a.total_pages) DESC;';
 EXEC sys.sp_executesql @sqllg; FETCH NEXT FROM clg INTO @dblg;
END
CLOSE clg; DEALLOCATE clg;
SELECT TOP (30) * FROM #lg ORDER BY row_count DESC;
DROP TABLE #lg;
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
try {
    $snapshot.security=[ordered]@{
        sql_logins=@(Invoke-InspectionQuery $sqlLoginsSql)
        sysadmin_members=@(Invoke-InspectionQuery $sysadminSql)
        linked_servers=@(Invoke-InspectionQuery $linkedServersSql)
        security_checks=@(Invoke-InspectionQuery $securityChecksSql)
    }
    [void]$moduleStatus.Add([ordered]@{name='安全与权限';status='success';row_count=$snapshot.security.sql_logins.Count;elapsed_ms=0;error=$null})
} catch {
    $snapshot.security=[ordered]@{sql_logins=@();sysadmin_members=@();linked_servers=@();security_checks=@()}; [void]$limitations.Add("安全与权限: $($_.Exception.Message)")
    [void]$moduleStatus.Add([ordered]@{name='安全与权限';status='failed';row_count=0;elapsed_ms=0;error=$_.Exception.Message})
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

Invoke-CollectionModule 'Always On' 'availability_replicas' @"
IF CAST(SERVERPROPERTY('IsHadrEnabled') AS int)=1
SELECT ag.name availability_group,ar.replica_server_name,DB_NAME(drs.database_id) database_name,
 ars.role_desc,ars.connected_state_desc,drs.synchronization_state_desc,drs.synchronization_health_desc,
 CAST(drs.log_send_queue_size/1024.0 AS decimal(18,2)) log_send_queue_mb,
 CAST(drs.redo_queue_size/1024.0 AS decimal(18,2)) redo_queue_mb,drs.last_commit_time
FROM sys.dm_hadr_database_replica_states drs
JOIN sys.availability_replicas ar ON drs.replica_id=ar.replica_id
JOIN sys.availability_groups ag ON ar.group_id=ag.group_id
LEFT JOIN sys.dm_hadr_availability_replica_states ars ON ar.replica_id=ars.replica_id AND drs.group_id=ars.group_id;
ELSE SELECT TOP (0) CAST(NULL AS nvarchar(128)) availability_group,CAST(NULL AS nvarchar(256)) replica_server_name,
CAST(NULL AS sysname) database_name,CAST(NULL AS nvarchar(60)) role_desc,CAST(NULL AS nvarchar(60)) connected_state_desc,
CAST(NULL AS nvarchar(60)) synchronization_state_desc,CAST(NULL AS nvarchar(60)) synchronization_health_desc,
CAST(NULL AS decimal(18,2)) log_send_queue_mb,CAST(NULL AS decimal(18,2)) redo_queue_mb,CAST(NULL AS datetime) last_commit_time;
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

Invoke-CollectionModule '复制配置' 'replication' @"
IF OBJECT_ID('msdb.dbo.MSpublications') IS NOT NULL
BEGIN
 SELECT name publication_name,description,type_desc,status_desc,
  CASE is_enabled_for_internet WHEN 1 THEN 'YES' ELSE 'NO' END internet_enabled
 FROM msdb.dbo.MSpublications ORDER BY name;
 SELECT publisher,publisher_db,publication,publication_type,
  CASE independent_agent WHEN 1 THEN 'YES' ELSE 'NO' END independent_agent,
  subscriber_server,subscriber_db,subscription_type,subscription_streams
 FROM msdb.dbo.MSsubscriptions ORDER BY subscriber_server;
END
ELSE SELECT TOP (0) CAST(NULL AS nvarchar(128)) publication_name,CAST(NULL AS nvarchar(256)) description,
 CAST(NULL AS nvarchar(60)) type_desc,CAST(NULL AS nvarchar(60)) status_desc,CAST(NULL AS varchar(3)) internet_enabled;
"@

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
SET NOCOUNT ON;
DECLARE @log table(LogDate datetime,ProcessInfo nvarchar(100),[Text] nvarchar(max));
INSERT @log EXEC master.dbo.xp_readerrorlog 0,1;
SELECT TOP (50) CONVERT(date,LogDate) event_date,
 TRY_CONVERT(int,SUBSTRING([Text],NULLIF(CHARINDEX('Severity:',[Text]),0)+9,3)) severity,
 COUNT(*) event_count,LEFT(MIN([Text]),1000) sample_message
FROM @log
WHERE [Text] LIKE N'%Error%' AND LogDate>=DATEADD(DAY,-7,GETDATE())
GROUP BY CONVERT(date,LogDate),TRY_CONVERT(int,SUBSTRING([Text],NULLIF(CHARINDEX('Severity:',[Text]),0)+9,3))
ORDER BY event_date DESC,event_count DESC;
"@

function Get-PerformancePoint {
    $query = @"
SELECT GETDATE() timestamp,
 MAX(CASE WHEN counter_name='Batch Requests/sec' THEN cntr_value END) batch_requests_raw,
 MAX(CASE WHEN counter_name='Transactions/sec' AND instance_name='_Total' THEN cntr_value END) transactions_raw,
 MAX(CASE WHEN counter_name='Page life expectancy' AND object_name LIKE '%Buffer Manager%' THEN cntr_value END) page_life_expectancy,
 MAX(CASE WHEN counter_name='Memory Grants Pending' THEN cntr_value END) memory_grants_pending,
 MAX(CASE WHEN counter_name='User Connections' THEN cntr_value END) user_connections,
 (SELECT COUNT(*) FROM sys.dm_exec_requests WHERE blocking_session_id<>0) blocked_session_count
FROM sys.dm_os_performance_counters
WHERE counter_name IN ('Batch Requests/sec','Transactions/sec','Page life expectancy','Memory Grants Pending','User Connections');
"@
    $r=@(Invoke-InspectionQuery $query); if ($r.Count) { return $r[0] }; return $null
}

$samples = @(); $previous=$null
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
    [void]$moduleStatus.Add([ordered]@{name='性能采样';status='success';row_count=$samples.Count;elapsed_ms=0;error=$null})
} catch {
    $snapshot.performance_samples=$samples; [void]$limitations.Add("性能采样: $($_.Exception.Message)")
    [void]$moduleStatus.Add([ordered]@{name='性能采样';status='failed';row_count=$samples.Count;elapsed_ms=0;error=$_.Exception.Message})
}

if ($ExcludeSqlText) {
    foreach ($key in @('blocking','active_requests','top_queries','top_queries_logical_reads','top_queries_executions','top_queries_physical_reads')) {
        foreach ($row in $snapshot[$key]) { if ($row.PSObject.Properties['sql_text']) { $row.sql_text='[excluded]' } }
    }
}

$snapshot.collection.finished_at=(Get-Date).ToString('o')
$jsonPath=Join-Path $packageRoot 'snapshot.json'
$snapshot | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $jsonPath -Encoding UTF8
$manifest=[ordered]@{schema_version='1.0';collector_version=$collectorVersion;created_at=$snapshot.collection.finished_at;files=@('snapshot.json','collector.log')}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $packageRoot 'manifest.json') -Encoding UTF8
Write-CollectorLog "Snapshot written: $jsonPath"

# --- Markdown Health Report ---
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
Add-MdLine "**实例**: $($inst.server_name)  |  **版本**: $($inst.product_version) $($inst.product_level)  |  **Edition**: $($inst.edition)"
if ($inst.physical_memory_mb) { Add-MdLine "**内存**: $([math]::Round($inst.physical_memory_mb/1024)) GB  |  **CPU**: $($inst.cpu_count) cores ($($inst.socket_count) socket x $($inst.cores_per_socket))  |  **启动时间**: $($inst.sqlserver_start_time)" }
Add-MdLine

# 1. Collection Status
Add-MdH2 "采集状态摘要"
$passCount = @($moduleStatus | Where-Object {$_.status -eq 'success'}).Count
$failCount = @($moduleStatus | Where-Object {$_.status -eq 'failed'}).Count
Add-MdLine "**模块**: 成功 $passCount / 失败 $failCount / 总计 $($moduleStatus.Count)"
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
    Add-MdLine "| 挂载点 | 卷名 | 文件系统 | 总量(MB) | 可用(MB) | 空闲 |"
    Add-MdLine "|--------|------|----------|----------|----------|------|"
    foreach ($v in $vol) {
        $state = if ($v.free_pct -lt 10) { "**`$($v.free_pct)%**" } else { "$($v.free_pct)%" }
        Add-MdLine "| $($v.volume_mount_point) | $($v.logical_volume_name) | $($v.file_system_type) | $($v.total_mb) | $($v.available_mb) | $state |"
    }
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
    Add-MdLine "| 日期 | Severity | 次数 | 示例 |"
    Add-MdLine "|------|----------|------|------|"
    [int]$eli = 0; foreach ($e in $el) { if ($eli -ge 15) { break }; Add-MdLine "| $($e.event_date) | $($e.severity) | $($e.event_count) | $(([string]$e.sample_message).Substring(0,[Math]::Min(80,([string]$e.sample_message).Length))) |"; $eli++ }
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



