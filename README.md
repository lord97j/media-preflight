# Media Preflight · 多媒体素材预处理

一个给 Codex / Claude 等本地 Agent 用的素材整理 Skill：把一个拍摄目录变成可审阅的 `素材索引.json`，补齐画面描述与标签后，用一条命令批量改名、写 Finder 注释与标签、移动进素材库，并重建各级索引。

面向「拍了一堆素材，想先看清楚再归档」的场景。所有处理在本地完成，脚本只用 Python 标准库，不需要 API 密钥。

> **试试这样说**
>
> 用 `$media-preflight` 处理「待整理素材」目录：先测人声，生成索引，把每条视频的画面描述、标签和归档目标列给我确认。
>
> 确认后再说：按这份索引批量归档到素材库。

## 工作流

```text
待整理素材/
   │
   ├─ 1. voice   逐个视频做 Silero VAD，判定 有人声 / 无人声
   ├─ 2. render  生成 素材索引.json（含标准名、原文路径、建议归档路径）
   ├─ 3. 人工审阅：补 description / evidence / tags / capture_time / archive_type
   ├─ 4. 文本确认：原文件名 → 标准名 → 目标路径
   └─ 5. batch   一次完成改名、注释、标签、移动、索引重建
```

第 5 步只需两个参数：索引文件和素材库路径。

```sh
python3 scripts/archive.py batch "/path/to/inbox/素材索引.json" "/path/to/library"
```

没有 `archive_type` 的条目不会归档，会在输出的 `skipped_without_category` 里列出。索引生成后如果原件大小或修改时间变了，整批中止，不移动任何文件。

## 整理后是什么样

文件名用 `日期_类型_场景_主体动作_序号.原扩展名`。归档时**场景段取归档分类**，原场景并入主体动作：

```text
20261030_视频_碧绿河道_筏队行进_028.MP4          # 整理前
20261030_视频_河谷与竹筏_碧绿河道筏队行进_028.MP4  # 归入「河谷与竹筏」后
```

目录按「类型 / 年-月 / 日 / 分类」分层：

```text
素材库/
├── 视频/
│   └── 2026-10/
│       └── 30/
│           ├── 河谷与竹筏/
│           │   ├── 20261030_视频_河谷与竹筏_碧绿河道筏队行进_028.MP4
│           │   ├── 20261030_视频_河谷与竹筏_碧绿河道筏队行进_028.SRT
│           │   └── 素材索引.json
│           └── 山地风光/
└── 图片/
    └── 2026-10/
        └── 30/
            └── 团队群像/
```

同主名的 `.SRT` / `.LRF` 会跟着主片一起移动并改成新主名。日期缺少可靠依据时归到 `日期未知`，不建日目录。

## 索引里有什么

`素材索引.json` 是递归汇总的：每个目录一份，父目录的 `assets` 合并所有下级素材，`child_indexes` 指向直接子索引。

每条素材的关键字段：

| 字段 | 说明 |
| --- | --- |
| `path` / `filename` | 相对当前索引的路径 / 当前文件名 |
| `standard_name` | 标准名 |
| `description` / `full_description` | 一句话画面摘要 / 逐段描述 |
| `tags` | 来源、画面、剪辑用途，以及 `有人声` / `无人声` |
| `evidence` | 证据：看了哪些帧、哪段原片、哪个旧报告 |
| `capture_time` | 拍摄时间，决定归档到哪一天 |
| `archive_type` | 归档分类；填了才会被 `batch` 归档 |
| `archive_target` | 按规则算出的完整目标路径，供确认 |
| `original_filename` | 归档前文件名（如 `DJI_20261030125833_0046_D.MP4`） |
| `source_path` | 归档前完整路径（新批次自动写入） |
| `companions` | 随主片移动的 SRT / LRF |

归档执行后，源索引会保留为记录：`mode` 变成 `archived`，每条带 `status: archived`、`archived_path`、`archived_filename`，原始文件名与路径字段不变。素材库索引同样保留这些字段。

## 人声标签

