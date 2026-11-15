# Windows/Maven 运行与报告

## 快速接手

PowerShell 7 非登录模式运行。运行器优先使用显式指定的 JavaHome 和 MavenHome，其次读取本机环境。PATH 的 java 与 JAVA_HOME 可能不同。启动错误时用 Goal Version 的 stdout.log 核对真实版本。MavenHome 接受安装根目录或 bin，运行器直接用 Java 调用 Maven。

```powershell
$runner = Join-Path $skillDir 'scripts/Invoke-BoundedMaven.ps1'
$reader = Join-Path $skillDir 'scripts/read_coverage.py'
python $reader --project $projectDir --top 12
python $reader --project $projectDir --top 12 --rank-by line
python $reader --project $projectDir --focus ExamGradingService
& $runner -ProjectPath $projectDir -Goal Test -FreshJacoco -LogDirectory $testLog
$pitDeadline = [DateTimeOffset]::UtcNow.AddMinutes(10)
& $runner -ProjectPath $projectDir -Goal Pit -PitDeadlineUtc $pitDeadline -LogDirectory $pitLog -PitReportsDirectory $pitNew
python $reader --project $projectDir --pit-report $pitNew --top 12
```

示例中的目录和时间预算由调用者按本机环境设置。FreshJacoco 适用于已确认使用 target/jacoco.exec 的项目。PitReportsDirectory 是本轮专用空目录。若 POM 写死 reportsDirectory，先核实 Maven 实际写入的地方。用户需要标准打包路径时，在运行成功、版本相符并核对完整报告后再放入目标目录。

## 控制运行与保全证据

- Test 默认 120 秒。本轮 PIT 的总预算最多 600 秒，从准备报告前开始计算。设置 PitDeadlineUtc，并与用户总截止时间取较早者。所有尝试沿用同一个截止时间，预计剩余时间不够时先交已有验证结果。
- PIT 运行器取 PitDeadlineUtc 与自身 TimeoutSeconds 的较早截止点，提前为终止和日志收尾留出时间。到点退出码为 124，只终止本次进程树。检查 result.json 的 pit_deadline_utc、退出码和完整报告，再引用 PIT 结果。
- 设置独立的 LogDirectory 后，可在运行时查看 stdout.log 与 stderr.log，并用 srg 搜索日志。完成后读取 result.json 的退出码、耗时、output_complete 和文件变化。受保护文件或测试源码中途发生变化时，即使 Maven 成功也返回 3。
- Test、PIT 和 IDEA 编译共用构建目录时串行运行。并行验证使用下节的独立快照。
- 完整 PIT 常规执行 test-compile 和 mutationCoverage。Clean 只在残留 .class、工具链切换等实际需要时启用。它删除整个构建目录。启用前检查 POM 是否使用自定义输出位置，保存所需的完整报告。
- FreshJacoco 把已有 target/jacoco.exec 移入日志目录，再执行 test。核对新执行文件的时间和 XML 生成情况。过滤运行产生的报告属于目标分区，交付全量时运行无过滤验证。
- TestFilter 示例为 StudentTest#testPricingBoundaries。一般补一批用例后直接执行完整 test。某个分区失败时才缩小验证范围。PIT 的 TestCompile 阶段只做编译。
- MavenOption 可传入经核对的 CLI 设置。实测有益时可设置 PIT 线程数，保留目标类和变异算子。若 MAVEN_OPTS、MAVEN_ARGS 或 .mvn/jvm.config 已配置，核对并通过 JvmArgument/MavenOption 传递等效设置，再指定 LauncherConfigReviewed。.mvn/maven.config 由 Maven 读取。

<a id="parallel-pit"></a>

## PIT 与覆盖率改进并行

已有稳定测试版本且需要 PIT 数据时，启动 PIT 后继续处理报告上的可达缺口。只读查看现成的 IDEA 或 JaCoCo 报告可以直接并行。若要修改测试并运行 JUnit，把当前版本复制到独立构建目录供 PIT 使用，原目录继续维护下一版。两边各用自己的 target、覆盖执行文件和报告路径。

