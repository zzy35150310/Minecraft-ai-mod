# mcgen

把一句中文需求变成能编译通过的 Minecraft Fabric 模组。

```bash
python3 mcgen.py new --req "加一把会爆炸的钻石剑" -o ./boomsword
```

> **状态：开发中。** 当前进度见下方「进度」，请先读完再判断能不能用。

---

## 它解决什么问题

这类工具真正的难点不是"调 LLM 写代码"，而是 **LLM 写出来的 Java 到底能不能编译**。

Fabric 的 API 表面每个版本都在变。LLM 靠记忆写出来的注册代码、类名、方法签名，
绝大多数编译不过——产出一堆看着像模组的废码，跑一次就没人再信了。

mcgen 的判断是：**把"LLM 写整个模组"降级为"LLM 只写方法体"**。

| 内容 | 谁负责 | 理由 |
|---|---|---|
| `build.gradle` / `fabric.mod.json` / 入口类 | **模板** | 纯替换，不能错 |
| 物品/方块的注册调用、`Identifier` 构造 | **模板** | 易错的样板代码 |
| 配方、物品模型等 JSON | **模板** | 结构固定，无推理成分 |
| 物品/方块的**行为** | **LLM** | 唯一真正需要推理的部分 |
| 贴图 PNG | **复用原版素材** | LLM 生成不了图片 |

结构性的东西交给确定性模板保证编译通过；只有"行为"交给 LLM，并且把**从真实 jar
里提取出来的 API 签名**塞进上下文。这是整个工具的核心思路。

### 贴图怎么办？不生成，直接引用原版

模型 json 的 `textures` 指向原版资源即可，**完全不需要产 PNG**：

```json
{ "parent": "minecraft:item/handheld",
  "textures": { "layer0": "minecraft:item/diamond_sword" } }
```

这让"LLM 产不了图"从阻塞问题降级为不存在的需求。

---

## 一个决定性的技术前提

**Minecraft 自 26.1 起不再混淆。** Fabric 已停止提供 Yarn 映射，Loom 1.17+ 默认使用
Mojang 官方映射。官方模板的 `build.gradle` 里**完全没有 `mappings` 配置行**，
代码里直接用真实名 `net.minecraft.resources.Identifier`（1.21 时代是 `ResourceLocation`）。

这一点对本方案是分水岭：jar 里就是可读的真实符号名，**解析常量池就能拿到准确签名**，
不需要 `javap`、不需要 `gradlew genSources`（那要反编译整个 Minecraft，慢且有稳定性问题）。

实测数据（54MB / 24744 个类的 jar）：提取单个类 0.054 秒，全 jar 搜方法名 2.2 秒，
**零依赖、不需要 JDK、不需要联网**。

---

## 进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 环境（JDK 25） | ✅ |
| P1 | 模板渲染 → `gradlew build` 出 jar | ⚠️ 渲染链路完成，**端到端构建尚未跑通**（网络问题，见下） |
| P2 | 需求 → spec（调 LLM） | ✅ 已对真实端点验证 |
| P3 | 生成行为代码 + 编译错误修复循环 | ⬜ 未开始 |
| P4 | API 签名索引 | ✅ 已完成（纯 Python 解析 class 文件） |
| P5 | 内容类型扩展（block/tool/armor/food/recipe…） | ⬜ 未开始，目前只有 `item` |

**P1 是闸门**——在它通过之前，其余都是空中楼阁。

### 已知阻塞

首次构建要从 `maven.fabricmc.net` 拉 Loom 插件与 Fabric API。实测该主机
（Cloudflare）在国内链路上只有 **4–27 KB/s 且会突发停顿**，而 Mojang、Maven Central
都是正常的（412 KB/s / 78 KB/s）。已尝试并排除的绕行方案：

- **BMCLAPI**：只硬编码了 fabric-loader 一个 jar，没有 pom 和元数据，Gradle 用不了
- **aliyun 镜像**：反而更糟，Gradle 把连接堆在它上面反复 `CLOSE_WAIT`，十几分钟零写入
- **固定 Cloudflare 边缘 IP**：一度测到某边缘快 16 倍，复测三次后确认是**幸运采样**，不可靠

现状是把 Gradle 的 socket 超时从默认 30 秒放宽到 300 秒（默认值会把"慢但确实在动"
的连接直接掐断，报 `Read timed out`），然后重试。没有更好的办法。

---

## 安装

需要 **Python 3.10+**（零第三方依赖，纯标准库）和 **JDK 25**。

```bash
brew install openjdk@25
```

JDK 是 keg-only 的，mcgen 内部会自动探测，不用手配 `JAVA_HOME`。

LLM 配置按优先级从命令行、环境变量、`~/.claude/settings.json` 读取：
若你已经在用 Claude Code 且配了 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`，
**开箱即用**，不用重复填。

```bash
# 用现成配置
python3 mcgen.py new --req "加一把会爆炸的钻石剑" -o ./out

# 或显式指定
python3 mcgen.py new --req "..." -o ./out \
    --format anthropic --base-url https://api.deepseek.com/anthropic \
    --model deepseek-chat --api-key sk-xxx
```

同时支持 **Anthropic 兼容**（`/v1/messages`）和 **OpenAI 兼容**
（`/v1/chat/completions`）两种请求格式，用 `--format` 切换。

## 命令

```
python3 mcgen.py new --req "自然语言需求" -o ./out     # 生成（调 LLM）
python3 mcgen.py new --spec spec.json -o ./out         # 从 spec 生成
python3 mcgen.py new --demo -o ./out                   # 内置样例，不调 LLM
python3 mcgen.py check --spec spec.json                # 只校验 spec
```

---

## 代码结构

| 文件 | 职责 |
|---|---|
| `mcgen.py` | CLI 入口、参数解析、流程编排 |
| `mcgen_llm.py` | provider 抽象（anthropic / openai 两分支）、HTTP、重试 |
| `mcgen_spec.py` | 需求 → spec、schema 校验、默认值填充 |
| `mcgen_render.py` | 模板渲染 |
| `mcgen_apis.py` | 从 jar 里提取真实 API 签名 |
| `mcgen_build.py` | gradle 调用、错误解析 |
| `templates/` | 工程骨架模板（真实文件 + `{{占位符}}`） |

模板存**真实文件**而非嵌在 Python 字符串里，便于对着官方模板 diff 更新。

## 开发

```bash
python3 selftest.py
```

74 项纯逻辑自测，**不依赖 JDK、不依赖网络**。覆盖错误解析（含中文 locale 兜底、
依赖下载失败、Java 堆栈滤除）、modid 归一化、spec 校验、LLM provider 逻辑、
重试循环、class 文件解析（用手搓的最小 class 文件做样本）。

改动 `mcgen_build.py` 的错误解析时要格外小心——这段逻辑很脆，
静默失效会返回空串，然后 LLM 拿着空气去修 bug。

## 许可

MIT
