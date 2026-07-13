# 双来源转换与 B 站搜索 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** 在不改动 SoulX-Singer 上游的情况下，让现有 SVCVC 转换页支持 B 站、网易云和本地的目标与参考来源，并提供 B 站音乐分区和网易云搜索页。

**Architecture:** 保留 /convert 的既有 11 个位置参数；新增的上传和参考来源参数位于其后并带默认值。app.py 继续负责安全下载、SoulX 调用、缓存与 Gradio 编排；新建的 bilibili_source.py 只负责 B 站链接规范化、WBI 搜索和 yt-dlp 音频下载。搜索结果使用统一的字典形状，供搜索页的 HTML 卡片和复制链接控件使用。

**Tech Stack:** Python 3.12、Gradio 6.3、requests、gradio-client、yt-dlp、pytest。

## Global Constraints

- 不修改 SoulX-Singer 或 SoulX-Singer-AMD-Patch，不得增加分轨端点或 /cover API。
- SoulX 仍完成原生人声分离、变调、音色转换和可选自动伴奏混音。
- /convert 前 11 个参数的名称、顺序、默认值与两个返回值保持不变；新参数只能追加且必须带组件默认值。
- 目标/参考优先级是上传文件、文本来源、voice_profiles 下拉框；缺少目标或参考必须在调用 SoulX 前失败。
- B 站转换输入只接受 BV 号、b23.tv 短链或 bilibili.com/video/ 链接；关键词只允许在搜索标签页使用。
- B 站搜索默认且优先使用音乐分区 tids=3，无结果时再回退全站视频搜索。
- 所有 B 站下载均通过已有的文件大小、音频格式和 LRU 缓存限制；不得绕过公网 URL 或音频有效性校验。

---

### Task 1: B 站链接、搜索与下载模块

**Files:**

- Create: bilibili_source.py
- Create: tests/test_bilibili_source.py
- Modify: requirements.txt
- Create: requirements-dev.txt

**Interfaces:**

- Consumes: requests.Session、pathlib.Path、SoulX 随附 FFmpeg 的目录。
- Produces: is_bilibili_source(value: str) -> bool、resolve_bilibili_video_url(value: str) -> str、search_bilibili_videos(keyword: str, session: requests.Session | None = None, limit: int = 10) -> list[dict[str, str]]、download_bilibili_audio(video_url: str, destination: Path, ffmpeg_location: Path) -> Path。
- Used by: Task 2 的来源解析和 Task 3 的 B 站搜索。

- [ ] **Step 1: 写出会失败的 B 站模块测试**

~~~python
# tests/test_bilibili_source.py
from bilibili_source import is_bilibili_source, resolve_bilibili_video_url


def test_recognizes_only_explicit_bilibili_conversion_sources():
    assert is_bilibili_source("BV1xx411c7mD") is True
    assert is_bilibili_source("https://www.bilibili.com/video/BV1xx411c7mD") is True
    assert is_bilibili_source("https://b23.tv/abc123") is True
    assert is_bilibili_source("七里香 周杰伦") is False


def test_resolves_bvid_and_rejects_plain_keyword():
    assert resolve_bilibili_video_url("请播放 BV1xx411c7mD") == "https://www.bilibili.com/video/BV1xx411c7mD"
    assert resolve_bilibili_video_url("https://b23.tv/abc123") == "https://b23.tv/abc123"
    try:
        resolve_bilibili_video_url("七里香 周杰伦")
    except ValueError as exc:
        assert "BV" in str(exc)
    else:
        raise AssertionError("plain keyword must not become an implicit Bilibili search")
~~~

- [ ] **Step 2: 运行测试，确认模块尚不存在**

Run: python -m pytest tests/test_bilibili_source.py -v

Expected: FAIL，提示 ModuleNotFoundError: No module named bilibili_source。

- [ ] **Step 3: 实现模块和依赖**

~~~python
# bilibili_source.py（公开接口和关键分支）
_BV_RE = re.compile(r"\b(BV[0-9A-Za-z]+)\b", re.IGNORECASE)


def is_bilibili_source(value: str) -> bool:
    text = str(value or "").strip()
    host = urlparse(text).netloc.casefold()
    return bool(_BV_RE.search(text)) or host == "b23.tv" or host.endswith(".b23.tv") or "bilibili.com" in host


def resolve_bilibili_video_url(value: str) -> str:
    text = str(value or "").strip()
    match = _BV_RE.search(text)
    if match:
        return f"https://www.bilibili.com/video/{match.group(1)}"
    parsed = urlparse(text)
    host = parsed.netloc.casefold()
    if host == "b23.tv" or host.endswith(".b23.tv"):
        return text
    if "bilibili.com" in host and "/video/" in parsed.path:
        return text
    raise ValueError("B站来源必须是 BV 号、b23.tv 短链或 bilibili.com/video 链接")
