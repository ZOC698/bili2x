import os
import sys
import json
import time
import subprocess
import requests
import yaml
from pathlib import Path

# 解决 Windows 命令行默认 GBK 导致 emoji 崩溃问题
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 确保引入当前目录
CURRENT_DIR = Path(__file__).parent
CONFIG_PATH = CURRENT_DIR / "config.json"

def disable_quick_edit():
    """禁用 Windows 控制台快速编辑模式，防止鼠标点击终端窗口挂起暂停程序执行"""
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            h_stdin = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE = -10
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(h_stdin, ctypes.byref(mode)):
                # ENABLE_QUICK_EDIT_MODE = 0x0040, ENABLE_EXTENDED_FLAGS = 0x0080
                new_mode = (mode.value & ~0x0040) | 0x0080
                kernel32.SetConsoleMode(h_stdin, new_mode)
        except Exception:
            pass

from bili2x_config import load_config, get_api_key

def verify_video_file(video_path, max_duration=300):
    """使用 ffprobe 严格检查视频流完整性与时长"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=width,height,codec_type",
        "-of", "json", str(video_path)
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise ValueError(f"视频文件损坏或未完全下载完成: {res.stderr}")
        
    data = json.loads(res.stdout)
    duration = float(data.get("format", {}).get("duration", 0))
    if duration <= 0:
        raise ValueError("视频时长异常 (<= 0s)")
    if duration > max_duration:
        print(f"[提示] 视频时长 ({duration:.1f}s) 超过设定上限 ({max_duration}s)，跳过处理以避免占用资源。")
        return None
        
    streams = data.get("streams", [])
    width, height = 0, 0
    has_audio = False
    for s in streams:
        if s.get("codec_type") == "video" and not width:
            width = s.get("width", 0)
            height = s.get("height", 0)
        elif s.get("codec_type") == "audio":
            has_audio = True
            
    if not has_audio:
        print("[提示] 视频不含音频流，跳过识别。")
        return None
        
    return {
        "duration": round(duration, 1),
        "resolution": f"{width}x{height}" if width and height else "未知"
    }

def format_timestamp(seconds: float) -> str:
    # Round the complete duration so milliseconds carry into seconds/minutes.
    milliseconds = max(0, round(seconds * 1000))
    hours, remaining = divmod(milliseconds, 3600000)
    minutes, remaining = divmod(remaining, 60000)
    secs, msec = divmod(remaining, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{msec:03d}"

# 自动检测并载入 nvidia pip 库中的 CUDA 12 DLLs (cublas, cudnn 等)
def setup_cuda_dll_paths():
    try:
        import site
        site_dirs = site.getsitepackages()
        for s in site_dirs:
            nvidia_dir = Path(s) / "nvidia"
            if nvidia_dir.exists():
                for sub in ["cublas", "cudnn", "cuda_nvrtc"]:
                    bin_dir = nvidia_dir / sub / "bin"
                    if bin_dir.exists():
                        try:
                            os.add_dll_directory(str(bin_dir))
                        except Exception:
                            pass
                        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass

setup_cuda_dll_paths()

def transcribe_audio(video_path, config):
    """Whisper 语音识别：输出结构化字典，带 GPU 失败自动降级 CPU 保护"""
    from faster_whisper import WhisperModel
    import gc
    
    device = config.get("whisper_device", "cuda")
    compute_type = config.get("whisper_compute_type", "float16")
    model_name = config.get("whisper_model", "small")
    
    vad_filter = config.get("whisper_vad_filter", True)
    vad_threshold = float(config.get("whisper_vad_threshold", 0.2))
    initial_prompt = config.get("whisper_initial_prompt", "咕嘎，咿呜，小家伙，小妈咪，撞头了")
    
    transcribe_kwargs = {
        "language": "zh",
        "vad_filter": vad_filter,
    }
    if vad_filter:
        transcribe_kwargs["vad_parameters"] = dict(
            threshold=vad_threshold,
            min_speech_duration_ms=100,
            min_silence_duration_ms=300
        )
    if initial_prompt:
        transcribe_kwargs["initial_prompt"] = initial_prompt

    print(f"[1/4] 正在加载 Whisper ({model_name} on {device})...")
    try:
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        print(f"[2/4] 正在进行音频转写 (VAD灵敏度: {vad_threshold}, 提示词引导已启用)...")
        segments, _ = model.transcribe(str(video_path), **transcribe_kwargs)
        segments_list = list(segments)
    except Exception as e:
        print(f"[提示] GPU 模式触发异常 ({e})，正在自动无缝切换到 CPU 模式继续...")
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        print(f"[2/4] (CPU 模式) 正在进行音频转写 (VAD灵敏度: {vad_threshold})...")
        segments, _ = model.transcribe(str(video_path), **transcribe_kwargs)
        segments_list = list(segments)
    
    items = []
    for idx, seg in enumerate(segments_list, start=1):
        text = seg.text.strip()
        if text:
            items.append({
                "id": idx,
                "start": seg.start,
                "end": seg.end,
                "start_str": format_timestamp(seg.start),
                "end_str": format_timestamp(seg.end),
                "zh": text
            })
            
    del model
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
        
    return items

def translate_texts_with_llm(items, api_key, config):
    """
    【根据 GPT 审阅优化】：
    只发送纯文本 JSON 给大模型，时间戳由本地保留并回填，杜绝格式错乱！
    同时翻译为【自然英语】与【地道二次元日语】双语字幕！
    """
    print(f"[3/4] 正在调用 DeepSeek 英日双语翻译 ({len(items)} 条台词)...")
    payload_input = [{"id": it["id"], "text": it["zh"]} for it in items]
    
    prompt = f"""你是一名专业的二次元短视频及动漫字幕译员，精通中、英、日三语互译。
