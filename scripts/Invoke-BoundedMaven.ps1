<#
.SYNOPSIS
以本机 JDK 启动 Maven，限时执行回归测试、PIT 或环境检查。
.DESCRIPTION
实时写入进程日志并验证构建输入的文件指纹。TimeoutSeconds 到期时仅终止本次进程树。FreshJacoco 用于执行数据输出为 target/jacoco.exec 的项目。
#>
#Requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ProjectPath,
    [ValidateSet('Test', 'Pit', 'Version')][string]$Goal = 'Test',
    [string]$JavaHome,
    [string]$MavenHome,
    [ValidateRange(0, 600)][int]$TimeoutSeconds = 0,
    [string]$LogDirectory,
    [string]$TestFilter,
    [string]$JacocoAgentPath,
    [string]$ExistingTestArgLine,
    [switch]$Clean,
    [switch]$FreshJacoco,
    [string]$PitReportsDirectory,
    [string[]]$JvmArgument = @(),
    [string[]]$MavenOption = @(),
    [switch]$LauncherConfigReviewed
)
$ErrorActionPreference = 'Stop'
$project = (Resolve-Path -LiteralPath $ProjectPath).Path
$pomPath = Join-Path $project 'pom.xml'
if (-not (Test-Path -LiteralPath $pomPath -PathType Leaf)) { throw 'ProjectPath must contain pom.xml.' }
if ($TimeoutSeconds -eq 0) { $TimeoutSeconds = if ($Goal -eq 'Pit') { 180 } else { 120 } }
if ($TestFilter -and $Goal -ne 'Test') { throw 'TestFilter is only supported for Goal Test.' }
if (($JacocoAgentPath -or $FreshJacoco) -and $Goal -ne 'Test') {
    throw 'JacocoAgentPath and FreshJacoco are only supported for Goal Test.'
}
if ($PitReportsDirectory -and $Goal -ne 'Pit') { throw 'PitReportsDirectory is only supported for Goal Pit.' }
if ($Clean -and $Goal -eq 'Version') { throw 'Clean requires Test or Pit.' }
if ($Clean -and $FreshJacoco) { throw 'Choose Clean or FreshJacoco. Clean removes the entire target directory.' }

# Do not silently ignore options normally supplied by mvn.cmd / mvn.
$launcherConfig = @('MAVEN_OPTS', 'MAVEN_ARGS') | Where-Object {
    -not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($_))
}
if (Test-Path -LiteralPath (Join-Path $project '.mvn/jvm.config')) { $launcherConfig += '.mvn/jvm.config' }
if ($launcherConfig.Count -gt 0 -and -not $LauncherConfigReviewed) {
    throw ('Review launcher configuration and pass resolved JvmArgument/MavenOption arrays with LauncherConfigReviewed: ' + ($launcherConfig -join ', '))
}
if (-not $JavaHome) {
    $JavaHome = $env:JAVA_HOME
    if (-not $JavaHome) {
        $javaCommand = Get-Command java -CommandType Application -ErrorAction Stop
        $JavaHome = Split-Path (Split-Path $javaCommand.Source -Parent) -Parent
    }
}
$javaName = if ($IsWindows) { 'java.exe' } else { 'java' }
$java = (Resolve-Path -LiteralPath (Join-Path $JavaHome ('bin/' + $javaName))).Path
$resolvedJavaHome = Split-Path (Split-Path $java -Parent) -Parent
if (-not $MavenHome) {
    $MavenHome = $env:MAVEN_HOME
    if (-not $MavenHome) { $MavenHome = $env:M2_HOME }
    if (-not $MavenHome) {
        $mavenCommand = Get-Command mvn -CommandType Application -ErrorAction Stop
        $MavenHome = Split-Path (Split-Path $mavenCommand.Source -Parent) -Parent
    }
}
$mavenRoot = (Resolve-Path -LiteralPath $MavenHome).Path
if ((Split-Path $mavenRoot -Leaf) -eq 'bin' -and (Test-Path -LiteralPath (Join-Path $mavenRoot 'm2.conf'))) {
    $mavenRoot = Split-Path $mavenRoot -Parent
}
$bootJar = @(Get-ChildItem -LiteralPath (Join-Path $mavenRoot 'boot') -Filter 'plexus-classworlds-*.jar' -File)
$mavenConf = Join-Path $mavenRoot 'bin/m2.conf'
if ($bootJar.Count -ne 1 -or -not (Test-Path -LiteralPath $mavenConf)) {
    throw 'MavenHome must be a Maven distribution with exactly one plexus-classworlds boot JAR and bin/m2.conf.'
}
if ($JacocoAgentPath) {
    $settings = [Xml.XmlReaderSettings]::new()
    $settings.DtdProcessing = [Xml.DtdProcessing]::Prohibit
    $reader = [Xml.XmlReader]::Create($pomPath, $settings)
    try { $pom = [Xml.XmlDocument]::new(); $pom.Load($reader) } finally { $reader.Dispose() }
    if (-not $pom.SelectSingleNode("//*[local-name()='plugin'][*[local-name()='artifactId']='jacoco-maven-plugin']")) {
        throw 'Relative JaCoCo output is only supported when this POM already declares the JaCoCo plugin.'
    }
    $existing = $pom.SelectNodes("//*[local-name()='argLine']")
    if ($existing.Count -gt 0 -and [string]::IsNullOrWhiteSpace($ExistingTestArgLine)) {
        throw 'Existing argLine found. Resolve and preserve it using ExistingTestArgLine before applying the relative-output workaround.'
    }
    $agent = (Resolve-Path -LiteralPath $JacocoAgentPath).Path
    if ($agent.Contains('"')) { throw 'Unsupported quote in JaCoCo agent path.' }
    $relativeAgent = '"-javaagent:' + $agent + '=destfile=target/jacoco.exec"'
    if ($ExistingTestArgLine) { $relativeAgent += ' ' + $ExistingTestArgLine }
}
# 以文件绝对路径为键建立 SHA-256 快照。
function Get-FileSnapshot($files) {
    $snapshot = @{}
    foreach ($file in $files) {
        $snapshot[$file.FullName] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    }
    return $snapshot
}