~~~

实现 WBI 签名时，从 /x/web-interface/nav 的 wbi_img 读取两个文件名密钥，用官方 64 位重排表导出 32 位 mixin key；先请求 {"search_type": "video", "keyword": keyword, "page": 1, "tids": 3}，结果为空时请求去掉 tids 的同类参数。每个结果规范化为：

~~~python
{"title": clean_title, "artist": author, "cover": https_cover_or_empty, "url": f"https://www.bilibili.com/video/{bvid}"}
~~~

download_bilibili_audio 使用 YoutubeDL 的 bestaudio/best、noplaylist=True 和 FFmpegExtractAudio WAV 后处理器；结束后必须确认 destination.with_suffix(".wav") 存在且非空。

在 requirements.txt 追加：

~~~text
yt-dlp>=2025.1.15
~~~

创建 requirements-dev.txt：

~~~text
pytest>=8.0,<9.0
~~~

- [ ] **Step 4: 扩充测试以验证音乐分区和输出规范化**

~~~python
def test_search_prefers_music_partition_and_normalizes_cover():
    from bilibili_source import search_bilibili_videos

    calls = []

    class Response:
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): pass
        def json(self): return self.payload

    class Session:
        def get(self, url, **kwargs):
            calls.append((url, kwargs.get("params")))
            if url.endswith("/nav"):
                return Response({"data": {"wbi_img": {
                    "img_url": "https://i0.hdslb.com/0123456789abcdef0123456789abcdef.png",
                    "sub_url": "https://i0.hdslb.com/fedcba9876543210fedcba9876543210.png"}}})
            if url.endswith("/"):
                return Response({})
            return Response({"code": 0, "data": {"result": [{
                "bvid": "BV1xx411c7mD", "title": "<em>七里香</em>",
                "author": "周杰伦", "pic": "//i0.hdslb.com/cover.jpg"}]}})

    results = search_bilibili_videos("七里香", session=Session())
    assert calls[2][1]["tids"] == 3
    assert results == [{
        "title": "七里香", "artist": "周杰伦",
        "cover": "https://i0.hdslb.com/cover.jpg",
        "url": "https://www.bilibili.com/video/BV1xx411c7mD"}]
~~~

- [ ] **Step 5: 运行模块测试并提交**

Run: python -m pytest tests/test_bilibili_source.py -v

Expected: PASS，所有链接识别、关键词拒绝和音乐分区结果测试通过。

~~~bash
git add bilibili_source.py tests/test_bilibili_source.py requirements.txt requirements-dev.txt
git commit -m "feat: add Bilibili source support"
~~~

### Task 2: 扩展来源解析与保留 /convert 兼容性

**Files:**

- Modify: app.py:1-45, 643-754, 999-1109
- Create: tests/test_convert_sources.py

**Interfaces:**

- Consumes: Task 1 的 B 站接口、已有 _resolve_target_audio、_validate_target_file 和 _call_soulx。
- Produces: _resolve_audio_source(source, status_callback=None) -> Path、_resolve_input_audio(source, upload, role, status_callback=None) -> Path、convert(song_name_src, model_dropdown, prompt_vocal_sep=False, target_vocal_sep=True, auto_shift=True, auto_mix_acc=True, pitch_shift=0, n_step=32, cfg=1.0, seed=42, random_seed=False, target_upload=None, reference_source="", reference_upload=None, progress=gr.Progress()) -> tuple[str, str]。
- Used by: Task 3 的转换页绑定和既有 AstrBot /convert 客户端。

- [ ] **Step 1: 写出来源优先级与参数兼容性测试**

~~~python
# tests/test_convert_sources.py
import inspect
from pathlib import Path
import app


def test_convert_keeps_legacy_parameter_prefix():
    assert list(inspect.signature(app.convert).parameters)[:11] == [
        "song_name_src", "model_dropdown", "prompt_vocal_sep", "target_vocal_sep",
        "auto_shift", "auto_mix_acc", "pitch_shift", "n_step", "cfg", "seed", "random_seed",
    ]


def test_uploaded_input_wins_over_text(monkeypatch, tmp_path):
    upload = tmp_path / "reference.wav"
    upload.write_bytes(b"RIFFxxxxWAVE")
    monkeypatch.setattr(app, "_validate_target_file", lambda path: Path(path))
    monkeypatch.setattr(app, "_resolve_audio_source",
                        lambda *_: (_ for _ in ()).throw(AssertionError("text should not be read")))
    assert app._resolve_input_audio("BV1xx411c7mD", str(upload), "参考音色") == upload