视频必须有且只有一个 `有人声` 或 `无人声`。检测用 Silero VAD，只判断有没有人声候选，不转写、不花钱：

```sh
python3 scripts/media.py voice "/path/to/inbox" --python /path/to/venv/bin/python
```

- 需要 `faster-whisper`；`--python` 指向装了它的解释器，脚本会自动切换过去
- 结果按 inode 缓存在 `.media-preflight/voice.json`，原件变了自动重测；`--force` 强制重跑
- 已有豆包批处理产物时加 `--from-batch doubao_vad_batch.json`，按原件大小与修改时间复用
- `无人声` 只代表 VAD 没检出，不等于确认没有语音；歌声、广播、音乐也可能被算作人声候选

## 安装

需要 Python 3.9+。归档功能需要 **macOS**，源与素材库在同一支持扩展属性的卷上。

```sh
git clone https://github.com/lord97j/media-preflight.git "$HOME/.agents/skills/media-preflight"
```

也可放到项目的 `.agents/skills/media-preflight`，或 `~/.codex/skills/`。可选依赖：

| 用途 | 依赖 |
| --- | --- |
| 读取视频时长、生成预览帧 | `ffmpeg` / `ffprobe` |
| `voice` 人声检测 | `faster-whisper` |
| `scripts/finder_comments.py` 同步 Finder 目录记录 | `ds-store`（建议装在独立 venv） |

## 命令行

| 命令 | 作用 |
| --- | --- |
| `media.py scan <dir>` | 盘点，输出待审阅 JSON（含命名/注释问题、人声状态） |
| `media.py render <dir>` | 生成或刷新 `素材索引.json`；`--tree` 自下而上重建整库 |
| `media.py voice <dir>` | 批量人声检测，写 `.media-preflight/voice.json` |
| `media.py plan/apply/rollback` | 仅改名的索引提交，含计划 SHA-256 与回滚 |
| `media.py search <dir> 词...` | 按文件名、标准名、标签、描述检索，可按 `--kind` 过滤 |
| `archive.py batch <index> <library>` | 按索引一次完成改名、注释、标签、移动、索引重建 |
| `archive.py plan/preview/apply` | 需要独立计划文件、逐条预览与 SHA-256 复核时使用 |
| `archive.py recover` | 中断后检查/恢复归档事务 |

常用组合：

```sh
# 索引 + 人工补注释后直接归档
python3 scripts/media.py render "/path/to/inbox"
# ……编辑 /path/to/inbox/素材索引.json……
python3 scripts/archive.py batch "/path/to/inbox/素材索引.json" "/path/to/library"

# 全库刷新索引（不动文件）
python3 scripts/media.py render "/path/to/library" --tree

# 按剪辑意图找镜头
python3 scripts/media.py search "/path/to/library" 河边 逆光 转场 --kind video
```

## 安全与可恢复

- 每条素材记录指纹（设备、inode、大小、修改时间）；索引生成后原件变化会让整批中止
- 归档前统一校验：标准名、目标重名、同卷、配套文件集合、人声标签、描述与证据完整性
- 归档过程写入事务日志，异常自动回滚移动与索引；中断可用 `archive.py recover` 检查
- 同名文件用排他重命名，不会覆盖；Finder 标签是合并而非替换，旧标签保留
- 改名会破坏已有剪辑工程的引用，批量归档前需确认这一点

## 性能

在本机（Apple silicon）用 120 个视频 + 60 个 SRT 实测：`batch` 全流程（改名、注释、标签、移动、重建 28 个索引）约 **0.7 秒**；生成 120 条索引约 **0.2 秒**。

主要来自三处：Finder 扩展属性走 libc 调用而不是 `xattr` 子进程（约 80 倍）、移动按批同步而不是每文件 fsync、索引只重建受影响目录。

## 许可

代码与文档使用 [MIT License](LICENSE)。

命名与批量审阅流程参考了 RenamerX。[使用指南](https://sancijun.com/guides/renamerx-user-guide) · [官网](https://renamerx.com/zh)
