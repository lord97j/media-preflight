# Media Preflight · 多媒体素材预处理

一个给 Codex 用的本地素材整理 Skill。把拍摄目录变成一份可浏览的 `素材索引.md`，再按描述、标签和时间点快速挑出能剪的镜头。需要整理素材库时，可以先审阅完整去向，再批量改名、加标签和归档。

> **试试这样说**
>
> `$media-preflight` 帮我扫描「待整理素材」目录，给每条视频和图片建议标准名、画面描述和标签，先生成索引。
>
> 接着可以说「找横屏的河岸逆光镜头，适合做过场」。

## 两种工作模式

| 模式 | 适用场景 | 产出 |
| --- | --- | --- |
| 索引预览 | 先了解素材，或剪辑工程仍引用原路径 | 在源目录生成 `素材索引.md`。列出已合规与待处理素材、建议名称、原片链接、画面描述、标签和已核验的时间点 |
| 确认归档 | 准备把素材收入长期素材库 | 按审阅过的方案批量改名、写 macOS 文件备注、追加 Finder 标签并移动。更新每个收到素材的分类索引，归档成功后撤下本批源目录生成的索引 |

索引预览只写 Markdown。用户可以在里面核对每条素材的完整归档目标。执行脚本还会比对计划的 SHA-256、源文件指纹和目标目录状态，并拦截重名或配对素材的单独移动。

## 整理后是什么样

默认文件名是 `日期_类型_场景_主体动作_序号.原扩展名`。素材库用「视频或图片 / 拍摄月份 / 内容类型」分层。拍摄日期缺乏依据时归到 `日期未知`。以下为示例。

```text
素材库/
├── 视频/
│   └── 2026-05/
│       └── 河岸风景/
│           ├── 20260518_视频_河岸_逆光步道与桥影_001.mp4
│           └── 素材索引.md
└── 图片/
    └── 2026-05/
        └── 街景/
            ├── 20260518_图片_旧街_窗边光影_002.jpg
            └── 素材索引.md
```

| 画面描述 | 标签 | 源片定位 |
| --- | --- | --- |
| 沿河岸步道向前拍。夕阳从树后照进来，镜头转向映在水里的桥影。 | 横屏、河岸、逆光、桥影、转场 | `00:04.20` 树间逆光 · `00:12.80` 水面桥影 |

时间点用于回到原片定位。正式落剪时，再回看片段确定连续可用的入出点和同期声。

## 安装到 Codex

需要 Python 3.9+。要查看视频帧和媒体元数据，可以安装 FFmpeg（`ffmpeg`、`ffprobe`）。批量归档需要 **macOS**，且源目录和素材库位于同一支持扩展属性的卷。

```sh
mkdir -p "$HOME/.agents/skills"
git clone https://github.com/lord97j/media-preflight.git "$HOME/.agents/skills/media-preflight"
```

在 Codex 中用 `$media-preflight` 调用，提供源目录的绝对路径。选择直接归档时，再提供素材库根目录。团队也可把整个 Skill 放入仓库的 `.agents/skills/media-preflight`。[Codex Skills 文档](https://learn.chatgpt.com/docs/build-skills)

## 命令行用法

Codex 负责查看画面、判断证据和整理建议。Python 脚本负责盘点、预览、执行和检索。脚本使用标准库，运行时无需 API 密钥。

### 1. 生成基础索引

在本仓库目录运行。先把 `source_dir` 换成实际素材路径，计划文件放在独立的临时目录。

```sh
source_dir="/path/to/inbox"
library_dir="/path/to/media-library"
review_dir="$(mktemp -d)"

python3 scripts/media.py scan "$source_dir" > "$review_dir/review.json"
python3 scripts/media.py render "$source_dir"
```

`render` 会在源目录生成 `素材索引.md`。把看过画面的候选项填回 `review.json`，保留扫描得到的源路径和指纹。每条注释至少填写 `description`、`tags` 和 `evidence`。对只登记建议名的条目用 `action: alias`。

```sh
python3 scripts/media.py plan "$source_dir" "$review_dir/review.json" > "$review_dir/plan.json"
python3 scripts/media.py render "$source_dir" --plan "$review_dir/plan.json"
```

### 2. 审阅归档路径后再执行

素材库根目录须已存在。将选中行的 `action` 改为 `archive`，补充 `archive_type`（例如「河岸风景」）和可信的 `annotation.capture_time`（例如 `2026-05-01T10:30:00+08:00`）。其余素材使用 `skip`。

```sh
python3 scripts/archive.py plan "$source_dir" "$library_dir" \
  "$review_dir/review.json" > "$review_dir/archive.json"

python3 scripts/archive.py preview "$source_dir" "$library_dir" \
  "$review_dir/archive.json"
```

打开源目录的 `素材索引.md`，核对每项改动和目标路径。确认后，将下面的占位符替换为索引中显示的计划 SHA-256。

```sh
python3 scripts/archive.py apply "$source_dir" "$library_dir" \
  "$review_dir/archive.json" --sha256 "这里填写已确认的计划摘要"
```

遇到中断，可以先运行 `archive.py recover` 检查待恢复记录。完整命令与确认规则见 [SKILL.md](SKILL.md)。

### 3. 按剪辑需求检索

在已经提交或归档的分类目录里，可以按多个词同时匹配当前文件名、标准名和注释字段。

```sh
python3 scripts/media.py search "$library_dir/视频/2026-05/河岸风景" \
  横屏 逆光 河岸 --kind video
```

仅预览的建议可以直接在 Markdown 中浏览。提交后，可搜索的数据存于分类目录的 `.media-preflight/index.json`。

## 文件备注和标签

归档把画面描述写入 macOS 文件备注扩展属性，并在现有 Finder 标签上追加新标签。标签、描述、证据和时间点也会保存到分类索引。素材内容字节及 EXIF、IPTC、QuickTime 容器内的字段保持原样。

## 许可与参考

代码与文档使用 [MIT License](LICENSE)。

模板与批量审阅流程参考了 RenamerX。[使用指南](https://sancijun.com/guides/renamerx-user-guide) · [官网](https://renamerx.com/zh)