def test_explicit_bilibili_source_uses_bilibili_downloader(monkeypatch, tmp_path):
    expected = tmp_path / "bilibili.wav"
    monkeypatch.setattr(app, "_resolve_bilibili_audio", lambda source, callback=None: expected)
    assert app._resolve_audio_source("BV1xx411c7mD") == expected
~~~

- [ ] **Step 2: 运行测试，确认新接口尚不存在**

Run: python -m pytest tests/test_convert_sources.py -v

Expected: FAIL，提示缺少 _resolve_input_audio，或 convert 参数列表尚未包含末尾可选参数。

- [ ] **Step 3: 实现明确 B 站解析、输入优先级和缓存描述符**

~~~python
def _resolve_audio_source(source: str, status_callback=None) -> Path:
    if is_bilibili_source(source):
        return _resolve_bilibili_audio(source, status_callback)
    return _resolve_target_audio(source, status_callback)


def _resolve_input_audio(source: str | None, upload: str | None, role: str, status_callback=None) -> Path:
    if upload:
        return _validate_target_file(Path(upload))
    if str(source or "").strip():
        return _resolve_audio_source(str(source), status_callback)
    raise ValueError(f"请提供{role}来源或上传{role}文件")


def _prompt_descriptor(path: Path, label: str) -> dict[str, str]:
    return {"profile_id": label, "audio_path": str(path.resolve()), "audio_sha256": _sha256_file(path)}
~~~

在 _resolve_bilibili_audio 中以规范化视频 URL 的 SHA-256 前 24 位命名 downloads/bilibili_<key>.wav。复用 _validate_downloaded_audio 和 _touch_and_trim_downloads，并把 FFMPEG_PATH.parent 传入 Task 1 的下载器。对普通文本绝不调用 search_bilibili_videos。

在 convert 中按以下顺序解析：

~~~python
target_path = _resolve_input_audio(song_name_src, target_upload, "目标歌曲", source_progress)
if reference_upload or str(reference_source or "").strip():
    prompt_path = _resolve_input_audio(reference_source, reference_upload, "参考音色", source_progress)
    prompt = _prompt_descriptor(prompt_path, "external-reference")
else:
    prompt = _find_profile(str(model_dropdown).strip())
    prompt_path = Path(prompt["audio_path"])
~~~

将 _cache_key 的第二个参数语义改为 prompt，但保留其 audio_sha256 字段；缓存参数中使用 prompt["profile_id"]，从而让外部参考音频内容参与缓存键。所有旧任务仍传 profile 字典，行为不变。

在 random_seed 后追加：

~~~python
target_upload: str | None = None,
reference_source: str = "",
reference_upload: str | None = None,
~~~

并保持 progress 为最后一个带 gr.Progress() 默认值的参数。对外部参考，继续使用现有 prompt_vocal_sep 控制；对 profile 也保持当前行为。无需修改 _call_soulx 的参数或端点列表。

- [ ] **Step 4: 加入缺少参考、外部参考与旧调用的测试**

~~~python
def test_external_reference_becomes_soulx_prompt(monkeypatch, tmp_path):
    target = tmp_path / "target.wav"; target.write_bytes(b"RIFFxxxxWAVE")
    prompt = tmp_path / "prompt.wav"; prompt.write_bytes(b"RIFFxxxxWAVE")
    result = tmp_path / "result.wav"; result.write_bytes(b"RIFFxxxxWAVE")
    paths = iter([target, prompt])
    called = []
    monkeypatch.setattr(app, "_resolve_input_audio", lambda *args, **kwargs: next(paths))
    monkeypatch.setattr(app, "_validate_parameters", lambda *args: (0, 1, 1.0, 42))
    monkeypatch.setattr(app, "_soulx_asset_fingerprint", lambda: {})
    monkeypatch.setitem(app.CONFIG["cache"], "enabled", False)
    monkeypatch.setattr(app, "_call_soulx", lambda *args: (called.append(args) or (result, "/soulx_svc_convert_path", tmp_path)))
    monkeypatch.setattr(app, "_export_mp3", lambda source, destination: destination)
    monkeypatch.setattr(app, "_cleanup_soulx_download_dir", lambda path: None)
    app.convert("target", "", reference_source="reference")
    assert called[0][0] == prompt
    assert called[0][1] == target
~~~

再加入没有上传、文本且 model_dropdown="" 的调用，断言异常文本包含“参考音色”。

