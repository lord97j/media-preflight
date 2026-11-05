---
name: media-preflight
description: 为本地图片和视频生成统一的素材索引文档，推荐标准名、画面描述与标签。确认后可批量改名、写入文件备注和标签并归档到素材库，更新分类目录索引以便剪辑选片。
---

# 多媒体素材预处理

给定源目录时，在其中生成 `素材索引.json`，列出全部已扫描素材、相对文件路径、标准名、画面描述、标签与状态。父目录索引的 `assets` 汇总所有下级素材，`child_indexes` 列出直接子目录索引。用户只要索引或暂未确认时，源目录仅生成或刷新 JSON 索引。用户选择直接修改并确认素材库与归档方案后，批量改名、写备注和标签并移动文件，在有新增素材的分类目录及其上级刷新 `素材索引.json`，归档成功后撤下本批源目录的索引。

使用随附的 `scripts/media.py` 盘点与检索，使用同目录的 `scripts/archive.py` 规划和执行归档。将两者的绝对路径分别记作 `$media_script` 和 `$archive_script`。从 Notion 读取时，使用本机已安装的 `~/.codex/skills/media-preflight/scripts/media.py`，按实际安装位置替换。源目录记作 `$media_dir`，用户指定的素材库根目录记作 `$library_dir`。脚本使用 Python 3.9+ 标准库；人声检测的 `voice` 子命令需要 faster-whisper，用 `--python` 指向已安装它的解释器。归档需 macOS、支持文件扩展属性且源与目标在同一卷。读取视频和生成预览图可用 `ffprobe`、`ffmpeg`，图片可用本地图像查看工具。

## 盘点与模板建议

1. 读源目录与素材库适用的协作规则。发现有配对文件、剪辑工程引用或原件保留约定时，把实际影响纳入方案。用户确认直接修改并归档后按授权执行，同名 SRT/LRF 随视频成组移动时启用配套文件归档，其他配对类型保留待复核。只需要浏览或原件需原址保留时，使用索引模式。
2. 运行 `python3 "$media_script" scan "$media_dir"`，把 JSON 输出写到目标目录以外的审阅文件。递归清点支持的图片和视频，按命名不合规、缺注释、索引与文件不符及空文件分类。任一项不合规都列入候选。索引须包含已合规、待处理和待核验的每条素材，并单列无法遍历或打开的路径。文件后缀只做初筛，遇到相机私有格式或未识别扩展名时单独说明覆盖范围。
3. 复用已确认的索引规则。首次根据目录内容推荐一个主模板，并用真实候选展示 3–5 组原名、建议名和注释，混合目录必要时给出一个项目版备选。默认用 `日期_类型_场景_主体动作_序号`，例如 `20261103_视频_古镇_沿河步行_001.MOV` 和 `日期未知_图片_室内_窗边剪影_002.HEIC`。需要区分多项目时用 `项目_日期_类型_场景_主体动作_序号`。日期优先来自可信的拍摄元数据或用户确认的来源。日期不可靠时写 `日期未知`。按相对路径稳定分配序号并保留已有编号。已有文件名符合选定标准时沿用。使用已有约定或项目版时，用 `scan --pattern` 提供匹配文件名主体的正则，扩展名由脚本单独校验。确认后的正则随索引保留。归档模式下场景段取归档分类，脚本按分类自动改写；索引模式下场景段写画面里的地点或场景。
4. 按下一节核验所有拟处理项。已存在的内容描述能对应当前原件时可复用，并注明来源。必要时读已有 EXIF/IPTC/QuickTime 描述、字幕或本地素材报告，不把任意非空备注当作达标。视频日记已有逐视频索引可提供帧、描述和原片时间码。

## 注释和证据

