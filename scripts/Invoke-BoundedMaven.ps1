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
if ($JacocoAgentPath -and $Goal -ne 'Test') { throw 'JacocoAgentPath is only supported for Goal Test.' }

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
function Get-ProtectedSnapshot {
    $files = @(Get-Item -LiteralPath $pomPath)
    $production = Join-Path $project 'src/main'
    if (Test-Path -LiteralPath $production) { $files += Get-ChildItem -LiteralPath $production -File -Recurse -Force }
    $snapshot = @{}
    foreach ($file in $files) { $snapshot[$file.FullName] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash }
    return $snapshot
}
$before = Get-ProtectedSnapshot
if (-not $LogDirectory) {
    $LogDirectory = Join-Path ([IO.Path]::GetTempPath()) ('codex-java-tests/' + [Guid]::NewGuid().ToString('N'))
}
[void](New-Item -ItemType Directory -Path $LogDirectory -Force)
$logRoot = (Resolve-Path -LiteralPath $LogDirectory).Path
$stdoutPath = Join-Path $logRoot 'stdout.log'
$stderrPath = Join-Path $logRoot 'stderr.log'
$resultPath = Join-Path $logRoot 'result.json'
$arguments = @('-Dfile.encoding=UTF-8', '-Dsun.stdout.encoding=UTF-8', '-Dsun.stderr.encoding=UTF-8')
$arguments += $JvmArgument
$arguments += @('-classpath', $bootJar[0].FullName,
    ('-Dclassworlds.conf=' + $mavenConf), ('-Dmaven.home=' + $mavenRoot),
    ('-Dmaven.multiModuleProjectDirectory=' + $project),
    'org.codehaus.plexus.classworlds.launcher.Launcher', '-B', '--no-transfer-progress')
$arguments += $MavenOption
switch ($Goal) {
    'Test' {
        $arguments += 'test'
        if ($TestFilter) { $arguments += '-Dtest=' + $TestFilter }
        if ($JacocoAgentPath) { $arguments += '-DargLine=' + $relativeAgent }
    }
    'Pit' { $arguments += @('clean', 'test-compile', 'org.pitest:pitest-maven:mutationCoverage') }
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
try {
    [void]$process.Start()
    $started = $true
    $ownedPid = $process.Id
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
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
    [IO.File]::WriteAllText($stdoutPath, $(if ($stdout.IsCompletedSuccessfully) { $stdout.Result } else { 'Output drain incomplete.' }), $utf8)
    [IO.File]::WriteAllText($stderrPath, $(if ($stderr.IsCompletedSuccessfully) { $stderr.Result } else { 'Output drain incomplete.' }), $utf8)
    $exitCode = if ($expired) { 124 } else { $process.ExitCode }
    $after = Get-ProtectedSnapshot
    $protectedChanges = @(@($before.Keys) + @($after.Keys) | Sort-Object -Unique | Where-Object { $before[$_] -ne $after[$_] })
    if ($protectedChanges.Count -gt 0 -and $exitCode -eq 0) { $exitCode = 3 }
    $result = [ordered]@{
        project = $project; goal = $Goal; test_filter = $TestFilter
        started_at_utc = $startedAt.ToString('o'); finished_at_utc = [DateTimeOffset]::UtcNow.ToString('o')
        pid = $ownedPid; timeout_seconds = $TimeoutSeconds; timed_out = $expired
        elapsed_seconds = [Math]::Round($watch.Elapsed.TotalSeconds, 2); exit_code = $exitCode
        output_complete = $drained; protected_files_unchanged = ($protectedChanges.Count -eq 0)
        protected_changes = $protectedChanges; stdout_log = $stdoutPath; stderr_log = $stderrPath
    }
    $json = $result | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText($resultPath, $json, $utf8)
    Write-Output $json
} finally {
    if ($started -and -not $process.HasExited) { $process.Kill($true) }
    $process.Dispose()
}
exit $exitCode