- [ ] **Step 5: 运行来源与回归测试并提交**

Run: python -m pytest tests/test_bilibili_source.py tests/test_convert_sources.py -v

Expected: PASS，旧参数前缀、上传优先级、显式 B 站解析和外部参考调用均通过。

~~~bash
git add app.py tests/test_convert_sources.py
git commit -m "feat: accept target and reference sources"
~~~

### Task 3: 搜索服务与现有转换页面重构

**Files:**

- Modify: app.py:1-45, 565-669, 1111-1195
- Create: tests/test_song_search.py

**Interfaces:**

- Consumes: Task 1 的 search_bilibili_videos，现有 _get_public_json、CONFIG["download"]["timeout_seconds"]。
- Produces: _search_netease_songs(keyword) -> list[dict[str, str]]、search_song_catalog(platform, keyword) -> list[dict[str, str]]、search_song_catalog_ui(platform, keyword) -> tuple[list[dict[str, str]], str, dict, str, str]。
- Used by: 新“歌曲搜索”Gradio 标签页；不参与自动下载或 SoulX 推理。

- [ ] **Step 1: 写出搜索结果和 HTML 复制控件的测试**

~~~python
# tests/test_song_search.py
import app


def test_search_routes_bilibili_to_music_search(monkeypatch):
    expected = [{"title": "七里香", "artist": "周杰伦", "cover": "",
                 "url": "https://www.bilibili.com/video/BV1xx411c7mD"}]
    monkeypatch.setattr(app, "search_bilibili_videos", lambda keyword: expected)
    assert app.search_song_catalog("B站", "七里香") == expected


def test_result_cards_escape_title_and_include_copy_button():
    cards = app._catalog_cards_html([{"title": "<script>", "artist": "作者",
                                      "cover": "", "url": "https://example.com/a"}])
    assert "&lt;script&gt;" in cards
    assert 'data-copy-url="https://example.com/a"' in cards
    assert "navigator.clipboard.writeText" in cards


def test_search_ui_returns_first_result_as_copyable_link(monkeypatch):
    monkeypatch.setattr(app, "search_song_catalog", lambda *_: [{
        "title": "歌", "artist": "人", "cover": "",
        "url": "https://music.163.com/#/song?id=1"}])
    _state, _cards, update, _cover, link = app.search_song_catalog_ui("网易云", "歌")
    assert link == "https://music.163.com/#/song?id=1"
    assert update["value"] == link
~~~

- [ ] **Step 2: 运行测试，确认搜索接口尚不存在**

Run: python -m pytest tests/test_song_search.py -v

Expected: FAIL，提示 search_song_catalog 或 _catalog_cards_html 未定义。

- [ ] **Step 3: 实现网易云搜索、统一结果与安全渲染**

~~~python
def search_song_catalog(platform: str, keyword: str) -> list[dict[str, str]]:
    if platform == "B站":
        return search_bilibili_videos(keyword)
    if platform == "网易云":
        return _search_netease_songs(keyword)
    raise ValueError("请选择 B站 或 网易云")
~~~

先在 app.py 顶部新增 import html。_search_netease_songs 调用既有安全 JSON 获取器，URL 为 https://api.vkeys.cn/v2/music/netease?word=<urlencode keyword>，最多保留 10 条唯一 ID；每项返回 title、artist、cover 与 https://music.163.com/#/song?id=<id>。

实现 _catalog_url、_catalog_label、_catalog_cover_html、_catalog_cards_html、_selected_catalog_result 和 search_song_catalog_ui。对标题、作者、封面 URL、链接使用 html.escape(..., quote=True)，封面 img 使用 referrerpolicy="no-referrer"。每个有效结果卡加入：

~~~html
<button type="button" data-copy-url="https://example.com/video" onclick="navigator.clipboard.writeText(this.dataset.copyUrl)">复制链接</button>
~~~

搜索空结果抛出 ValueError(f"{platform} 没有找到与“{keyword}”匹配的结果")；B 站 WBI 请求拒绝异常要保留其“B站搜索失败”上下文，不得改写为“没有视频”。

- [ ] **Step 4: 修改现有转换页并新增搜索标签**

在 build_app() 中将当前单页包裹进 gr.Tabs()，但保持所有现有高级开关、输出和 api_name="convert"。新增组件及绑定输入按此顺序排列：