- 图片打开原图或可靠预览。视频先用探测工具取时长和方向，再查看首、中、尾以及能代表场景变化的帧。长片或多场景按段查看。仅抽帧时明确标注抽帧证据。精确入出点需回看对应片段，音频和对白来自实听或可对齐的转写。
- `description` 写可浏览的画面摘要。视频有可靠的逐段分析时，把完整画面描述和对应原片时间码放入 `full_description`，注明是抽帧还是连续回看。旧报告要先核对原片指纹再复用。 `tags` 是便于组合搜索的短标签。 `evidence` 标记已看的帧、片段、元数据或对应旧索引。可追加 `shot`、`orientation`、`usage`、`limitations` 及 `segments`。片段以原视频秒数记录，只有单点证据时用 `at_s`。区间有依据时写 `in_s` 和 `out_s`。
- 标签同时服务素材来源与剪辑用途。脚本在生成计划时自动加入处理前的直接上级文件夹名，例如原片在 `江西婺源/` 下，就加入 `江西婺源`，预览与归档索引都保留这个标签。把目录名当作用户的素材分组，具体拍摄地仍以画面或可信元数据为准。
- 按实际画面选择剪辑标签。`A-roll` 用于承担主叙事的口播、采访或主要事件。`B-roll` 用于辅助叙事的环境、动作和补充画面。可叠加 `细节镜头`（手部、物件或局部特写）、`空镜头`（以环境为主体）、`过渡镜头`（用于连接场景或段落）。多场景视频确有不同用途时可同时打多个标签，用时间点区分。再按需补充主体、场景和画幅。沿用这些标准写法，证据不足时跳过对应标签。
- 人物身份、地点、授权和音频状态无依据时留空或写未知。解码失败、只能读文件名、画面判断不足的项目保留在待核验清单。禁止凭猜测补写内容。使用本地工具处理文件与生成预览。需要第三方识别服务时遵守用户对素材外发的授权。
- 示例注释为 `{"description":"一人沿河从画面左侧走入，背景是石栏与水面","tags":["古镇","步行","河边","横屏","B-roll","有人声"],"evidence":"查看了 0.8–4.5 秒原片；Silero VAD 全片检出人声","usage":"行程过渡","segments":[{"in_s":0.8,"out_s":4.5,"note":"进入画面至走过石栏"}]}`。

## 人声标签

视频必须有且只有一个 `有人声` 或 `无人声` 标签，来自批量检测，不按有没有音轨、也不凭印象手填。检测使用 Silero VAD，参数与常见的本地语音批处理脚本一致，只判定有没有人声候选，不转写、不调用云服务。

1. 先跑检测，再盘点或规划。用装了 faster-whisper 的解释器：
   `python3 "$media_script" voice "$media_dir" --python /path/to/venv/bin/python`
   已有豆包 `doubao_vad_batch.json` 时加 `--from-batch <doubao_vad_batch.json>`，按原件大小和修改时间复用原批次状态，未覆盖的视频再本地检测。结果写入 `$media_dir/.media-preflight/voice.json`，按 inode 缓存，原件改动后自动重测。`--force` 重测全部，`--threshold` 默认 0.5，`--workers` 默认 2。检测逐个视频解码音频，长素材耗时明显，先说明进度再开跑。
2. `scan` 每行带 `voice` 字段，顶层 `voice_unchecked` 是尚未检测的视频数。`plan`、`render` 和归档计划会从缓存自动写入标签，替换旧的人声标签，不会两个并存。
3. 状态映射：`has_voice` → `有人声`；`no_voice` → `无人声`；`no_audio` → `无人声`，证据注明无音频轨道；`unreadable` 或 `error` → 不打标签，留在待核验清单。视频没有有效人声结果时 `plan` 会拒绝，先补检测或说明原因。
4. `无人声` 只表示 VAD 未检出人声，不等于确认没有语音，可能漏掉小声、方言、远处或多人交叠；人声候选也可能包含歌声、广播或音乐。`evidence` 写明“Silero VAD 全片检测，检出/未检出人声”，引用同期声前仍要听原声。已有豆包批次时与批次状态对照，两者冲突先复核原件再定。

## 一次确认、批量归档

目标目录的标准流程是「生成索引 → 文本确认 → 一条命令执行」，不再需要单独的计划文件：