请将以下 JSON 列表中的原视频台词分别转换为【英文 (en)】与【日文 (ja)】双语字幕。

【核心语言处理规则 - 严格执行】：
1. 【只翻译中文内容，绝不能输出任何中文】：
   - 最终输出中，"en" 字段必须为英文，"ja" 字段必须为日文，严禁在任何输出字段中出现中文！
   - 原文为中文时：分别翻译为自然口语化的英文与地道动漫风格的日文。
2. 【对于原视频中出现的其他语言（英语、日语、其他外语）】：
   - 严禁将原视频里的英语或日语翻译成中文！其他语言一律不管，绝不反向汉化！
   - 如果原文本身就是英文（如 "Swingin' beer! Let me down!"、"Hello" 等）：
     * "en" 字段必须直接保留原英文文本，严禁改动；
     * "ja" 字段请将其自然翻译为日文。
   - 如果原文本身就是日文（如「すごい！」、「かわいい」等）：
     * "ja" 字段必须直接保留原日文文本；
     * "en" 字段请将其自然翻译为英文。
   - 如果原文为中英/中日混合：只翻译其中的中文部分，英文部分保留在 "en"，日文部分保留在 "ja"，严禁反向翻译回中文。
3. 【二次元与萌系语气拟声词】：
   - 特色叫声及拟声词（如“咕嘎”、“咿呜”、“嘿嘿”等）：
     * 英文：生动拟声（如 *Guga!*、*Eee-woo~*、*Nom nom* 等）
     * 日文：动漫片假名/拟声词（如「グガッ！」、「イィウ〜」、「えへへ」等）
4. 【严格 JSON 输出格式】：
   严格返回合法的纯 JSON 数组，格式为:
   [
     {{"id": 1, "en": "English text", "ja": "日本語テキスト"}},
     ...
   ]
5. 严禁改变 id 编号，不要包含任何 markdown 代码块标记，不要包含多余解说。

