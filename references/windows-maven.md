# Windows/Maven 运行、排错和分享

## 调用

脚本依赖 PowerShell 7 与本机已有 JDK/Maven；报告读取器只依赖 Python 3 标准库，无需额外 pip 安装。

从允许执行的非登录 PowerShell 7 运行技能目录中的脚本。ProjectPath 必填；JavaHome/MavenHome 可显式指定，否则读取 JAVA_HOME、MAVEN_HOME/M2_HOME，最后从 PATH 定位；只读取 mvn 路径，**不执行 mvn.cmd，也不增加 cmd.exe /c shell 层**。

```powershell
$skillDir = '当前技能的绝对目录'
$projectDir = '当前项目的绝对目录'
& (Join-Path $skillDir 'scripts/Invoke-BoundedMaven.ps1') -ProjectPath $projectDir -Goal Version
& (Join-Path $skillDir 'scripts/Invoke-BoundedMaven.ps1') -ProjectPath $projectDir -Goal Test
& (Join-Path $skillDir 'scripts/Invoke-BoundedMaven.ps1') -ProjectPath $projectDir -Goal Pit
& (Join-Path $skillDir 'scripts/Invoke-BoundedMaven.ps1') -ProjectPath $projectDir -Goal Test
python (Join-Path $skillDir 'scripts/read_coverage.py') --project $projectDir
```

在工具提供 shell 选择器时用 shell=pwsh、非登录模式。不要用 Windows PowerShell 5.1、cmd、Bash 或 WSL 编排。Maven 插件内部可能使用自己的进程启动机制，这与额外嵌套一个命令 shell 来编排工具不同。

本机 PATH 的 java 与 JAVA_HOME 可能不一致；使用 Version 的 stdout.log 检查真实 Java、Maven 版本。项目需要 JDK 21 而 PATH 指向 17 时，选已有 JDK 21 并传 JavaHome，不改 compiler.source/target。

## 参数及退出状态

- Goal Test 默认 120 秒，Pit 默认 180 秒，可用 TimeoutSeconds 有界调整，最大 600 秒。到期退出码 124，只终止本次 Java/Maven 进程树。不要通过全局进程名杀掉用户其他 Java 或 IDEA 进程。
- LogDirectory 可指定独立日志目录；默认位于系统临时目录的唯一运行目录。输出 JSON 和 result.json 包含期限、退出码、源码检查、日志位置。不要放到技能安装目录或共享压缩包内。
- 完整 stdout/stderr 分开存储；定位日志内容用 srg。退出码 0 不代替用例数量、跳过数和报告的检查。
- Maven 运行前后检查 src/main 的全部文件和 pom.xml。若这些文件变化，会报告 protected_changes，原成功退出码改为 3；不会自动回滚外部修改。这个检查不代替编辑测试前对其他配置和完整修改范围的人工核对。
- TestFilter 只用于定位原始唯一测试类内的方法，如 `CreditCardValidatorTest#americanExpressLengthBoundaries`；不得为过滤或定位另建测试类，最终完整验收不得带过滤。
- 如果存在 MAVEN_OPTS、MAVEN_ARGS 或 .mvn/jvm.config，脚本会要求先核对；通过 JvmArgument/MavenOption 参数数组提供等效启动设置，并指定 LauncherConfigReviewed。不要清空全局环境，也不要默默忽略原有选项。.mvn/maven.config 由 Maven 本身读取，同样需在预检时核对。
- MavenOption 是经核对的 CLI 设置通道，不能用来跳过测试、缩小最终生产范围、替换变异算子或加入无关目标。
- script 语法错误、环境缺失或前置检查失败时不一定生成 result.json，应根据实际错误更新项目原有说明，修复环境选择后再运行。

## 中文路径与 JaCoCo

只有确认 POM 已配置代理，而 test 输出“missing execution data”或数据落到乱码路径时，才使用下述修复：

1. 从已有的 prepare-agent 日志定位匹配当前插件版本的 runtime agent JAR；不写死原作者 Maven 仓库或版本。
2. 传入 JacocoAgentPath。脚本临时把代理输出改为 ASCII 相对路径 target/jacoco.exec，不修改 POM。
3. 如果 POM 中有 argLine，先解析并通过 ExistingTestArgLine 保留其必要设置；不能覆盖调试、内存或其他代理参数，也不能重复挂载同一 JaCoCo 代理。脚本检测到现有 argLine 而未显式保留时会停止。
4. 修复后检查实际 exec 文件及新报告时间，而不只是 BUILD SUCCESS。
5. 不擅自修改系统编码、项目路径、全局 JDK/Maven 设置或构建配置。

```powershell
& (Join-Path $skillDir 'scripts/Invoke-BoundedMaven.ps1') -ProjectPath $projectDir -Goal Test -JavaHome '已有匹配JDK目录' -MavenHome '已有Maven目录' -JacocoAgentPath '现有匹配版本的runtime-agent.jar绝对路径'
```

## 读取报告

`read_coverage.py` 只读 XML/HTML，不修改说明、不生成新的 Markdown、不运行 Maven。它输出：

- JaCoCo 原始计数、缺失行/分支；
- PIT 总览、状态分布和存活变异的类文件、方法及源码行；
- 测试与生产源文件相对报告时间的新鲜度；
- 手工 IDEA 目录的 JaCoCo 格式 HTML 总览；如果 target/classes 可匹配，另提供生产类范围。格式不支持或范围未知时如实返回警告。

脚本不会自动把存活变异判为等价，也不把未覆盖行判为不可达。IDEA 非 JaCoCo 格式需人工按真实表头核对。修改过的源码比报告新、代码版本相同但运行会话不明等情况，还需要调用者核实；时间戳只是证据之一。

## 分享与安装

共享 ZIP 的根目录是 java-maven-test-coverage，内部有 SKILL.md、agents、scripts、references 和 assets；不包含用户目录、原项目测试答案、Maven 缓存、日志或历史项目报告。

接收者先解压，然后把**整个** java-maven-test-coverage 文件夹放进自己 Codex 实际发现的用户技能目录。本机环境使用 CODEX_HOME/skills；当前官方文档列出的标准用户目录是用户主目录下的 .agents/skills。不要只拷贝 SKILL.md，也不要把同一技能重复复制到多个扫描目录。

调用示例：

```text
使用 $java-maven-test-coverage 测试这个 Maven 项目，只在原项目已有的唯一 *Test.java 测试类内按注释分区，保持原文件名和类名，禁止新增或拆分测试类。
```

Codex 会自动检测技能变化；若列表未刷新，再重启 Codex。参考 [官方技能说明](https://learn.chatgpt.com/docs/build-skills)。
