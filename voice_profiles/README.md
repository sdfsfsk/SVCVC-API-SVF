# 参考音色库

把 5～30 秒的参考音频直接放在此目录。支持：`.wav`、`.flac`、`.mp3`、`.ogg`、`.m4a`、`.aac`。

音频文件名默认就是 `profile_id` 和显示名称。例如 `松子.wav` 会显示为 `松子`。

可以添加同名 JSON 覆盖信息：

```json
{
  "profile_id": "matsuko",
  "display_name": "松子",
  "description": "干净女声参考",
  "prompt_vocal_sep": false
}
```

建议直接存放干净、无伴奏、无混响的单人演唱，此时 `prompt_vocal_sep` 保持关闭。替换音频后 SHA256 会改变，旧结果缓存不会误命中。