1. 视频先跑 `voice`（见上节）。
2. `python3 "$media_script" render "$media_dir"` 生成 `素材索引.json`。
3. 在 JSON 的 `assets` 里补齐 `description`、`evidence`、`tags`（含 `有人声`/`无人声`）、`capture_time` 和归档分类 `archive_type`。参考已有目标文件时也可先 `scan`，但最终以这份索引为准。
4. 把待归档条目以文本清单给用户确认：原文件名、标准名、目标路径、描述与标签。`archive_type` 存在时 `render` 会算出 `archive_target`，可直接核对。
5. 用户确认后执行：
   `python3 "$archive_script" batch "$media_dir/素材索引.json" "$library_dir"`
   一次完成改名、写 Finder 注释与标签、移动配套 SRT/LRF、刷新受影响目录索引。没有 `archive_type` 的条目不归档，在 `skipped_without_category` 列出。
6. 执行后源 `素材索引.json` 保留为记录：`mode` 变 `archived`，每条带 `status: archived`、`archived_path`、`archived_filename`，并保留 `original_filename` 和 `source_path`（原完整路径）。素材库索引同样保留原始文件名和完整路径，`archive_type` 一并写入。
7. 索引生成后文件大小或修改时间发生变化时，`batch` 会整批中止，不移动任何文件；重新 `render` 再确认。

## 索引模式

先按上节完成视频人声检测，再编辑盘点 JSON 中候选的 `standard_name` 与 `annotation`，把有完整建议的条目设为 `alias`，其余设为 `skip`。用 `python3 "$media_script" plan "$media_dir" "$review_json" > "$plan_json"` 校验，再用 `python3 "$media_script" render "$media_dir" --plan "$plan_json"` 生成完整的 `素材索引.json`。没有可提议条目时省略 `--plan`，仍盘点全部素材。索引模式只生成 JSON，计划文件放在源目录以外。用户另行确认要写入本地可搜索索引时，可用 `media.py apply` 和计划 SHA-256 提交别名。同名人工 JSON 需先核对内容，脚本只覆盖自己生成的索引。转换旧版索引时，先写入新 JSON，再移除有本 Skill 标记的旧 Markdown。

## 直接修改并归档

已确认 `素材索引.json` 时优先用上节的 `batch`。以下 plan / preview / apply 流程用于需要单独计划文件、逐条预览或 SHA-256 复核的场景。

1. 从用户处取得已有的素材库根目录。分类默认用 `视频或图片/YYYY-MM/DD/内容类型`，例如 `视频/2026-10/30/河谷与竹筏`。日期缺乏依据时用 `视频/日期未知/内容类型`，不建日目录。归档类型用少量可复用的类别，优先沿用目标库已经存在的类别，且不要含下划线。
2. 从盘点 JSON 保留源路径与指纹。选中行设 `action: archive`，补齐 `standard_name`、`annotation` 和 `archive_type`。视频的 `annotation.tags` 需含 `有人声` 或 `无人声`；voice 缓存命中且指纹一致时脚本会自动补上。标准名的场景段必须与归档分类一致：脚本在归档计划里自动改写，把原场景并入主体动作，例如 `20261030_视频_碧绿河道_筏队行进_028.MP4` 归入 `河谷与竹筏` 后变成 `20261030_视频_河谷与竹筏_碧绿河道筏队行进_028.MP4`；场景已等于分类时不重复拼接。自定义 `--pattern` 不做自动改写，需在审阅时手工对齐。同名 SRT/LRF 需一并归档时加 `archive_companions: true`，逐条核对主片与配套文件的目标路径。从可信日期生成 `annotation.capture_time`（ISO 日期或含时区的日期时间）。执行 `python3 "$archive_script" plan "$media_dir" "$library_dir" "$review_json" > "$plan_json"`。通过 `python3 "$archive_script" preview "$media_dir" "$library_dir" "$plan_json"` 在源目录索引中展示每条标准名、画面描述、标签与完整目标路径。
3. 用户确认本批操作及目标素材库、分类规则后，用 `python3 "$archive_script" apply "$media_dir" "$library_dir" "$plan_json" --sha256 "$reviewed_sha256"` 执行。只批准模板或目的地时继续做预览。已明确指定本批、素材库与执行规则并要求立即处理时，直接校验和执行。源文件或归档目标发生实质变化时重新展示方案。
4. 归档使用标准名移动文件。配套 SRT/LRF 与主片同目录同主名，保留各自扩展名，分类索引保留配套文件的相对路径。把 `description` 和已有的 `full_description` 一起写入 macOS 文件备注扩展属性，把 `tags` 加到 Finder 标签并保留已有标签。常规归档保留 EXIF、IPTC 和 QuickTime 容器字段。用户指定「属性里的描述和注释都用画面描述」时，按下节同步视频内嵌字段。脚本合并既有分类索引，为每个收到素材的子目录及其上级更新 `素材索引.json`，其中保留画面描述、标签、证据、原片路径与时间点。处理成功后撤下本批源目录生成的索引。未选中的素材留在源目录。核验改名、移动、备注、标签和索引路径后再报告。
5. 发生中断时用 `python3 "$archive_script" recover "$media_dir" "$library_dir"` 查看事务记录。恢复获准后加 `--confirm`，将移动、文件备注、标签和索引恢复到批次之前。普通索引批次的撤销用 `media.py rollback`，撤销后重新生成 JSON 索引。

