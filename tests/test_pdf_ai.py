from __future__ import annotations

from tests import isolation  # Must precede application imports.

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz

from qwen_asr_app.pdf_ai import (
    ai_pdf_output_path,
    convert_pdf_to_ai,
    validate_pdf_sources,
)


ROOT = Path(__file__).resolve().parent.parent


class PdfAiNamingTests(unittest.TestCase):
    def test_appends_ai_without_changing_original_stem(self) -> None:
        source = Path("/tmp/光电子学-第一章.v2.pdf")
        self.assertEqual(
            ai_pdf_output_path(source).name,
            "光电子学-第一章.v2(AI).pdf",
        )

    def test_validation_rejects_existing_output_and_ai_input(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            source = folder / "讲义.pdf"
            source.write_bytes(b"%PDF-test")
            self.assertEqual(validate_pdf_sources([str(source), str(source)]), [source.resolve()])
            ai_pdf_output_path(source).write_bytes(b"existing")
            with self.assertRaises(FileExistsError):
                validate_pdf_sources([str(source)])
            already_ai = folder / "讲义(AI).pdf"
            already_ai.write_bytes(b"%PDF-test")
            with self.assertRaises(ValueError):
                validate_pdf_sources([str(already_ai)])


class PdfAiPackagingTests(unittest.TestCase):
    def test_builder_keeps_original_page_and_adds_unicode_latex_layer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            source = folder / "公式课件.pdf"
            markdown = folder / "公式课件.md"
            content = folder / "公式课件_content_list.json"
            output = folder / "公式课件(AI).pdf"

            document = fitz.open()
            page = document.new_page(width=600, height=400)
            page.insert_text((50, 80), "Original visual page", fontsize=20)
            document.save(source)
            document.close()
            markdown.write_text("正文 $E=mc^2$", encoding="utf-8")
            content.write_text(
                json.dumps(
                    [
                        {"type": "text", "text": "中文正文", "page_idx": 0},
                        {
                            "type": "equation",
                            "text": "$$\\frac{dF}{dz}=r_{\\text{净}}(h\\nu)$$",
                            "page_idx": 0,
                        },
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_ai_friendly_pdf.py"),
                    "--source",
                    str(source),
                    "--markdown",
                    str(markdown),
                    "--content-list",
                    str(content),
                    "--output",
                    str(output),
                    "--title",
                    source.stem,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            reader = fitz.open(output)
            self.assertEqual(len(reader), 2)
            self.assertIn("AI 友好型 PDF", reader[0].get_text())
            transcript = reader[1].get_text()
            self.assertIn("Original visual page", transcript)
            self.assertIn("[[AI_TRANSCRIPT_PAGE:1]]", transcript)
            self.assertIn("中文正文", transcript)
            self.assertIn(r"\frac{dF}{dz}=r_{\text{净}}(h\nu)", transcript)
            self.assertEqual(
                sorted(reader.embfile_names()),
                ["ai-page-transcript.txt", "mineru-content-list.json", "mineru-transcript.md"],
            )
            reader.close()

    def test_conversion_uses_ocr_formula_profile_and_atomic_target_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            source = folder / "讲义.pdf"
            source.write_bytes(b"%PDF-source")
            calls: list[list[str]] = []
            progress: list[tuple[str, int]] = []

            def fake_run(command: list[str], log_path: Path, *args, **kwargs) -> str:
                calls.append(command)
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("ok", encoding="utf-8")
                if "--content-list" not in command:
                    output_root = Path(command[command.index("-o") + 1]) / "doc" / "hybrid_ocr"
                    output_root.mkdir(parents=True)
                    (output_root / "doc.md").write_text("正文", encoding="utf-8")
                    (output_root / "doc_content_list.json").write_text("[]", encoding="utf-8")
                else:
                    Path(command[command.index("--output") + 1]).write_bytes(b"%PDF-result")
                return "ok"

            with patch(
                "qwen_asr_app.pdf_ai.find_mineru_binary",
                return_value=Path("/usr/bin/true"),
            ), patch("qwen_asr_app.pdf_ai._run_mineru", side_effect=lambda command, log, progress_callback, *args: fake_run(command, log)), patch("qwen_asr_app.pdf_ai._run_checked", side_effect=fake_run):
                result = convert_pdf_to_ai(
                    source,
                    builder_script=ROOT / "scripts" / "build_ai_friendly_pdf.py",
                    python_executable=Path(sys.executable),
                    progress=lambda stage, percent: progress.append((stage, percent)),
                )

            self.assertEqual(result, (folder / "讲义(AI).pdf").resolve())
            self.assertEqual(result.read_bytes(), b"%PDF-result")
            mineru_command = calls[0]
            self.assertIn("ocr", mineru_command)
            self.assertIn("hybrid-engine", mineru_command)
            self.assertIn("--image-analysis", mineru_command)
            self.assertEqual(progress[-1], ("转换完成", 100))


if __name__ == "__main__":
    unittest.main()
