import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bili2x_config as config_module
import bili2x_worker as worker


class SafetyTests(unittest.TestCase):
    def test_timestamps_carry_milliseconds(self):
        self.assertEqual(worker.format_timestamp(59.9996), "00:01:00,000")
        self.assertEqual(worker.format_timestamp(3599.9996), "01:00:00,000")
        self.assertEqual(worker.format_timestamp(2.15), "00:00:02,150")

    def test_portable_paths_and_private_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text(json.dumps({"inbox_dir": "inbox", "subtitle_alignment": 6}))
            (root / "config.local.json").write_text(json.dumps({"output_dir": "private-output"}))
            config = config_module.load_config(root / "config.json")
            self.assertEqual(config["inbox_dir"], str(root / "inbox"))
            self.assertEqual(config["output_dir"], str(root / "private-output"))
            self.assertEqual(config["subtitle_alignment"], 6)

    def test_env_key_precedes_local_config(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "unit-test-placeholder"}):
            self.assertEqual(config_module.get_api_key({"deepseek_api_key": "other-placeholder"}),
                             "unit-test-placeholder")

    def translate(self, response):
        items = [{"id": 1, "start_str": "00:00:01,000", "end_str": "00:00:02,000", "zh": "你好"}]
        with patch.object(worker.requests, "post") as post, contextlib.redirect_stdout(io.StringIO()):
            post.return_value.json.return_value = {"choices": [{"message": {"content": json.dumps(response)}}]}
            result = worker.translate_texts_with_llm(items, "unit-test-placeholder", {})
            sent = post.call_args.kwargs["json"]["messages"][1]["content"]
            self.assertNotIn("00:00:01,000", sent)
            self.assertEqual(post.call_args.kwargs["json"]["temperature"], 0.3)
            self.assertEqual(post.call_args.kwargs["timeout"], 60)
            return result

    def test_translation_preserves_local_timing(self):
        result = self.translate([{"id": 1, "en": "Hello", "ja": "こんにちは"}])
        self.assertIn("00:00:01,000 --> 00:00:02,000", result[3])
        self.assertIn("Hello\nこんにちは", result[3])

    def test_translation_rejects_incomplete_duplicate_or_invalid_responses(self):
        good = {"id": 1, "en": "Hello", "ja": "こんにちは"}
        for response in ([], [good, good], [{"id": 2, "en": "Hello", "ja": "こんにちは"}],
                         [{"id": 1, "en": "", "ja": "こんにちは"}], {"id": 1},
                         [{"id": "1", "en": "Hello", "ja": "こんにちは"}]):
            with self.subTest(response=response), self.assertRaises(ValueError):
                self.translate(response)

    def test_history_omits_computer_calendar_time(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(worker.time, "strftime") as clock:
            path = Path(directory) / "history.md"
            with contextlib.redirect_stdout(io.StringIO()):
                worker.append_to_history(path, "sample", {"duration": 2}, [])
            clock.assert_not_called()
            self.assertIn("[processed]", path.read_text(encoding="utf-8"))

    def test_notifications_pass_title_as_data(self):
        malicious = "sample'; throw 'unexpected"
        with patch.object(worker.sys, "platform", "win32"), patch.object(worker.subprocess, "run") as run:
            worker.notify_user(malicious, malicious)
        args, kwargs = run.call_args
        self.assertNotIn(malicious, args[0][-1])
        self.assertEqual(kwargs["env"]["BILI2X_NOTICE_TITLE"], malicious)

    def test_failed_encoder_does_not_create_false_completed_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            srt = root / "captions with ' quotes.srt"
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
            output = root / "result.mp4"
            with patch.object(worker.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                worker.burn_subtitles_nvenc(root / "input.mp4", srt, output, {})
            self.assertFalse(output.exists())

    def test_nvenc_and_cpu_preserve_parameters_and_remove_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            srt = root / "captions with ' quotes.srt"
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                self.assertIn("setsar=1,subtitles=filename=captions.srt:force_style='Alignment=6,FontSize=8,MarginV=4", command[5])
                self.assertNotIn(str(srt), command[5])
                self.assertTrue((Path(kwargs["cwd"]) / "captions.srt").exists())
                self.assertIn("-map_metadata:s", command)
                self.assertIn("-map_chapters", command)
                if len(calls) == 1:
                    return subprocess.CompletedProcess(command, 1)
                Path(command[-1]).write_bytes(b"encoded-test-placeholder")
                return subprocess.CompletedProcess(command, 0)
            with patch.object(worker.subprocess, "run", side_effect=run), \
                    patch.object(worker, "get_aspect_ratio_filter", return_value="setsar=1"), \
                    contextlib.redirect_stdout(io.StringIO()):
                output = worker.burn_subtitles_nvenc(root / "input.mp4", srt, root / "result.mp4", {})
            self.assertEqual(output.read_bytes(), b"encoded-test-placeholder")
            self.assertIn("h264_nvenc", calls[0])
            self.assertIn("p4", calls[0])
            self.assertIn("23", calls[0])
            self.assertIn("libx264", calls[1])
            self.assertIn("22", calls[1])
            self.assertIn("fast", calls[1])

    def test_silent_video_keeps_normalization_without_subtitles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def run(command, **kwargs):
                self.assertEqual(command[5], "setsar=1")
                self.assertIn("-map_metadata:s", command)
                Path(command[-1]).write_bytes(b"encoded-test-placeholder")
                return subprocess.CompletedProcess(command, 0)
            with patch.object(worker.subprocess, "run", side_effect=run), \
                    patch.object(worker, "get_aspect_ratio_filter", return_value="setsar=1"), \
                    contextlib.redirect_stdout(io.StringIO()):
                worker.burn_subtitles_nvenc(root / "input.mp4", None, root / "result.mp4", {})
            self.assertTrue((root / "result.mp4").exists())


if __name__ == "__main__":
    unittest.main()