## 文件属性与 Finder 注释

- Finder「显示简介」的注释由目录的 Finder 记录显示，文件扩展属性和 Spotlight 能读回时，仍须核对 Finder 界面。使用 `scripts/finder_comments.py` 同步缺失的目录记录。该脚本需要 Python 环境里的 `ds-store`，可在独立虚拟环境安装。默认只预检。用户同意写入时提供 `--backup-dir`（素材库外的目录），会保留原始目录记录和非空的用户注释。用户明确要求统一覆盖时才加 `--replace-existing`。
- 用户要求视频属性的「描述」和「注释」一致时，采用同一画面文本。有 `full_description` 时附在摘要后。用 ExifTool 在工作副本设置 MP4/MOV 的 `ItemList:Description`、`ItemList:Comment`、`Keys:Description` 和 `Keys:Comment`，并将同一文本写入 Finder 注释。保存旧字段，逐项读回，核对音视频流和媒体数据一致后替换，随后更新本地指纹与 JSON 索引。文件格式或字段不可写时列出跳过项。
- 若 Finder 窗口仍显示旧值，说明界面缓存状态，区分文件字段验证、目录记录验证与界面实际显示。同步后优先重新打开该目录核验。

## 刷新与全库检索

- 刷新现有素材库时运行 `python3 "$media_script" render "$library_dir" --tree`。脚本由分类子目录向上更新索引，优先读取文件当前的 Finder 标签和备注，并保留已有的逐段描述、时间点与配套文件关系。刷新只更新索引。已在素材库里的素材按当前路径核验，只有用户另外授权重新分类时才移动。
- JSON 的 `assets` 是扁平数组，每条包含 `path`、`filename`、`standard_name`、`tags`、`description`、`original_filename`、`source_path` 和按需的 `full_description`、`segments`、`companions`、`archive_type`、`archive_target`。素材、配套文件和 `child_indexes` 的路径相对当前索引所在目录，`source_path` 是原完整路径。预览另有 `plan_sha256` 和逐条 `proposal`。父目录汇总子目录素材，可直接扫描根目录的 `assets` 检索全库。
- 运行 `python3 "$media_script" search "$library_dir" "河边" "步行" --kind video`。空关键词列出全部当前有效条目，多词同时匹配文件名、标签和描述。按剪辑意图返回少量候选，给真实路径、标准名、匹配理由与已核验时间码。正式落剪前打开命中片段复核。

命名流程参考了先定模板、逐条审阅、批量应用与撤销的设计。 [RenamerX 中文使用指南](https://sancijun.com/guides/renamerx-user-guide) · [RenamerX 官网](https://renamerx.com/zh)