# 记录 POM 和 src/main 的指纹以核对受保护文件。
function Get-ProtectedSnapshot {
    $files = @(Get-Item -LiteralPath $pomPath)
    $production = Join-Path $project 'src/main'
    if (Test-Path -LiteralPath $production) { $files += Get-ChildItem -LiteralPath $production -File -Recurse -Force }
    return Get-FileSnapshot $files
}

# 记录 src/test 下 Java 源码的指纹以确认报告对应版本。
function Get-TestSnapshot {
    $testRoot = Join-Path $project 'src/test'
    if (Test-Path -LiteralPath $testRoot) {
        return Get-FileSnapshot (Get-ChildItem -LiteralPath $testRoot -File -Recurse -Filter '*.java')
    }
    return @{}
}

# 比较两组指纹，返回新增、删除或内容变化的文件。
function Get-SnapshotChanges($initial, $current) {
    @($initial.Keys) + @($current.Keys) |
        Sort-Object -Unique |
        Where-Object { $initial[$_] -ne $current[$_] }
}

$before = Get-ProtectedSnapshot
$testBefore = Get-TestSnapshot
if (-not $LogDirectory) {
    $LogDirectory = Join-Path ([IO.Path]::GetTempPath()) ('codex-java-tests/' + [Guid]::NewGuid().ToString('N'))
}
[void](New-Item -ItemType Directory -Path $LogDirectory -Force)
$logRoot = (Resolve-Path -LiteralPath $LogDirectory).Path
$stdoutPath = Join-Path $logRoot 'stdout.log'
$stderrPath = Join-Path $logRoot 'stderr.log'
$resultPath = Join-Path $logRoot 'result.json'
if (Test-Path -LiteralPath $resultPath) {
    throw 'LogDirectory already has a result.json. Use a separate directory for this run.'
}

if ($PitReportsDirectory) {
    [void](New-Item -ItemType Directory -Path $PitReportsDirectory -Force)
    $PitReportsDirectory = (Resolve-Path -LiteralPath $PitReportsDirectory).Path
    if (Get-ChildItem -LiteralPath $PitReportsDirectory -Force | Select-Object -First 1) {
        throw 'PitReportsDirectory must be empty. Use a separate directory for this run.'
    }
}

