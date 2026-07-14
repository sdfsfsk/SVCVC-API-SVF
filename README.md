# SVCVC-API-SVF

面向 AstrBot `astrbot_plugin_matsuko_cover` 的 SoulX-Singer-SVC 轻量中间层。目标歌曲可以选择 SoulX 自带分离、MSST BS-Roformer 分离或不分离；F0 提取和音色转换仍由上游 SoulX-Singer `127.0.0.1:7861` 完成。

GitHub 源码仓库不提交大型模型权重、ROCm 运行时、生成音频或任何私人参考音色；离线发布包可以把 MSST 运行时与权重直接放在本项目内部，不依赖外部目录。SoulX-Singer 的 Windows AMD ROCm 适配与中间层接口补丁见 [`sdfsfsk/SoulX-Singer-AMD-Patch`](https://github.com/sdfsfsk/SoulX-Singer-AMD-Patch)。上游项目版权归 [Soul-AILab/SoulX-Singer](https://github.com/Soul-AILab/SoulX-Singer) 所有。

## 一键启动

1. 准备官方 SoulX-Singer，并应用上述 AMD 补丁；在 `启动SoulX-Singer.bat` 中选择 `1`，等待 7861 就绪。MSST 自动混伴奏需要补丁提供 `/soulx_svc_convert_external_acc_path` 隐藏接口。
2. 双击 `启动SVCVC-API.bat`。首次启动会自动安装独立 CPython 3.12 便携环境和轻量依赖。
3. 中间层默认地址为 `http://127.0.0.1:6767`，与 `astrbot_plugin_matsuko_cover` v2.8.1 起的默认 `svcvc_base_url` 一致；启动脚本会读取 `config.json` 中的实际 `server.host` / `server.port`，并清理同端口的旧实例。

### MSST 单目录离线包

`config.json` 默认使用 `"msst.root": "."`，MSST 依赖全部位于 `SVCVC-API-SVF` 内部：

```text
SVCVC-API-SVF/
├─ runtime/                         # 中间层轻量 Python
├─ runtime-rocm/Scripts/python.exe  # MSST 原生 AMD ROCm Python
├─ msst/
│  ├─ msst_separate.py
│  ├─ pretrain/vocal_models/model_bs_roformer_ep_317_sdr_12.9755.ckpt
│  └─ configs/vocal_models/model_bs_roformer_ep_317_sdr_12.9755.ckpt.yaml
├─ app.py
└─ config.json
```

#### MSST 模型下载

Git 仓库不会提交模型权重。下载后请保持文件名不变，并放入 `msst/pretrain/vocal_models/`：

| 模型 | 下载链接 | SHA-256 |
|---|---|---|
| `model_bs_roformer_ep_317_sdr_12.9755.ckpt`（默认、推荐） | [UVR 公共模型 GitHub Release](https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/model_bs_roformer_ep_317_sdr_12.9755.ckpt) | `5B84F37E8D444C8CB30C79D77F613A41C05868FF9C9AC6C7049C00AEFAE115AA` |
| `bs_roformer_karaoke_frazer_becruily.ckpt`（可选） | [Becruily Hugging Face](https://huggingface.co/becruily/bs-roformer-karaoke/resolve/main/bs_roformer_karaoke_frazer_becruily.ckpt)；[ModelScope 备用源](https://modelscope.cn/models/CCYellowStar/bs_roformer_karaoke_frazer_becruily/resolve/master/bs_roformer_karaoke_frazer_becruily.ckpt) | `EB90EE24C1154D83FBCFD27E96182F19E061557CC6E4746953125E08C29389F9` |

Windows PowerShell 可在目标目录执行：

```powershell
curl.exe -L "https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/model_bs_roformer_ep_317_sdr_12.9755.ckpt" -o "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
curl.exe -L "https://modelscope.cn/models/CCYellowStar/bs_roformer_karaoke_frazer_becruily/resolve/master/bs_roformer_karaoke_frazer_becruily.ckpt" -o "bs_roformer_karaoke_frazer_becruily.ckpt"
```

下载后可用 `Get-FileHash -Algorithm SHA256 <文件名>` 对照上表校验，避免损坏或下错模型。

把整个 `SVCVC-API-SVF` 文件夹压缩后即可分发，不需要在旁边再放 `RVCSVC-API-MSST`。由于 GitHub 会忽略 `runtime-rocm/` 和 `msst/pretrain/`，从源码仓库自行克隆的用户仍需补齐这两部分；缺失时 SoulX 分离和“不分离”可继续使用，MSST 模式会明确报告缺少哪项依赖。

启动脚本只会结束命令行明确属于本目录 `app.py` 的旧进程；若配置端口属于其他程序会拒绝操作，并等待旧实例真正释放端口后再启动。

合并旧版本后若便携环境缺少新加入的 `yt-dlp`，启动脚本会自动检测并根据 `requirements.txt` 修复依赖。若手动修改 `config.json` 的端口，也需要在 AstrBot 插件配置中同步修改 `svcvc_base_url`，或使用 `/设置svcvc后端链接 <URL>`。

## 参考音色

将参考音频放入 `voice_profiles/`。支持 WAV、FLAC、MP3、OGG、M4A、AAC，并支持同名 JSON 元数据。目录里的 `official_demo.mp3` 是官方 Prompt 示例，只用于首次联调。

## 来源输入与歌曲搜索

"音色转换"标签页中的目标歌曲和参考音色都支持以下来源：B站关键词、BV 号、`b23.tv` 短链或视频链接，网易云歌曲 ID 或链接，HTTP(S) 音频链接，本地路径和页面上传文件。

参考音色按以下优先级选择：参考上传文件 > 参考来源文本 > `voice_profiles/` 下拉框。也就是说，旧的已保存音色工作流保持不变；填写或上传临时参考音色后，会优先使用临时参考而不是下拉框选择。

转换输入框会自动识别 B站关键词，并优先在音乐分区搜索；音乐分区没有结果时会回退全站。也可以先到“歌曲搜索”标签页，在 B站或网易云搜索歌曲，再复制结果链接到目标歌曲或参考音色来源框。搜索页每页显示 10 条，支持上一页/下一页、封面预览和逐条复制链接；它只用于查找和复制链接，不会自动下载、回填表单或启动转换。

网易云搜索优先使用 `api.vkeys.cn`；主接口请求失败、返回异常或没有结果时，会自动改用网易云 Web 搜索接口。备用接口使用标准偏移量分页，并根据返回的 `picId` 生成专辑封面链接。

## Gradio API

所有 API 都显式命名：

- `/show_model`：返回 `profile_id` 字符串数组。
- `/refresh_profiles`：刷新 WebUI 参考音色下拉选项。
- `/list_voice_profiles`：返回音色 ID、显示名、文件、SHA256 和 Prompt 分离建议。
- `/health`：返回服务版本、音色数量、SoulX 在线状态和实际发现的推理端点。
- `/show_msst_models`：返回本目录内已安装的 MSST 模型，并以 `current` 标记当前选择。
- `/select_msst_model`：按模型 ID、显示名或序号切换 MSST 模型，并原子写回 `config.json`。
- `/cache_info`：返回结果、下载和随机输出缓存占用。
- `/clear_cache`：安全清理 `all`、`results`、`downloads` 或 `outputs`；不会删除 `voice_profiles/` 参考音色。
- `/convert`：按目标分离方式调用 MSST/SoulX-SVC，返回 `(output_audio, cache_hit)`。
- `/search_song_catalog`：搜索 B站或网易云；参数为 `platform`、`keyword` 和可选的 `page=1`，返回搜索页使用的结果、卡片和链接数据。

`/convert` 参数顺序：

```text
song_name_src, model_dropdown,
prompt_vocal_sep, target_vocal_sep, auto_shift, auto_mix_acc,
pitch_shift, n_step, cfg, seed, random_seed,
target_upload=None, reference_source="", reference_upload=None
target_separation=""
```

原有前 14 个参数的顺序和默认值没有变化；已部署的 AstrBot 插件可继续按旧方式调用 `/convert`。新增的末尾参数 `target_separation` 可选值为 `soulx`、`msst`、`none`。留空时会按旧 `target_vocal_sep` 布尔值迁移：`true -> soulx`，`false -> none`。

MSST 模式先使用 `model_bs_roformer_ep_317_sdr_12.9755.ckpt` 生成 vocals/other，再把纯人声送入 SoulX；启用自动混合伴奏时，other 会通过配套隐藏接口交给 SoulX，确保自动或手动变调后伴奏使用相同的等效移调。分离 stems 会独立缓存，切换参考音色或种子时无需重复分离同一首歌。

中间层会在开始 MSST 分离前检查运行中的 SoulX 是否已公开 `/soulx_svc_convert_external_acc_path`。若 7861 仍是旧进程，会立即报错并提示用当前 `启动SoulX-Singer.bat` 重新启动，不再等完整首歌曲分离结束后才失败。

`song_name_src` 和 `reference_source` 支持本地绝对路径、HTTP/HTTPS 音频 URL、网易云歌曲 ID 或链接，以及 B站关键词、BV 号、`b23.tv` 短链或 `bilibili.com/video/` 链接。本地文件必须使用支持的音频扩展名且不能超过 `download.max_size_mb`。远程下载会检查每一跳重定向和最终 URL，拒绝本机、私网、链路本地及保留地址，并校验大小、内容类型和音频文件头。QQ 音乐由 AstrBot 插件负责搜索并下载到本地，再把本地文件交给中间层；因此中间层日志会显示为“已收到本地音频（QQ音乐由 AstrBot 插件预下载）”。中间层兼容 SoulX 路径端点 `/soulx_svc_convert_path`、音频端点 `/soulx_svc_convert` 与旧端点 `/_start_svc`，并且只允许一个 GPU 推理任务同时运行。

MSST/SoulX 的分离、Prompt/Target 预处理、F0 提取、逐段推理（如 `9/14`）、伴奏混合和 MP3 导出进度会通过 Gradio 进度事件传给中间层，再由插件按 `progress_update_interval` 输出到 QQ。MSST 原生 ROCm 子进程会把 `Processing audio chunks: 0%～100%` 转成 `MSST 正在分离目标歌曲 [x%]`，并由请求主线程安全上报，避免子线程丢失 Gradio 进度上下文。最终结果统一转为 320 kbps MP3，降低 QQ 语音上传和大 WAV 下载失败的概率；启用插件“同时发送文件”后，QQ群优先使用 OneBot 群文件上传接口。

## 随机种子与缓存

- v1.2.1 起，SoulX 种子使用完整的无符号 32 位范围 `0～4294967295`，插件、中间层和本体 WebUI 应同步更新。
- `random_seed=false`、`seed=42`：固定种子，可复现并允许持久缓存。
- `seed=-1`：中间层生成一次 `0～4294967295` 的实际种子。
- `random_seed=true`：表示随机任务；实际种子仍使用传入的 `seed`（若为 `-1` 则生成），默认不读写持久缓存。

建议插件在任务创建时只生成一次实际随机种子，并把同一实际值用于重试。日志会打印实际种子。缓存键包括目标音频 SHA256、Prompt SHA256、全部 SoulX 参数、实际种子、管线版本，以及 SoulX 主模型、推理配置、Whisper 编码器和预处理模型的资产指纹；替换任一关键资产后不会误用旧缓存。资产指纹只读取相对路径、文件大小和纳秒修改时间，不会反复读取或哈希数 GB 的模型内容，并按 `soulx.asset_fingerprint_refresh_seconds` 定期刷新。

插件默认关闭 `svcvc_random_seed`，相同歌曲、音色和参数的第二次请求会命中持久缓存；需要每次产生不同结果时可重新打开随机种子，但随机任务默认不会命中持久缓存。

WebUI 的参考音色下拉框支持动态值并提供“刷新参考音色”按钮，新放入 `voice_profiles/` 的音色无需重启中间层即可选择。

## 配置

编辑 `config.json` 可修改监听地址、SoulX 地址、MSST 根目录/模型/批大小/重叠次数、超时、下载大小限制和缓存上限。`msst.stem_cache_max_files` 默认保留 20 首歌曲的 vocals/other；`download.max_files`（默认 100）限制网易云、HTTP 和 B站源音频缓存数量，命中时会刷新使用时间并按最近使用顺序淘汰旧文件；当前任务使用的源文件不会被清理。执行 `results` 或 `all` 缓存清理后会自动重建 `_msst_stems` 等运行期目录，下一次 MSST 任务无需手动建目录。SoulX 请求超时默认 `9000` 秒，以兼容 AMD 上的长歌曲推理；服务默认只监听 `127.0.0.1`，不会暴露给局域网。

## 许可证与数据说明

中间层源码使用 Apache License 2.0。`voice_profiles/` 中的音频由使用者自行提供，默认被 `.gitignore` 排除；使用者应确保对参考音频、目标歌曲和生成结果拥有相应权利。
