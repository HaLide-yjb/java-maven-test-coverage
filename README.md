# java-maven-test-coverage

面向 Java/Maven 软件测试的 Codex Skill，结合 JUnit、JaCoCo 与 PIT 变异测试，以生产代码类、方法、行、分支覆盖率及变异检出率 100% 为目标。

**只在原项目已有的唯一测试类中编写测试，保持原文件名、包名和类名。** 所有目标类通过注释分区，生产代码与构建配置只读。

## 核心规则

- 锁定原始的一个 `*Test.java` 文件；禁止新增、拆分、重命名测试类，以及通过内部或嵌套测试类绕过限制。
- 公共属性、辅助方法、参数源及执行保护均留在原类的公共区域；各生产类的测试分别放在对应注释分区。
- 保持 `src/main`、`pom.xml`、其他配置和测试资源只读；运行前后核对生产源码及 POM 指纹。
- 用例输入、循环次数和运行时间有明确上限；遇到长循环先停止，移除或替换危险写法，再验证替代方案。
- IDEA 人工导出、Maven JaCoCo 与 PIT 分别核验，保留原始计数及报告时间。
- 持续更新同一份 `测试覆盖率阻碍说明.md`，记录类、方法、行号、课程依据、尝试及证据；不按日期或轮次重复创建说明。

100% 是目标，不是保证。存活变异不自动等价，未覆盖路径不自动不可达；证据不足时保留“尚未解决”。有证明的等价变异和不可达路径也保留在原始统计范围内。

## 测试组织

以下分区全部位于原项目同一个测试类内部：

```java
//=======================公共区域============
// 公共属性、辅助方法、参数源和执行保护。

//=======================AmericanExpressValidator.java============
// 本目标类的测试。

//=======================ChinaTUnionValidator.java============
// 本目标类的测试。
```

生产文件名只是分区标题，不表示需要创建相应的测试类。

## 使用

本仓库根目录就是完整的 Skill 目录。安装或分享时保留 `SKILL.md`、`agents`、`assets`、`references` 和 `scripts`，不要只复制入口文件。安装位置按你的 Codex 环境实际使用的技能目录确定。

调用示例：

```text
使用 $java-maven-test-coverage 测试这个 Maven 项目。
只修改原项目已有的唯一测试类，保持原文件名、包名和类名，在类内按目标类注释分区。
生产代码和构建配置只读；限时运行，以全部覆盖率及 PIT 变异检出率 100% 为目标，
并持续更新项目原有的测试覆盖率阻碍说明.md。
```

对应的 Maven 目标：

```text
mvn test
mvn clean test-compile org.pitest:pitest-maven:mutationCoverage
```

Windows 使用 PowerShell 7。随包运行器通过原生 Java 入口执行 Maven，不额外嵌套 cmd；test 默认限时 120 秒，PIT 默认限时 180 秒。PIT 的 clean 会清除 target，必要时在 PIT 完成后再运行 test，以同时保留两份最新报告。

IDEA 覆盖率需由使用者对原始唯一测试类执行 **Run with Coverage** 并导出 HTML，再交给 Codex 核验。自动生成的 Maven/PIT 报告不能替代这一人工步骤。

## 目录

| 文件 | 用途 |
| --- | --- |
| [SKILL.md](SKILL.md) | 修改范围、单类约束、测试流程与验收要求 |
| [agents/openai.yaml](agents/openai.yaml) | 技能显示信息与调用提示 |
| [references/test-design.md](references/test-design.md) | 五章方法提炼、方法选择及缺口复核 |
| [references/windows-maven.md](references/windows-maven.md) | 环境选择、运行参数、中文路径及报告排错 |
| [references/practice-case.md](references/practice-case.md) | 历史练习案例及结论边界 |
| [assets/blocker-report.template.md](assets/blocker-report.template.md) | 固定维护的阻碍说明模板 |
| [scripts/Invoke-BoundedMaven.ps1](scripts/Invoke-BoundedMaven.ps1) | 限时执行 Maven、终止本次进程树并核对受保护文件 |
| [scripts/read_coverage.py](scripts/read_coverage.py) | 读取覆盖率、变异状态及报告新鲜度 |

脚本使用本机已有的 JDK、Maven、PowerShell 7 和 Python 3；报告读取器仅依赖 Python 标准库。项目需具备对应测试与覆盖率配置，不会通过修改构建文件迁就环境。

## 测试方法

依据《软件测试方法 · 内容概要》五章提炼测试预言、PIE、Bug 特性、等价类、随机与组合、边界、变异、逻辑、控制流和数据流方法。指南包含适用条件、具体动作和证据要求；用户另有学习材料时按相关章节回查原文。
