# SVCVC-API-SVF

面向 AstrBot `astrbot_plugin_matsuko_cover` 的 SoulX-Singer-SVC 轻量中间层。它不安装 PyTorch、ROCm、MSST 或 UVR5；所有人声分离、F0 提取、音色转换与伴奏混合均交给上游 SoulX-Singer `127.0.0.1:7861`。

本仓库仅发布中间层源码与便携 Python 安装脚本，不包含模型权重、ROCm 运行时、生成音频或任何私人参考音色。SoulX-Singer 的 Windows AMD ROCm 适配与中间层接口补丁见 [`sdfsfsk/SoulX-Singer-AMD-Patch`](https://github.com/sdfsfsk/SoulX-Singer-AMD-Patch)。上游项目版权归 [Soul-AILab/SoulX-Singer](https://github.com/Soul-AILab/SoulX-Singer) 所有。

## 一键启动

1. 准备官方 SoulX-Singer，并应用上述 AMD 补丁；在 `启动SoulX-Singer.bat` 中选择 `1`，等待 7861 就绪。
2. 双击 `启动SVCVC-API.bat`。首次启动会自动安装独立 CPython 3.12 便携环境和轻量依赖。
3. 中间层默认地址为 `http://127.0.0.1:6767`；启动脚本会读取 `config.json` 中的实际 `server.host` / `server.port`，并清理同端口的旧实例。

启动脚本只会结束命令行明确属于本目录 `app.py` 的旧进程；若配置端口属于其他程序会拒绝操作，并等待旧实例真正释放端口后再启动。

## 参考音色

将参考音频放入 `voice_profiles/`。支持 WAV、FLAC、MP3、OGG、M4A、AAC，并支持同名 JSON 元数据。目录里的 `official_demo.mp3` 是官方 Prompt 示例，只用于首次联调。

## 来源输入与歌曲搜索

“音色转换”标签页中的目标歌曲和参考音色都支持以下来源：B站 BV 号或视频链接、网易云歌曲 ID 或链接、HTTP(S) 音频链接、本地路径和页面上传文件。

参考音色按以下优先级选择：参考上传文件 > 参考来源文本 > `voice_profiles/` 下拉框。也就是说，旧的已保存音色工作流保持不变；填写或上传临时参考音色后，会优先使用临时参考而不是下拉框选择。

转换输入框不会把普通文字当作 B站关键词搜索。请先到“歌曲搜索”标签页，在 B站音乐分区或网易云搜索歌曲，再复制结果链接到目标歌曲或参考音色来源框。搜索页只用于查找和复制链接，不会自动下载、回填表单或启动转换。

## Gradio API

所有 API 都显式命名：

- `/show_model`：返回 `profile_id` 字符串数组。
- `/refresh_profiles`：刷新 WebUI 参考音色下拉选项。
- `/list_voice_profiles`：返回音色 ID、显示名、文件、SHA256 和 Prompt 分离建议。
- `/health`：返回服务版本、音色数量、SoulX 在线状态和实际发现的推理端点。
- `/cache_info`：返回结果、下载和随机输出缓存占用。
- `/clear_cache`：安全清理 `all`、`results`、`downloads` 或 `outputs`；不会删除 `voice_profiles/` 参考音色。
- `/convert`：调用 SoulX-SVC，返回 `(output_audio, cache_hit)`。
- `/search_song_catalog`：搜索 B站音乐分区或网易云，返回搜索页使用的结果、卡片和链接数据。

`/convert` 参数顺序：

```text
song_name_src, model_dropdown,
prompt_vocal_sep, target_vocal_sep, auto_shift, auto_mix_acc,
pitch_shift, n_step, cfg, seed, random_seed,
target_upload=None, reference_source="", reference_upload=None
```

原有前 11 个参数的顺序和默认值没有变化；已部署的 AstrBot 插件可继续按旧方式调用 `/convert`。后面的三个参数全部可选，分别对应目标上传文件、临时参考来源和参考上传文件。

`song_name_src` 和 `reference_source` 支持本地绝对路径、HTTP/HTTPS 音频 URL、网易云歌曲 ID 或链接，以及 B站 BV 号、`b23.tv` 短链或 `bilibili.com/video/` 链接。本地文件必须使用支持的音频扩展名且不能超过 `download.max_size_mb`。远程下载会检查每一跳重定向和最终 URL，拒绝本机、私网、链路本地及保留地址，并校验大小、内容类型和音频文件头。QQ 音乐由 AstrBot 插件负责搜索并下载到本地，再把本地文件交给中间层；因此中间层日志会显示为“已收到本地音频（QQ音乐由 AstrBot 插件预下载）”。中间层兼容 SoulX 路径端点 `/soulx_svc_convert_path`、音频端点 `/soulx_svc_convert` 与旧端点 `/_start_svc`，并且只允许一个 GPU 推理任务同时运行。

SoulX 的 Prompt/Target 预处理、F0 提取、逐段推理（如 `9/14`）、伴奏混合和 MP3 导出进度会通过 Gradio 进度事件传给中间层，再由插件按 `progress_update_interval` 输出到 QQ。最终结果统一转为 320 kbps MP3，降低 QQ 语音上传和大 WAV 下载失败的概率；语音发送失败时插件仍会继续尝试发送音频文件。

## 随机种子与缓存

- `random_seed=false`、`seed=42`：固定种子，可复现并允许持久缓存。
- `seed=-1`：中间层生成一次 `0～10000` 的实际种子。
- `random_seed=true`：表示随机任务；实际种子仍使用传入的 `seed`（若为 `-1` 则生成），默认不读写持久缓存。

建议插件在任务创建时只生成一次实际随机种子，并把同一实际值用于重试。日志会打印实际种子。缓存键包括目标音频 SHA256、Prompt SHA256、全部 SoulX 参数、实际种子、管线版本，以及 SoulX 主模型、推理配置、Whisper 编码器和预处理模型的资产指纹；替换任一关键资产后不会误用旧缓存。资产指纹只读取相对路径、文件大小和纳秒修改时间，不会反复读取或哈希数 GB 的模型内容，并按 `soulx.asset_fingerprint_refresh_seconds` 定期刷新。

插件默认关闭 `svcvc_random_seed`，相同歌曲、音色和参数的第二次请求会命中持久缓存；需要每次产生不同结果时可重新打开随机种子，但随机任务默认不会命中持久缓存。

WebUI 的参考音色下拉框支持动态值并提供“刷新参考音色”按钮，新放入 `voice_profiles/` 的音色无需重启中间层即可选择。

## 配置

编辑 `config.json` 可修改监听地址、SoulX 地址、超时、下载大小限制和缓存上限。`download.max_files`（默认 100）限制网易云、HTTP 和 B站源音频缓存数量，命中时会刷新使用时间并按最近使用顺序淘汰旧文件；当前任务使用的源文件不会被清理。SoulX 请求超时默认 `9000` 秒，以兼容 AMD 上的长歌曲推理；服务默认只监听 `127.0.0.1`，不会暴露给局域网。

## 许可证与数据说明

中间层源码使用 Apache License 2.0。`voice_profiles/` 中的音频由使用者自行提供，默认被 `.gitignore` 排除；使用者应确保对参考音频、目标歌曲和生成结果拥有相应权利。