单模块项目可按下面的方式准备 PIT 快照。多模块或引用相邻文件的项目应保留整个构建布局，并核对 POM、配置文件和测试资源中实际写入的路径。复制时选择构建输入，跳过已有的 target 和报告目录。testRelative 是前面已锁定的原始测试文件相对路径。

```powershell
$pitDeadline = [DateTimeOffset]::UtcNow.AddMinutes(10)
$snapshot = Join-Path ([IO.Path]::GetTempPath()) ('coverage-pit-' + [Guid]::NewGuid().ToString('n'))
[void](New-Item -ItemType Directory -Path $snapshot)
foreach ($inputPath in @('pom.xml', 'src', '.mvn')) {
    $item = Join-Path $projectDir $inputPath
    if (Test-Path -LiteralPath $item) {
        Copy-Item -LiteralPath $item -Destination $snapshot -Recurse
    }
}
$testedHash = (Get-FileHash -LiteralPath (Join-Path $snapshot $testRelative)).Hash
$currentHash = (Get-FileHash -LiteralPath (Join-Path $projectDir $testRelative)).Hash
if ($currentHash -ne $testedHash) { throw 'Test changed during snapshot creation' }
```

在一个 PowerShell 7 会话中用限时运行器执行快照的 PIT，并记录上述 SHA-256。另一会话用报告读取器按分支排序，定向读源码，在原测试文件中补用例，并用原项目独立的 target 跑短回归或全量 JUnit。需要 fresh JaCoCo 且使用标准执行数据路径时，沿用 FreshJacoco。

```powershell
& $runner -ProjectPath $snapshot -Goal Pit -PitDeadlineUtc $pitDeadline -LogDirectory $pitLog
```

快照报告用快照路径核验，工作目录报告用工作目录核验。PIT 可能在工作目录的新测试写完之后才结束，此时报告时间较新也仍对应旧哈希。可以用该报告指导下一轮，最终提交的 PIT 指标以该提交版本实际运行的结果为准。待验收版本变化时，先看本轮 PIT 剩余预算，再决定是否对最终版本补完整验证。截止前先交已通过的测试及覆盖率，标明已有 PIT 对应的版本。

## 中文路径与 JaCoCo

只有 POM 已配置代理且日志确认为 missing execution data 或执行数据路径乱码时，使用 JacocoAgentPath。先从日志或本机 Maven 缓存定位对应版本的 runtime agent JAR。脚本临时使用相对路径 target/jacoco.exec。POM 已有 argLine 时，先保留其余 JVM 选项和其他所需代理，避免同一 JaCoCo 代理重复挂载，再通过 ExistingTestArgLine 提供。运行后以真实执行数据和 XML 验证。

## 报告读取

报告读取器只读，依赖 Python 3.9 以上版本的标准库。

- --top N 输出精简排行，优先使用 --rank-by branch 或 --rank-by line 指定当前目标。--focus 类名可展开源码行和 PIT 变异。需要完整归档时直接把全量 JSON 写入临时文件。
- IDEA 原生 HTML 按真实表头读取类、方法、分支、行的计数。类页的红色和部分覆盖标记用于定位，可用分支数取自表格。核对 scope 和是否混用会话，再将总览用于交付。JaCoCo 格式的人工导出沿用已编译生产类映射。
- Maven JaCoCo XML 提供生产代码计数和位置。Surefire XML 提供实际通过、失败、错误和跳过，结合 result.json 核对运行是否带过滤。
- PIT 优先读 XML，取得实际算子、字节码索引与状态。--pit-report 可指定暂存报告目录或 XML。只有 HTML 时读取它的汇总和明细。KILLED、TIMED_OUT、SURVIVED 与 NO_COVERAGE 用原始计数。报告缺失、仍在生成或分母冲突时保留警告。
- 时间戳用于发现可能的旧报告。把测试版本、构建退出码及报告范围一同复核。IDEA 是交付要求时，请用户对最终测试类运行 Run with Coverage 并导出。