待处理台词内容：
{json.dumps(payload_input, ensure_ascii=False, indent=2)}
"""
    api_base = config.get("deepseek_api_base", "https://api.deepseek.com").rstrip("/")
    model_name = config.get("deepseek_model", "deepseek-chat")
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    body = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": "You are a professional subtitle translator that outputs pure JSON."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3
    }
    
    resp = requests.post(f"{api_base}/chat/completions", json=body, headers=headers, timeout=60)
    resp.raise_for_status()
    raw_content = resp.json()["choices"][0]["message"]["content"].strip()
    
    # 清洗可能存在的 Markdown 标记
    if raw_content.startswith("```"):
        raw_lines = raw_content.splitlines()
        if raw_lines[0].startswith("```"):
            raw_lines = raw_lines[1:]
        if raw_lines and raw_lines[-1].strip() == "```":
            raw_lines = raw_lines[:-1]
        raw_content = "\n".join(raw_lines).strip()
        
    # Reject incomplete/ambiguous responses instead of publishing Chinese fallbacks.
    try:
        translated_list = json.loads(raw_content)
        if not isinstance(translated_list, list):
            raise ValueError("expected a JSON array")
        expected = {it["id"] for it in items}
        translations = {}
        for item in translated_list:
            if not isinstance(item, dict) or type(item.get("id")) is not int:
                raise ValueError("invalid subtitle id")
            idx = item["id"]
            if idx not in expected or idx in translations:
                raise ValueError("unexpected or duplicate subtitle id")
            for language in ("en", "ja"):
                if not isinstance(item.get(language), str) or not item[language].strip():
                    raise ValueError("missing translation")
                # Keep each translated language on one SRT line.
                item[language] = " ".join(item[language].split())
            translations[idx] = item
        if set(translations) != expected:
            raise ValueError("missing subtitle ids")
        en_map = {idx: it["en"] for idx, it in translations.items()}
        ja_map = {idx: it["ja"] for idx, it in translations.items()}
    except (ValueError, TypeError, KeyError):
        raise ValueError("翻译响应不完整或格式无效；未生成成品视频，中文字幕已保留。") from None

    # 本地回填生成完整的中、英、日及英日双语 SRT
    zh_srt_lines = []
    en_srt_lines = []
    ja_srt_lines = []
    bilingual_srt_lines = []
    
    for it in items:
        idx = it["id"]
        time_header = f"{it['start_str']} --> {it['end_str']}"
        zh_srt_lines.append(f"{idx}\n{time_header}\n{it['zh']}\n")
        
        en_text = en_map[idx].strip()
        ja_text = ja_map[idx].strip()
        
        en_srt_lines.append(f"{idx}\n{time_header}\n{en_text}\n")
        ja_srt_lines.append(f"{idx}\n{time_header}\n{ja_text}\n")
        
        # 双语字幕：第一行英文，第二行日文（紧凑小字号置顶贴边）
        if en_text and ja_text:
            bi_text = f"{en_text}\n{ja_text}"
        else:
            bi_text = en_text or ja_text or it["zh"]
            
        bilingual_srt_lines.append(f"{idx}\n{time_header}\n{bi_text}\n")
        it["en"] = en_text
        it["ja"] = ja_text
        
    return "\n".join(zh_srt_lines), "\n".join(en_srt_lines), "\n".join(ja_srt_lines), "\n".join(bilingual_srt_lines), items

def get_aspect_ratio_filter(video_path):
    """
    检查并规范化视频宽高比与像素比 (SAR)：
    解决非标分辨率 (如 1882x1080) 或奇异像素比 (如 17090:17093) 导致 X/Twitter 或播放器挤压变形为竖屏的问题。
    统一规范化输出像素比 setsar=1，且智能补齐标准 16:9 / 9:16 / 1:1 画布，彻底杜绝拉伸变形。
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=width,height,sample_aspect_ratio",
        "-select_streams", "v:0", "-of", "json", str(video_path)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        data = json.loads(res.stdout)
        s = data.get("streams", [{}])[0]
        w = int(s.get("width", 0))
        h = int(s.get("height", 0))
        if not w or not h:
            return "setsar=1"
        
        ratio = w / h
        # 横屏且接近 16:9 (1.5 ~ 2.0)
        if 1.5 <= ratio <= 2.0:
            tw, th = (1920, 1080) if max(w, h) >= 1000 else (1280, 720)
            return f"scale={tw}:{th}:force_original_aspect_ratio=decrease,pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
        # 竖屏且接近 9:16 (0.5 ~ 0.67)
        elif 0.5 <= ratio <= 0.67:
            tw, th = (1080, 1920) if max(w, h) >= 1000 else (720, 1280)
            return f"scale={tw}:{th}:force_original_aspect_ratio=decrease,pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
        # 方形接近 1:1 (0.9 ~ 1.1)
        elif 0.9 <= ratio <= 1.1:
            dim = 1080 if max(w, h) >= 900 else 720
            return f"scale={dim}:{dim}:force_original_aspect_ratio=decrease,pad={dim}:{dim}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
        else:
            # 其他比例 (如 4:3, 21:9 等)，保证偶数宽高并强制像素比为 1:1
            return "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1"
    except Exception:
        return "setsar=1"

