# Bili2X — 中文视频英日双语字幕流水线

监听下载收件箱，使用 faster-whisper 识别语音，通过 DeepSeek 翻译为英文和日文，保留本地时间轴，用 FFmpeg 压制双语字幕。每个视频在独立工作进程中串行处理，输出视频和四种 SRT，并生成本地 Markdown 记录。

此仓库发布的是脱敏后的源代码副本，不包含 API Key、原始视频、生成字幕、处理历史、个人目录、设备型号或 Antigravity 会话。当前识别、翻译和编码参数已保留。项目名称来自早期 Grok 方案评估，实际翻译服务是 DeepSeek。

## 安装与运行

需要 Python 3.11，以及可在 PATH 中运行的 `ffmpeg` 和 `ffprobe`。FFmpeg 需要支持 `subtitles` / libass；日文显示需要可用的日文字体。

在 Windows PowerShell 中执行：

```powershell
git clone https://github.com/ZOC698/bili2x.git
cd bili2x
powershell -NoProfile -ExecutionPolicy Bypass -File .\Setup_Bili2X.ps1 -Gpu
$env:DEEPSEEK_API_KEY = Read-Host 'DeepSeek API Key' -MaskInput
.\Start_Bili2X_Console.bat
```

`-MaskInput` 需要 PowerShell 7.1 或更高版本。Windows PowerShell 5.1 可改为：

```powershell
$secureKey = Read-Host 'DeepSeek API Key' -AsSecureString
$credential = [System.Management.Automation.PSCredential]::new('api', $secureKey)
$env:DEEPSEEK_API_KEY = $credential.GetNetworkCredential().Password
```

使用 CPU 环境时省略 `-Gpu`，并在本地覆盖文件里设置 `whisper_device` 为 `cpu`、`whisper_compute_type` 为 `int8`。默认仍保留 `cuda` / `float16`，GPU 失败时尝试 CPU。GPU 库要求参见 [faster-whisper 官方说明](https://github.com/SYSTRAN/faster-whisper#gpu)。

将视频下载到 `inbox/` 即可。启动时会扫描没有同名成品的存量视频。也可单独运行：

```powershell
.\.venv\Scripts\python.exe .\bili2x_worker.py '.\inbox\sample.mp4'
```

双击 `Start_Bili2X_Silent.vbs` 可静默启动。用 `Stop_Bili2X.bat` 终止当前仓库路径下的监听器、工作进程及其子进程。仅启动一个监听器，避免重复处理；停止正在编码的任务可能留下忽略的临时文件。

## 配置与密钥

`config.json` 是可公开的默认参数。相对路径固定相对于配置文件所在目录解析，不依赖启动位置。`config.local.json` 可覆盖任意参数，已加入 Git 忽略；其中个人路径和凭据不得提交。

密钥读取顺序为：`DEEPSEEK_API_KEY` 环境变量、合并后配置中的 `deepseek_api_key`、本机 `~/.dsh/.credentials.yaml` 的 `refs.DEEPSEEK_API_KEY`。默认公共密钥为空。`.env.example` 仅作说明，程序不会自动读取 `.env`。

本地覆盖文件示例：

```json
{
  "inbox_dir": "inbox",
  "output_dir": "output",
  "subtitles_dir": "subtitles",
  "deepseek_api_key": ""
}
```

| 配置项 | 保留值 |
| --- | --- |
| Whisper 模型 / 设备 / 精度 | `small` / `cuda` / `float16` |
| 最长视频 | `140` 秒（本项目处理上限） |
| VAD / 阈值 | `true` / `0.2` |
| Whisper 初始提示词 | `咕嘎，咿呜，小家伙，小妈咪，撞头了` |
| VAD 最短语音 / 静音 | `100` / `300` 毫秒 |
| ASR 语言 | `zh` |
| 翻译 API / 模型 | `https://api.deepseek.com` / `deepseek-chat` |
| 翻译 temperature / 请求超时 | `0.3` / `60` 秒 |
| 对齐 / 字号 / 纵边距 | `6` / `8` / `4` |
| 边框 / 描边 / 阴影 | `1` / `0` / `0` |
| 主颜色 | `&H33FFFFFF` |
| NVENC 编码 | `h264_nvenc`, preset `p4`, CQ `23` |
| CPU 编码回退 | `libx264`, CRF `22`, preset `fast` |
| 音频 | copy |

保留了当前工作脚本新增的画幅规范化规则（16:9 / 9:16 / 1:1 补边、`setsar=1`）以及未识别到台词时输出无字幕视频的分支。此规则可能放大或补边原画面；具体局限见审阅报告。

注意：保留的 `subtitle_alignment=6` 在 ASS 中是右侧居中。若要顶部居中，应自行在本地改为 `8`。原说明混用了旧 SSA 编号和 ASS 编号，本次仅修正文档，保留实际参数。参见 [libass 格式规范](https://github.com/libass/libass/wiki/ASS-File-Format-Guide#v4-ass-style)和 [Aegisub 对齐规则](https://aegisub.org/docs/latest/ass_tags/#line-alignment)。

## 隐私与审阅

- 所有原有媒体、字幕、运行历史、虚拟环境和一次性调试文件均未发布。
- 本地历史默认不写电脑的日历时间；视频内部 SRT 时间轴保留。需要日历时间时可在私有覆盖文件中设置 `history_include_wall_clock: true`。
- 编码时禁用原文件全局/流元数据和章节的自动复制，避免沿用源文件的日期、位置、标题或设备标签。参见 [FFmpeg 文档](https://www.ffmpeg.org/ffmpeg.html#Metadata)。封装器/编码器仍可能生成通用技术标签；画面、声音、文件名中的个人信息需要另行处理。
- 程序发送给翻译服务的是字幕序号和台词文本，不发送本地路径、电脑日历时间、设备标识或视频二进制。台词本身仍会发送到配置的服务，代码默认不做内容匿名化。
- 翻译响应需要完整且唯一的 id，以及非空 en/ja。失败会停止生成成品，并保留中文字幕；不再悄悄回退成中文。此检查不等同于语言或译文质量验证。
- 通知文本以环境变量传入 PowerShell，文件标题不再被拼入脚本；字幕滤镜使用固定临时文件名，避开文件路径中的引号和特殊符号。

审阅结论、保留参数和验证范围见 [REVIEW.md](REVIEW.md)。

## 本地检查

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts\check_publication.py
```

该项目不会自动发布到 X。没有附带许可文件；公开可见本身不授予再分发许可。