~~~python
target_source = gr.Textbox(label="目标歌曲来源（B站 BV/链接、网易云、HTTP 或本地路径）")
target_upload = gr.Audio(label="目标歌曲本地上传（优先）", type="filepath")
reference_source = gr.Textbox(label="参考音色来源（B站 BV/链接、网易云、HTTP 或本地路径）")
reference_upload = gr.Audio(label="参考音色本地上传（优先）", type="filepath")
model_dropdown = gr.Dropdown(
    choices=choices,
    value=default_profile,
    label="已保存参考音色（未填写参考来源时使用）",
    allow_custom_value=True,
)
~~~

run_button.click 仍绑定 convert 和 api_name="convert"，输入列表必须精确为：

~~~python
inputs=[
    target_source, model_dropdown, prompt_vocal_sep, target_vocal_sep,
    auto_shift, auto_mix_acc, pitch_shift, n_step, cfg, seed, random_seed,
    target_upload, reference_source, reference_upload,
]
~~~

上传组件默认值为 None、参考文本默认值为空，使 Gradio 客户端在旧 11 参数调用时可填充默认值。

第二个标签页包含 gr.Radio(["B站", "网易云"])、关键词框、搜索按钮、gr.State([])、HTML 结果卡、结果下拉框、只读且带 buttons=["copy"] 的链接框和封面 HTML。搜索按钮绑定 search_song_catalog_ui，选择变化绑定 _selected_catalog_result；两者均不写入转换组件、不启动转换。

- [ ] **Step 5: 运行全部测试并提交**

Run: python -m pytest tests -v

Expected: PASS，所有 B 站、来源优先级、API 签名、网易云搜索、HTML 转义和复制链接测试通过。

~~~bash
git add app.py tests/test_song_search.py
git commit -m "feat: add song search interface"
~~~

### Task 4: 文档、API 契约检查与运行验证

**Files:**

- Modify: README.md
- Create: tests/test_api_contract.py

**Interfaces:**

- Consumes: Task 2 的 convert 签名和 Task 3 的 search_song_catalog。
- Produces: 面向用户的来源格式、优先级、旧插件兼容说明和搜索页使用说明。
- Used by: 现有 AstrBot 用户与 PR 审阅者。

- [ ] **Step 1: 写出 API 契约测试**

~~~python
# tests/test_api_contract.py
import inspect
import app


def test_convert_legacy_prefix_and_optional_extensions():
    parameters = list(inspect.signature(app.convert).parameters.values())
    assert [parameter.name for parameter in parameters[:11]] == [
        "song_name_src", "model_dropdown", "prompt_vocal_sep", "target_vocal_sep",
        "auto_shift", "auto_mix_acc", "pitch_shift", "n_step", "cfg", "seed", "random_seed",
    ]
    assert [parameter.name for parameter in parameters[11:14]] == [
        "target_upload", "reference_source", "reference_upload"]
    assert [parameter.default for parameter in parameters[11:14]] == [None, "", None]
~~~

- [ ] **Step 2: 运行契约测试，确认最终签名符合预期**

Run: python -m pytest tests/test_api_contract.py -v

Expected: PASS，后追加参数均具有默认值，旧 11 参数前缀未发生变化。

- [ ] **Step 3: 更新 README**

在“参考音色”后新增“来源输入与搜索”章节，明确：

~~~text
目标歌曲与参考音色均可使用 B站 BV/视频链接、网易云 ID/链接、HTTP(S) 音频链接、本地路径或页面上传。
参考优先级：上传 > 参考来源 > voice_profiles 下拉框。
转换输入框不接受 B站关键词；请先到“歌曲搜索”标签页搜索并复制链接。
~~~

在 API 章节保留原 11 参数代码块，紧接着列出三个追加可选参数及默认值；说明已部署的 AstrBot 插件可不作改动。说明搜索页返回的是链接发现功能，不会下载、填表或启动任务。

- [ ] **Step 4: 全量检查与手动冒烟验证**

Run: python -m pytest tests -v && python -m compileall -q app.py bilibili_source.py

Expected: PASS，pytest 无失败、compileall 无输出。

用 启动SVCVC-API.bat 启动后，依次人工确认：

1. 仅选择 voice_profiles 音色并输入旧网易云来源，旧流程正常显示并开始转换。
2. 输入 B 站目标 BV 号和 B 站参考 BV 号，日志分别显示 B 站音频下载与普通 SoulX 转换调用。
3. 在 B 站音乐区和网易云各搜索一次；结果展示标题、作者、封面或无封面占位，复制链接可粘贴回目标/参考来源框。
4. 输入普通 B 站关键词到转换来源框，界面提示必须使用 BV/视频链接，且日志没有发出 B 站搜索请求。

- [ ] **Step 5: 提交文档与验证文件**

~~~bash
git add README.md tests/test_api_contract.py
git commit -m "docs: describe dual source conversion"
~~~