def burn_subtitles_nvenc(video_path, srt_path, output_path, config=None):
    """Preserve the configured style/encoders and omit source metadata."""
    import shutil
    import tempfile

    cfg = config or {}
    style = ",".join(f"{name}={cfg.get(key, default)}" for name, key, default in (
        ("Alignment", "subtitle_alignment", 6),
        ("FontSize", "subtitle_fontsize", 8),
        ("MarginV", "subtitle_margin_v", 4),
        ("BorderStyle", "subtitle_border_style", 1),
        ("Outline", "subtitle_outline", 0),
        ("Shadow", "subtitle_shadow", 0),
        ("PrimaryColour", "subtitle_primary_colour", "&H33FFFFFF"),
    ))
    video_path = Path(video_path).resolve()
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    print("[4/4] 正在按当前配置压制英日双语字幕...")
    # A constant filter filename avoids injecting quotes/colons from user paths.
    with tempfile.TemporaryDirectory(prefix="bili2x-") as temp_dir:
        vf_style = get_aspect_ratio_filter(video_path)
        if srt_path is not None:
            shutil.copyfile(srt_path, Path(temp_dir) / "captions.srt")
            vf_style += f",subtitles=filename=captions.srt:force_style='{style}'"
        metadata = ["-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1"]
        base = ["ffmpeg", "-y", "-i", str(video_path), "-vf", vf_style]
        # Write to a temporary artifact; interrupted jobs cannot leave a false success.
        temporary_output = Path(temp_dir) / ("encoded" + output_path.suffix)
        cmd_nvenc = base + ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23",
                            "-c:a", "copy"] + metadata + [str(temporary_output)]
        res = subprocess.run(cmd_nvenc, capture_output=True, cwd=temp_dir)
        if res.returncode != 0:
            print("[提示] NVENC 未成功，回退到 CPU 编码 (libx264)...")
            cmd_cpu = base + ["-c:v", "libx264", "-crf", "22", "-preset", "fast",
                              "-c:a", "copy"] + metadata + [str(temporary_output)]
            result = subprocess.run(cmd_cpu, capture_output=True, cwd=temp_dir)
            if result.returncode != 0:
                raise RuntimeError("FFmpeg 编码失败；请检查输入、音频格式和字幕滤镜。")
        # Replace on the destination volume only after the complete encode succeeds.
        import os
        import uuid
        staging = output_path.with_name(".bili2x-" + uuid.uuid4().hex + output_path.suffix)
        try:
            shutil.copyfile(temporary_output, staging)
            os.replace(staging, output_path)
        finally:
            staging.unlink(missing_ok=True)
    return output_path