$previousJacoco = $null
$buildOutput = Join-Path $project 'target'
if (($Clean -or $FreshJacoco) -and (Test-Path -LiteralPath $buildOutput)) {
    $targetItem = Get-Item -LiteralPath $buildOutput
    if ($targetItem.FullName -ne $buildOutput -or -not $targetItem.PSIsContainer -or
        ($targetItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Unexpected target directory. Inspect the build output before cleaning.'
    }
}
if ($FreshJacoco) {
    $jacocoData = Join-Path $buildOutput 'jacoco.exec'
    if (Test-Path -LiteralPath $jacocoData -PathType Leaf) {
        $oldData = Get-Item -LiteralPath $jacocoData
        if ($oldData.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'JaCoCo output is a reparse point. Inspect the execution file.'
        }
        $previousJacoco = Join-Path $logRoot ('previous-jacoco-' + [Guid]::NewGuid().ToString('N') + '.exec')
        Move-Item -LiteralPath $jacocoData -Destination $previousJacoco
    }
}

$arguments = @('-Dfile.encoding=UTF-8', '-Dsun.stdout.encoding=UTF-8', '-Dsun.stderr.encoding=UTF-8')
$arguments += $JvmArgument
$arguments += @('-classpath', $bootJar[0].FullName,
    ('-Dclassworlds.conf=' + $mavenConf), ('-Dmaven.home=' + $mavenRoot),
    ('-Dmaven.multiModuleProjectDirectory=' + $project),
    'org.codehaus.plexus.classworlds.launcher.Launcher', '-B', '--no-transfer-progress')
$arguments += $MavenOption
if ($Clean) { $arguments += 'clean' }
switch ($Goal) {
    'Test' {
        $arguments += 'test'
        if ($TestFilter) { $arguments += '-Dtest=' + $TestFilter }
        if ($JacocoAgentPath) { $arguments += '-DargLine=' + $relativeAgent }
    }
    'Pit' {
        if ($PitReportsDirectory) { $arguments += '-DreportsDirectory=' + $PitReportsDirectory }
        $arguments += @('test-compile', 'org.pitest:pitest-maven:mutationCoverage')
    }
    'Version' { $arguments += '--version' }
}
$info = [Diagnostics.ProcessStartInfo]::new()
$info.FileName = $java
$info.WorkingDirectory = $project
$info.UseShellExecute = $false
$info.CreateNoWindow = $true
$info.RedirectStandardOutput = $true
$info.RedirectStandardError = $true
$info.StandardOutputEncoding = [Text.UTF8Encoding]::new($false)
$info.StandardErrorEncoding = [Text.UTF8Encoding]::new($false)
$info.Environment['JAVA_HOME'] = $resolvedJavaHome
$info.Environment['PATH'] = (Join-Path $resolvedJavaHome 'bin') + [IO.Path]::PathSeparator + $env:PATH
foreach ($argument in $arguments) { $info.ArgumentList.Add($argument) }
$process = [Diagnostics.Process]::new()
$process.StartInfo = $info
$started = $false
$expired = $false
$exitCode = 1
$startedAt = [DateTimeOffset]::UtcNow
$watch = [Diagnostics.Stopwatch]::StartNew()
$stdoutFile = $null
$stderrFile = $null
try {
    $stdoutFile = [IO.FileStream]::new($stdoutPath, 'Create', 'Write', 'ReadWrite', 1, $true)
    $stderrFile = [IO.FileStream]::new($stderrPath, 'Create', 'Write', 'ReadWrite', 1, $true)
    [void]$process.Start()
    $started = $true
    $ownedPid = $process.Id
    $stdout = $process.StandardOutput.BaseStream.CopyToAsync($stdoutFile)
    $stderr = $process.StandardError.BaseStream.CopyToAsync($stderrFile)
    while (-not $process.WaitForExit(500)) {
        if ($watch.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
            $expired = $true
            $process.Kill($true)
            [void]$process.WaitForExit(5000)
            break
        }
    }
    $drained = [Threading.Tasks.Task]::WaitAll([Threading.Tasks.Task[]]@($stdout, $stderr), 5000)
    $utf8 = [Text.UTF8Encoding]::new($false)
    $exitCode = if ($expired) { 124 } else { $process.ExitCode }
    $protectedChanges = @(Get-SnapshotChanges $before (Get-ProtectedSnapshot))
    $testChanges = @(Get-SnapshotChanges $testBefore (Get-TestSnapshot))
    if (($protectedChanges.Count -gt 0 -or $testChanges.Count -gt 0) -and $exitCode -eq 0) { $exitCode = 3 }
    $result = [ordered]@{
        project = $project; goal = $Goal; test_filter = $TestFilter
        started_at_utc = $startedAt.ToString('o'); finished_at_utc = [DateTimeOffset]::UtcNow.ToString('o')
        pid = $ownedPid; timeout_seconds = $TimeoutSeconds; timed_out = $expired
        elapsed_seconds = [Math]::Round($watch.Elapsed.TotalSeconds, 2); exit_code = $exitCode
        output_complete = $drained; protected_files_unchanged = ($protectedChanges.Count -eq 0)
        protected_changes = $protectedChanges; test_sources_unchanged = ($testChanges.Count -eq 0)
        test_source_changes = $testChanges; clean = [bool]$Clean; previous_jacoco_exec = $previousJacoco
        requested_pit_reports = $PitReportsDirectory; stdout_log = $stdoutPath; stderr_log = $stderrPath
    }
    $json = $result | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText($resultPath, $json, $utf8)
    Write-Output $json
} finally {
    if ($started -and -not $process.HasExited) { $process.Kill($true) }
    $process.Dispose()
    if ($null -ne $stdoutFile) { $stdoutFile.Dispose() }
    if ($null -ne $stderrFile) { $stderrFile.Dispose() }
}
exit $exitCode
