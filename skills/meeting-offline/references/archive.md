# 录音与交付归档

默认目录由 `device_context.py init` 的 `archive` 返回，新设备通常是 `~/Documents/会议纪要/<device-id>`；设置了 Obsidian 库时是库里的 `会议纪要` 文件夹（见 [Obsidian 库](obsidian.md)）。每场一个 `YYYY-MM-DD_会议主题` 文件夹，里面放移动后的原录音、`会议纪要.html`、`会议纪要.png`、`会议纪要.md` 和 `逐字稿.md`。原录音保留原文件名；转写、词库与角色档案仍保存在独立本地工作区，不混入对外交付文件。仅列出最近录音不会触发移动，选定要处理的文件后才执行。

## 选定录音后

```bash
python3 <skill-dir>/scripts/archive_meeting.py \
  --audio /path/to/录音.m4a --topic 会议主题 --meeting-date 2026-10-12
```

`--meeting-date` 仅填有依据的会议日期。未知时省略，脚本用本机当天日期并明确命名 `YYYY-MM-DD（归档）_主题`；可用 `--archive-date YYYY-MM-DD` 显式指定当前本地归档日期。不要把导出文件的创建日期当作会议日期。主题暂不明确时可省略，先用原文件名，完成后会按最终主题调整同一个文件夹。下游转写、工作区导入应使用脚本返回的 `audio` 路径。

普通本地录音默认移动原文件，不保留源目录副本。Apple 语音备忘录应用托管的库文件仍只读，先由只读导出流程取得普通本地文件，再移动导出文件。此归档操作本身没有网络请求，也不扩大转写的云端授权。

## 正文与 PNG 完成后

工作区导出已自动生成 HTML 与 PNG。打开 PNG 检查后：

```bash
python3 <skill-dir>/scripts/workspace.py publish --workspace /path/to/workspace
python3 <skill-dir>/scripts/archive_meeting.py \
  --workspace /path/to/workspace --source 4
```

脚本核对发布版本、HTML/PNG/Markdown 校验值和录音校验值，再归档并记录新 `audio_path`、原路径；现有复核服务按新路径回听。检查音频 Range 请求、文件夹内文件及校验值后交付。

独立模式可补齐同一录音的交付物：

```bash
python3 <skill-dir>/scripts/archive_meeting.py \
  --audio /path/to/归档后的录音.m4a --topic 最终会议主题 \
  --html /path/to/已核对的纪要.html --png /path/to/对应纪要.png
```

可用 `--root /path/to/会议纪要` 覆盖归档根目录。同一音频按校验值定位目录，但工作区交付同时校验 `workspace_id` 与 `source_id`；不同工作区或来源不得占用已绑定目录覆盖交付物。尚无交付物的独立音频归档，可由使用实际归档音频路径的工作区首次绑定；旧记录缺少绑定且已有交付物时拒绝自动认领。不同音频的同名会议加后缀，不覆盖原文件。隐藏的 `.archive-index.json`（Obsidian 库里是每台设备一份 `.archive-index-<设备>.json`）和 `.archive.json` 用于续做、查验和恢复路径。已归档的 HTML、PNG 或 Markdown 若被用户修改，保留原稿，新生成稿另存为 `会议纪要-更新版-校验值前缀`（逐字稿为 `逐字稿-更新版-…`）。再次执行前沿用工作区/脚本返回的新路径，不继续使用已移动的旧路径。

目录更名前先保存包含工作区、来源、旧路径和新路径的恢复记录；更名后清单或索引写入失败可按提示重试。恢复时请沿用工作区或初次调用中的 `--audio` 路径；若目标目录被占用，先核对并保留占用内容，再将其移开，或恢复原目录后重试，不能直接覆盖。脚本验证恢复记录与音频、清单相符，不搜索并冒认其他文件。

工作区发布的网页引用录音文件名，因此归档若会改变录音文件名会在移动前拒绝。请保留原名，或先在工作区更新录音路径、重新导出核对并发布，再归档。跨磁盘移动可能表现为复制后删除，不能保证任意断电下仅出现一份音频；失败时保留现场并按路径和校验值核对，不盲目重复移动。