def append_to_history(history_file, video_title, video_info, items, config=None):
    """将处理记录写成 Markdown，供 DSH 原生直接查阅"""
    history_path = Path(history_file)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    
    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) if (config or {}).get("history_include_wall_clock", False) else "processed"
    lines = [
        f"## [{now_str}] {video_title}",
        f"- **规格**：时长 {video_info.get('duration', 0)} 秒 | 分辨率 {video_info.get('resolution', '未知')}",
        f"- **产出**：`[EN_JA]_{video_title}.mp4` 及配套中/英/日多语字幕",
        "",
        "### 中英日三语字幕对照摘要",
        "```srt"
    ]
    for it in items:
        lines.append(f"{it['id']}")
        lines.append(f"{it['start_str']} --> {it['end_str']}")
        lines.append(f"中: {it['zh']}")
        lines.append(f"英: {it.get('en', '')}")
        lines.append(f"日: {it.get('ja', '')}")
        lines.append("")
    lines.extend(["```", "", "---", ""])
    
    with open(history_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[归档] 记录已追加至: {history_path}")

def notify_user(title, message):
    """Pass notification text as data; filenames must not become PowerShell code."""
    if sys.platform != "win32":
        return
    try:
        env = os.environ.copy()
        env["BILI2X_NOTICE_TITLE"] = title
        env["BILI2X_NOTICE_MESSAGE"] = message
        ps_cmd = """
        Add-Type -AssemblyName System.Windows.Forms
        $notify = New-Object System.Windows.Forms.NotifyIcon
        try {
            $notify.Icon = [System.Drawing.SystemIcons]::Information
            $notify.Visible = $true
            $notify.ShowBalloonTip(4000, $env:BILI2X_NOTICE_TITLE,
                $env:BILI2X_NOTICE_MESSAGE, [System.Windows.Forms.ToolTipIcon]::Info)
        } finally { $notify.Dispose() }
        """
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], env=env,
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def main():
    disable_quick_edit()
    if len(sys.argv) < 2:
        print("用法: python bili2x_worker.py <视频文件路径>")
        sys.exit(1)
        
    video_path = Path(sys.argv[1])
    if not video_path.exists():
        print(f"错误: 找不到视频文件 {video_path}")
        sys.exit(1)
        
    config = load_config()
    out_dir = Path(config.get("output_dir", CURRENT_DIR / "output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    
    sub_dir = Path(config.get("subtitles_dir", CURRENT_DIR / "subtitles"))
    sub_dir.mkdir(parents=True, exist_ok=True)
    
    video_title = video_path.stem
    print(f"\n================ [Worker 独立进程] 开始处理: {video_title} ================")
    start_time = time.time()
    
    # 1. 验证文件完整性
    max_sec = config.get("max_duration_seconds", 300) # 支持长达 5 分钟 (300 秒) 视频
    v_info = verify_video_file(video_path, max_sec)
    if not v_info:
        sys.exit(0)
        
    # 2. Whisper 语音提取
    items = transcribe_audio(video_path, config)
    output_video_path = out_dir / f"[EN_JA]_{video_path.name}"
    history_file = config.get("history_file", str(CURRENT_DIR / "history.md"))
    
    if not items:
        print("[提示] 未识别到台词；按当前画幅规则输出无字幕视频。")
        burn_subtitles_nvenc(video_path, None, output_video_path, config)
        append_to_history(history_file, video_title, v_info, [], config)
        notify_user("Bili2X 转制成功", "无字幕视频已输出至 output 目录。")
        return

    # Save source subtitles before calling the API, including failed translations.
    (sub_dir / f"{video_title}.zh.srt").write_text("\n".join(
        f"{it['id']}\n{it['start_str']} --> {it['end_str']}\n{it['zh']}\n"
        for it in items), encoding="utf-8")

    # 3. DeepSeek 纯文本 JSON 英日双语翻译 + 本地时间戳回填
    api_key = get_api_key(config)
    zh_srt, en_srt, ja_srt, bi_srt, enriched_items = translate_texts_with_llm(items, api_key, config)
    
    # 保存中/英/日及双语 srt 到独立字幕库 (保证 output 目录纯净只有成品视频)
    (sub_dir / f"{video_title}.zh.srt").write_text(zh_srt, encoding="utf-8")
    (sub_dir / f"{video_title}.en.srt").write_text(en_srt, encoding="utf-8")
    (sub_dir / f"{video_title}.ja.srt").write_text(ja_srt, encoding="utf-8")
    bi_srt_path = sub_dir / f"{video_title}.bilingual.srt"
    bi_srt_path.write_text(bi_srt, encoding="utf-8")
    
    # 4. ffmpeg NVENC 置顶贴边压制英日双语字幕
    output_video_path = out_dir / f"[EN_JA]_{video_path.name}"
    burn_subtitles_nvenc(video_path, bi_srt_path, output_video_path, config)
    
    # 5. 追加至 history.md
    history_file = config.get("history_file", str(CURRENT_DIR / "history.md"))
    append_to_history(history_file, video_title, v_info, enriched_items, config)
    
    elapsed = round(time.time() - start_time, 1)
    print(f"\n[Worker] 视频转制成功！总耗时: {elapsed} 秒")
    print(f"[输出] 纯净成品视频 (供发布X): {output_video_path}")
    print(f"[输出] 独立字幕文件 (中/英/日归档): {sub_dir}")
    
    notify_user("Bili2X 转制成功", f"《{video_title}》英日双语字幕压制完成，已存入 output 目录！(耗时 {elapsed}s)")

if __name__ == "__main__":
    main()
