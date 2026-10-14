from __future__ import annotations

import asyncio
import importlib
import os
import struct
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.dont_write_bytecode = True

WEB_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEB_DIR.parent
STARTER_DIR = REPO_ROOT / "workshop" / "starter"
AGENTS_DIR = STARTER_DIR / "agents"
DEPLOY_DIR = STARTER_DIR / "deploy"

CAMPAIGN_PROMPT = "create a campaign to promote fitbit air, for healthy lifestyle"
PDF_OUTPUT_PATHS = (
    REPO_ROOT / "test_output.pdf",
    WEB_DIR / "test_output.pdf",
)
DOWNLOADED_IMAGE_PATHS = (
    (
        REPO_ROOT / "fitbit_air_tranquil_recovery.png",
        "Concept 1: Revised Tranquil Recovery (gs://ssd-instagram-campaign-campaign-images/campaign-images/revised_tranquil_recovery-c82f3e30.png)",
    ),
    (
        REPO_ROOT / "fitbit_air_minimalist_essential.png",
        "Concept 2: Revised Minimalist Essential (gs://ssd-instagram-campaign-campaign-images/campaign-images/revised_minimalist_essential-af22b3f3.png)",
    ),
    (
        REPO_ROOT / "fitbit_air_campaign_image.png",
        "Concept 3: Revised Unplugged Movement (gs://ssd-instagram-campaign-campaign-images/campaign-images/revised_unplugged_movement-47f3e899.png)",
    ),
)


def _extract_png_info_and_idat(png_path: Path) -> tuple[int, int, int, int, bytes]:
    """Parse a PNG file and return (width, height, bit_depth, colors, concatenated_idat_bytes)."""
    data = png_path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Not a valid PNG file: {png_path}")
    pos = 8
    width = height = bit_depth = color_type = 0
    idat_chunks: list[bytes] = []
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        chunk_type = data[pos + 4 : pos + 8]
        chunk_data = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", chunk_data[:10])
        elif chunk_type == b"IDAT":
            idat_chunks.append(chunk_data)
        elif chunk_type == b"IEND":
            break
    colors = 3 if color_type == 2 else (1 if color_type == 0 else 3)
    return width, height, bit_depth, colors, b"".join(idat_chunks)


def _sanitize_pdf_text(text: str) -> str:
    """Normalize text for PDF Type1 Helvetica encoding and escape PDF string delimiters."""
    replacements = {
        "✓": "[OK]",
        "⚠️": "[WARN]",
        "❌": "[ERROR]",
        "🤖": "[Agent]",
        "🎨": "[Studio]",
        "📸": "[Images]",
        "🚀": "[Launch]",
        "📊": "[Strategy]",
        "✍️": "[Copy]",
        "🔍": "[Review]",
        "📅": "[Timeline]",
        "🔗": "[Link]",
        "—": "-",
        "–": "-",
        "→": "->",
        "•": "*",
        "“": '"',
        "”": '"',
        "’": "'",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    ascii_text = text.encode("ascii", errors="replace").decode("ascii")
    return ascii_text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def write_pdf_document(
    lines: list[str],
    output_path: Path,
    images: list[tuple[Path, str]] | None = None,
) -> None:
    """Write a standards-compliant multi-page PDF 1.4 document with text and embedded PNG images."""
    wrapped_lines: list[str] = []
    for raw_line in lines:
        for sub_line in raw_line.splitlines() or [""]:
            if not sub_line.strip():
                wrapped_lines.append("")
            else:
                wrapped_lines.extend(
                    textwrap.wrap(
                        sub_line,
                        width=88,
                        break_on_hyphens=False,
                        break_long_words=False,
                    )
                    or [""]
                )

    while wrapped_lines and not wrapped_lines[-1].strip():
        wrapped_lines.pop()

    lines_per_page = 48
    pages_lines = [
        wrapped_lines[i : i + lines_per_page]
        for i in range(0, max(len(wrapped_lines), 1), lines_per_page)
    ]

    objects: dict[int, bytes] = {}
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    page_obj_ids: list[int] = []
    next_obj_id = 4

    for page_chunk in pages_lines:
        page_id = next_obj_id
        content_id = next_obj_id + 1
        next_obj_id += 2
        page_obj_ids.append(page_id)

        stream_cmds = ["BT", "/F1 10 Tf", "14 TL", "45 745 Td"]
        for idx, line in enumerate(page_chunk):
            escaped = _sanitize_pdf_text(line)
            if idx == 0:
                stream_cmds.append(f"({escaped}) Tj")
            else:
                stream_cmds.append(f"T* ({escaped}) Tj")
        stream_cmds.append("ET")
        stream_data = "\n".join(stream_cmds).encode("ascii")

        objects[content_id] = (
            f"<< /Length {len(stream_data)} >>\nstream\n".encode("ascii")
            + stream_data
            + b"\nendstream"
        )
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode("ascii")

    # Add dedicated image pages for each downloaded campaign image
    for img_path, caption in images or []:
        if not img_path.exists():
            continue
        width, height, bit_depth, colors, idat_bytes = _extract_png_info_and_idat(img_path)
        page_id = next_obj_id
        content_id = next_obj_id + 1
        image_obj_id = next_obj_id + 2
        next_obj_id += 3
        page_obj_ids.append(page_id)

        color_space = "/DeviceRGB" if colors == 3 else "/DeviceGray"
        objects[image_obj_id] = (
            f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
            f"/ColorSpace {color_space} /BitsPerComponent {bit_depth} "
            f"/Filter /FlateDecode "
            f"/DecodeParms << /Predictor 15 /Colors {colors} /BitsPerComponent {bit_depth} /Columns {width} >> "
            f"/Length {len(idat_bytes)} >>\nstream\n".encode("ascii")
            + idat_bytes
            + b"\nendstream"
        )

        # Scale image to fit nicely on a 612x792 Letter page
        max_w, max_h = 420.0, 520.0
        scale = min(max_w / max(width, 1), max_h / max(height, 1))
        draw_w = width * scale
        draw_h = height * scale
        x_pos = (612.0 - draw_w) / 2.0
        y_pos = 160.0

        escaped_caption = _sanitize_pdf_text(caption)
        content_cmds = [
            "BT",
            "/F1 11 Tf",
            "45 735 Td",
            f"({escaped_caption}) Tj",
            "ET",
            "q",
            f"{draw_w:.2f} 0 0 {draw_h:.2f} {x_pos:.2f} {y_pos:.2f} cm",
            "/Im1 Do",
            "Q",
        ]
        stream_data = "\n".join(content_cmds).encode("ascii")
        objects[content_id] = (
            f"<< /Length {len(stream_data)} >>\nstream\n".encode("ascii")
            + stream_data
            + b"\nendstream"
        )
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> /XObject << /Im1 {image_obj_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("ascii")

    kids_str = " ".join(f"{pid} 0 R" for pid in page_obj_ids)
    objects[2] = f"<< /Type /Pages /Kids [{kids_str}] /Count {len(page_obj_ids)} >>".encode("ascii")

    total_objects = next_obj_id - 1
    buf = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}

    for obj_id in range(1, total_objects + 1):
        offsets[obj_id] = len(buf)
        buf.extend(f"{obj_id} 0 obj\n".encode("ascii"))
        buf.extend(objects[obj_id])
        buf.extend(b"\nendobj\n")

    xref_offset = len(buf)
    buf.extend(f"xref\n0 {total_objects + 1}\n".encode("ascii"))
    buf.extend(b"0000000000 65535 f \n")
    for obj_id in range(1, total_objects + 1):
        buf.extend(f"{offsets[obj_id]:010d} 00000 n \n".encode("ascii"))

    buf.extend(
        f"trailer\n<< /Size {total_objects + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    output_path.write_bytes(bytes(buf))

for path_entry in (str(WEB_DIR), str(STARTER_DIR), str(AGENTS_DIR), str(DEPLOY_DIR)):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)


def _install_mock_dependencies() -> None:
    """Install lightweight mock modules for optional cloud/UI packages when running offline."""
    if "streamlit" not in sys.modules:
        st_mod = types.ModuleType("streamlit")
        st_mod.session_state = SimpleNamespace()
        st_mod.cache_resource = lambda func=None, **kwargs: (func if func is not None else (lambda f: f))
        st_mod.set_page_config = MagicMock()
        st_mod.title = MagicMock()
        st_mod.caption = MagicMock()
        st_mod.markdown = MagicMock()
        st_mod.image = MagicMock()
        st_mod.error = MagicMock()
        st_mod.warning = MagicMock()
        st_mod.stop = MagicMock()
        st_mod.rerun = MagicMock()
        st_mod.chat_input = MagicMock(return_value=None)
        st_mod.empty = MagicMock(return_value=MagicMock())
        st_mod.spinner = MagicMock(return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock()))
        st_mod.chat_message = MagicMock(return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock()))
        st_mod.status = MagicMock(return_value=MagicMock())
        st_mod.sidebar = SimpleNamespace(button=MagicMock(return_value=False))
        sys.modules["streamlit"] = st_mod

    if "vertexai" not in sys.modules:
        vertexai_mod = types.ModuleType("vertexai")
        vertexai_mod.init = MagicMock()
        agent_engines_mod = types.ModuleType("vertexai.agent_engines")
        agent_engines_mod.get = MagicMock()
        agent_engines_mod.create = MagicMock()
        agent_engines_mod.AdkApp = MagicMock()
        vertexai_mod.agent_engines = agent_engines_mod
        vertexai_mod.Client = MagicMock()
        sys.modules["vertexai"] = vertexai_mod
        sys.modules["vertexai.agent_engines"] = agent_engines_mod


_install_mock_dependencies()
import app as creator_web_app  # noqa: E402


class SessionStateDict(dict):
    """Dict supporting both attribute and item access like Streamlit's SessionState."""

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value) -> None:
        self[name] = value

    def __delattr__(self, name: str) -> None:
        try:
            del self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class TestGeminiVersionsInCreatorWeb(unittest.TestCase):
    """Verify Gemini version (3.7, 3.8, 4.0) resolution and compatibility in creator-web."""

    GEMINI_VERSIONS = ("3.7", "3.8", "4.0")

    def test_shorthand_versions_resolve_to_flash_models(self):
        expected = {
            "3.7": "gemini-3.7-flash",
            "3.8": "gemini-3.8-flash",
            "4.0": "gemini-4.0-flash",
        }
        for version, expected_model in expected.items():
            with self.subTest(version=version):
                self.assertTrue(creator_web_app.is_supported_gemini_version(version))
                self.assertEqual(creator_web_app.extract_gemini_version(version), version)
                self.assertEqual(creator_web_app.resolve_gemini_model(version), expected_model)

    def test_full_model_names_across_3_7_3_8_4_0(self):
        variants = [
            ("gemini-3.7-flash", "3.7"),
            ("gemini-3.7-pro", "3.7"),
            ("gemini-3.7-flash-preview", "3.7"),
            ("gemini-3.8-flash", "3.8"),
            ("gemini-3.8-pro", "3.8"),
            ("gemini-3.8-flash-001", "3.8"),
            ("gemini-4.0-flash", "4.0"),
            ("gemini-4.0-pro", "4.0"),
            ("gemini-4.0-ultra", "4.0"),
        ]
        for model_name, expected_version in variants:
            with self.subTest(model_name=model_name):
                self.assertEqual(creator_web_app.extract_gemini_version(model_name), expected_version)
                self.assertTrue(creator_web_app.is_supported_gemini_version(model_name))
                self.assertEqual(creator_web_app.resolve_gemini_model(model_name), model_name)

    def test_env_var_resolution_for_each_gemini_version(self):
        for version in self.GEMINI_VERSIONS:
            with self.subTest(version=version):
                model_id = f"gemini-{version}-flash"
                with patch.dict(os.environ, {"GEMINI_MODEL": model_id}, clear=False):
                    self.assertEqual(creator_web_app.resolve_gemini_model(), model_id)

                with patch.dict(os.environ, {"GEMINI_MODEL": "", "GEMINI_VERSION": version}, clear=False):
                    self.assertEqual(creator_web_app.resolve_gemini_model(), model_id)

    def test_unsupported_gemini_versions_rejected(self):
        unsupported = ["1.5", "2.0", "gemini-1.5-pro", "gemini-2.0-flash", "gpt-4o", "invalid"]
        for invalid_value in unsupported:
            with self.subTest(invalid_value=invalid_value):
                self.assertFalse(creator_web_app.is_supported_gemini_version(invalid_value))
                with self.assertRaises(ValueError):
                    creator_web_app.resolve_gemini_model(invalid_value)


class TestGcsPathHandlingInCreatorWeb(unittest.TestCase):
    """Verify GCS URI validation, parsing, MIME inference, and extraction from text and file_data."""

    def test_valid_gcs_uris(self):
        valid_uris = [
            "gs://ssd-instagram-campaign-images/campaign-images/post1_concept_a-1a2b3c4d.png",
            "gs://my-project-campaign-images/campaign-images/caption2-9f8e7d6c.jpg",
            "gs://bucket.with.dots-123/nested/path/v3.8/image_asset.webp",
            "gs://creative-studio-40/assets/hero.jpeg",
        ]
        for uri in valid_uris:
            with self.subTest(uri=uri):
                self.assertTrue(creator_web_app.is_valid_gcs_uri(uri))
                bucket, blob = creator_web_app.parse_gcs_uri(uri)
                self.assertTrue(bucket)
                self.assertTrue(blob)
                self.assertEqual(f"gs://{bucket}/{blob}", uri)

    def test_invalid_gcs_uris(self):
        invalid_uris = [
            None,
            "",
            "https://storage.googleapis.com/bucket/image.png",
            "http://example.com/image.png",
            "gs://",
            "gs:///missing-bucket.png",
            "gs://bucket-only",
            "gs://bucket-only/",
            "gs://UPPERCASE_BUCKET/image.png",
            "gs://bucket//leading-slash.png",
            "gs://bucket/path with spaces/image.png",
        ]
        for uri in invalid_uris:
            with self.subTest(uri=uri):
                self.assertFalse(creator_web_app.is_valid_gcs_uri(uri))
                with self.assertRaises(ValueError):
                    creator_web_app.parse_gcs_uri(uri)  # type: ignore[arg-type]

    def test_infer_image_mime_type(self):
        self.assertEqual(
            creator_web_app.infer_image_mime_type("gs://my-bucket/img.png"),
            "image/png",
        )
        self.assertEqual(
            creator_web_app.infer_image_mime_type("gs://my-bucket/img.jpg"),
            "image/jpeg",
        )
        self.assertEqual(
            creator_web_app.infer_image_mime_type("gs://my-bucket/img.jpeg"),
            "image/jpeg",
        )
        self.assertEqual(
            creator_web_app.infer_image_mime_type("gs://my-bucket/img.webp"),
            "image/webp",
        )
        self.assertEqual(
            creator_web_app.infer_image_mime_type("gs://my-bucket/file_without_ext", "image/png"),
            "image/png",
        )
        self.assertIsNone(creator_web_app.infer_image_mime_type("gs://my-bucket/report.pdf"))
        self.assertIsNone(creator_web_app.infer_image_mime_type("gs://my-bucket/notes.txt"))

    def test_extract_gcs_uris_from_text_and_markdown(self):
        markdown_text = """
        **For Caption 1: "Hydrate Smarter"**
        - Generated: `gs://ssd-instagram-campaign/campaign-images/caption1_concept_a-11223344.png`
        - Backup visual: [Concept B](gs://ssd-instagram-campaign/campaign-images/caption2_concept_b-55667788.jpg).
        - Raw link in parens (gs://ssd-instagram-campaign/campaign-images/caption3_concept_c-99aabbcc.webp),
        - Duplicate link: gs://ssd-instagram-campaign/campaign-images/caption1_concept_a-11223344.png
        - Non-image artifact: gs://ssd-instagram-campaign/logs/session.json
        """
        extracted = creator_web_app.extract_gcs_uris_from_text(markdown_text, image_only=True)
        self.assertEqual(
            extracted,
            [
                "gs://ssd-instagram-campaign/campaign-images/caption1_concept_a-11223344.png",
                "gs://ssd-instagram-campaign/campaign-images/caption2_concept_b-55667788.jpg",
                "gs://ssd-instagram-campaign/campaign-images/caption3_concept_c-99aabbcc.webp",
            ],
        )

        all_uris = creator_web_app.extract_gcs_uris_from_text(markdown_text, image_only=False)
        self.assertEqual(len(all_uris), 4)
        self.assertIn("gs://ssd-instagram-campaign/logs/session.json", all_uris)


class TestStreamProcessingAcrossGeminiVersions(unittest.TestCase):
    """Verify stream event processing across Gemini 3.7, 3.8, and 4.0 event structures and GCS paths."""

    def test_gemini_3_7_stream_with_dict_events_file_data_and_text_gcs(self):
        """Gemini 3.7: snake_case dict parts with function_call, file_data gs:// URI, inline_data, and text."""
        stream = [
            {
                "content": {
                    "parts": [
                        {"function_call": {"name": "brand_strategist"}},
                        {"text": "✓ Research complete.\n"},
                    ]
                }
            },
            {
                "content": {
                    "parts": [
                        {"function_call": {"name": "designer"}},
                        {
                            "file_data": {
                                "file_uri": "gs://campaign-bucket-37/campaign-images/post1-12345678.png",
                                "mime_type": "image/png",
                            }
                        },
                        {
                            "text": (
                                "✓ Design complete. Image stored at "
                                "gs://campaign-bucket-37/campaign-images/post1-12345678.png and "
                                "gs://campaign-bucket-37/campaign-images/post2-87654321.jpg."
                            )
                        },
                    ]
                }
            },
            {
                "content": {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": b"\x89PNG\r\n\x1a\nfake",
                            }
                        }
                    ]
                }
            },
        ]

        status_mock = MagicMock()
        placeholder_mock = MagicMock()
        rendered_images: list[tuple] = []

        full_text, images = creator_web_app.process_stream_events(
            response_stream=stream,
            status_container=status_mock,
            placeholder=placeholder_mock,
            image_renderer=lambda content, caption=None: rendered_images.append((content, caption)),
        )

        self.assertIn("✓ Research complete.", full_text)
        self.assertIn("✓ Design complete.", full_text)
        self.assertEqual(status_mock.write.call_count, 2)
        status_mock.write.assert_any_call("🤖 Calling specialist: **brand_strategist**...")
        status_mock.write.assert_any_call("🤖 Calling specialist: **designer**...")

        # 2 unique GCS images (post1 deduplicated between file_data and text, post2 from text) + 1 inline image
        self.assertEqual(len(images), 3)
        self.assertEqual(
            images[0],
            {
                "role": "assistant",
                "content": "gs://campaign-bucket-37/campaign-images/post1-12345678.png",
                "is_image": True,
                "caption": "Generated Asset (GCS)",
            },
        )
        self.assertEqual(
            images[1],
            {
                "role": "assistant",
                "content": "gs://campaign-bucket-37/campaign-images/post2-87654321.jpg",
                "is_image": True,
                "caption": "Generated Asset (GCS)",
            },
        )
        self.assertEqual(images[2]["caption"], "Generated Asset")
        self.assertEqual(len(rendered_images), 3)

    def test_gemini_3_8_stream_with_object_parts_camelcase_and_thoughts(self):
        """Gemini 3.8: object attributes & camelCase dicts, thought parts ignored, GCS paths extracted."""
        thought_part = SimpleNamespace(
            thought=True,
            text="Internal reasoning with gs://campaign-bucket-38/campaign-images/internal_only-00000000.png",
        )
        func_part = SimpleNamespace(
            thought=False,
            text=None,
            function_call=SimpleNamespace(name="copywriter"),
            inline_data=None,
            file_data=None,
        )
        camel_file_part = {
            "fileData": {
                "fileUri": "gs://campaign-bucket-38/campaign-images/concept_a-abcdef01.webp",
                "mimeType": "image/webp",
            }
        }
        text_part = SimpleNamespace(
            thought=False,
            text="Generated visual: `gs://campaign-bucket-38/campaign-images/concept_b-abcdef02.png`",
            function_call=None,
            inline_data=None,
            file_data=None,
        )

        stream = [
            SimpleNamespace(content=SimpleNamespace(parts=[thought_part, func_part])),
            {"content": {"parts": [camel_file_part]}},
            SimpleNamespace(content=SimpleNamespace(parts=[text_part])),
        ]

        status_mock = MagicMock()
        placeholder_mock = MagicMock()
        full_text, images = creator_web_app.process_stream_events(
            response_stream=stream,
            status_container=status_mock,
            placeholder=placeholder_mock,
        )

        # Thought text and its internal GCS URI must be excluded
        self.assertNotIn("Internal reasoning", full_text)
        self.assertIn("concept_b-abcdef02.png", full_text)
        status_mock.write.assert_called_once_with("🤖 Calling specialist: **copywriter**...")

        self.assertEqual(
            [img["content"] for img in images],
            [
                "gs://campaign-bucket-38/campaign-images/concept_a-abcdef01.webp",
                "gs://campaign-bucket-38/campaign-images/concept_b-abcdef02.png",
            ],
        )

    def test_gemini_4_0_stream_with_candidates_inferred_mime_and_split_gcs_chunks(self):
        """Gemini 4.0: candidates[0].content.parts, file_data without mime_type, and split text chunks."""
        stream = [
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"functionCall": {"name": "critic"}},
                                {
                                    "file_data": {
                                        "file_uri": "gs://campaign-bucket-40/campaign-images/hero-11112222.png",
                                        # mime_type omitted in event; should be inferred from .png extension
                                    }
                                },
                            ]
                        }
                    }
                ]
            },
            # Split GCS URI across two streaming text events
            "Here is the second image: gs://campaign-bucket-40/campaign-images/",
            "lifestyle-33334444.jpg ready for review.",
            # Fallback event structure with embedded GCS URI
            {"output": "\nAnd third asset: gs://campaign-bucket-40/campaign-images/story-55556666.webp"},
        ]

        status_mock = MagicMock()
        placeholder_mock = MagicMock()
        full_text, images = creator_web_app.process_stream_events(
            response_stream=stream,
            status_container=status_mock,
            placeholder=placeholder_mock,
        )

        status_mock.write.assert_called_once_with("🤖 Calling specialist: **critic**...")
        self.assertIn("gs://campaign-bucket-40/campaign-images/lifestyle-33334444.jpg", full_text)
        self.assertIn("gs://campaign-bucket-40/campaign-images/story-55556666.webp", full_text)
        self.assertEqual(
            [img["content"] for img in images],
            [
                "gs://campaign-bucket-40/campaign-images/hero-11112222.png",
                "gs://campaign-bucket-40/campaign-images/lifestyle-33334444.jpg",
                "gs://campaign-bucket-40/campaign-images/story-55556666.webp",
            ],
        )

    def test_invalid_gcs_uri_in_file_data_is_ignored(self):
        stream = [
            {
                "content": {
                    "parts": [
                        {"file_data": {"file_uri": "gs:///broken_uri.png", "mime_type": "image/png"}},
                        {"file_data": {"file_uri": "gs://valid-bucket/valid.png", "mime_type": "image/png"}},
                    ]
                }
            }
        ]
        _, images = creator_web_app.process_stream_events(stream)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["content"], "gs://valid-bucket/valid.png")


class TestCreatorWebAppExecutionAcrossGeminiVersions(unittest.TestCase):
    """End-to-end UI flow tests for creative-director-web across Gemini 3.7, 3.8, and 4.0."""

    def test_run_app_across_gemini_3_7_3_8_4_0(self):
        pdf_report_lines: list[str] = [
            "==========================================================================",
            "CREATIVE DIRECTOR WEB - MULTI-VERSION GEMINI VERIFICATION REPORT",
            "==========================================================================",
            f"Campaign Prompt: {CAMPAIGN_PROMPT}",
            "Verified Gemini Versions: 3.7 (gemini-3.7-flash), 3.8 (gemini-3.8-flash), 4.0 (gemini-4.0-flash)",
            "",
        ]

        for version in ("3.7", "3.8", "4.0"):
            with self.subTest(gemini_version=version):
                model_name = f"gemini-{version}-flash"
                gcs_asset_1 = (
                    "gs://ssd-instagram-campaign-campaign-images/campaign-images/"
                    "revised_tranquil_recovery-c82f3e30.png"
                )
                gcs_asset_2 = (
                    "gs://ssd-instagram-campaign-campaign-images/campaign-images/"
                    "revised_unplugged_movement-47f3e899.png"
                )

                campaign_output_text = (
                    f"### Fitbit Air Launch Campaign: 'The Invisible Coach' ({model_name})\n\n"
                    f"**1. Market Research & Strategy (brand_strategist):**\n"
                    f"- Target Audience: Minimalist Optimizers, Gen Z Health-First, Longevity Seekers.\n"
                    f"- Competitive Positioning: 'The Invisible Coach' - distraction-free, screen-free AI mentor at $99.\n"
                    f"- Key Trends Leveraged: Digital Detox, Biological Aging (Healthspan), Hyper-Personalized AI Coaching.\n\n"
                    f"**2. Social Media Copy (copywriter):**\n"
                    f"- Post 1 (The Longevity Expert): 3 markers of biological aging that matter more than your step count: "
                    f"HRV, Sleep Quality, Recovery Score. #healthspan #longevity #fitbitair\n"
                    f"- Post 2 (The Minimalist Aesthetic): The best technology is the kind you don't see. "
                    f"A quiet whisper of data in a world of digital noise. #minimalism #digitaldetox #fitbitair\n"
                    f"- Post 3 (The Unplugged Movement): Deep health insights without a single notification to break your flow. "
                    f"#genzwellness #screenfree #healthylifestyle\n\n"
                    f"**3. Visual Concepts & Generated Assets (designer):**\n"
                    f"- Concept 1 (Revised Tranquil Recovery): `{gcs_asset_1}`\n"
                    f"- Concept 3 (Revised Unplugged Movement): `{gcs_asset_2}`\n\n"
                    f"**4. Quality Review (critic):**\n"
                    f"- Posts Score: 9/10 (APPROVED) | Visuals Score: 8.5/10 (APPROVED after Revision Round 1) | All Approved: YES\n\n"
                    f"**5. Project Timeline (project_manager):**\n"
                    f"- Phase 1: Strategy & Research (Oct 1 - Oct 6)\n"
                    f"- Phase 2: Content Creation    (Oct 7 - Oct 11)\n"
                    f"- Phase 3: Review & Revision   (Oct 12 - Oct 14)\n"
                    f"- Phase 4: Launch & Monitoring (Oct 15 - Oct 30) | Notion ID: FB-AIR-2026\n"
                )

                session_state = SessionStateDict()
                mock_agent = MagicMock()
                mock_agent.create_session.return_value = {"id": f"session-{version}"}
                mock_agent.stream_query.return_value = [
                    {
                        "content": {
                            "parts": [
                                {"function_call": {"name": "brand_strategist"}},
                                {"function_call": {"name": "copywriter"}},
                                {"function_call": {"name": "designer"}},
                                {
                                    "file_data": {
                                        "file_uri": gcs_asset_1,
                                        "mime_type": "image/png",
                                    }
                                },
                                {"function_call": {"name": "critic"}},
                                {"function_call": {"name": "project_manager"}},
                                {"text": campaign_output_text},
                            ]
                        }
                    }
                ]

                status_container = MagicMock()
                with patch.dict(os.environ, {"GEMINI_MODEL": model_name}, clear=False), \
                     patch.object(creator_web_app, "get_remote_agent", return_value=mock_agent), \
                     patch.object(creator_web_app.vertexai, "init") as mock_vertex_init, \
                     patch.object(creator_web_app.st, "session_state", session_state), \
                     patch.object(creator_web_app.st, "chat_input", return_value=CAMPAIGN_PROMPT), \
                     patch.object(creator_web_app.st, "status", return_value=status_container), \
                     patch.object(creator_web_app.st, "caption") as mock_caption, \
                     patch.object(creator_web_app.st, "image") as mock_st_image:

                    creator_web_app.run_app()

                    mock_vertex_init.assert_called_once()
                    mock_agent.stream_query.assert_called_once_with(
                        user_id=session_state.user_uuid,
                        session_id=f"session-{version}",
                        message=CAMPAIGN_PROMPT,
                    )
                    self.assertEqual(session_state.agent_session_id, f"session-{version}")
                    caption_arg = mock_caption.call_args[0][0]
                    self.assertIn(model_name, caption_arg)

                    # Messages should include: user prompt, assistant text, and 2 GCS images (file_data + text)
                    self.assertEqual(len(session_state.messages), 4)
                    self.assertEqual(session_state.messages[0]["role"], "user")
                    self.assertEqual(session_state.messages[0]["content"], CAMPAIGN_PROMPT)
                    self.assertEqual(session_state.messages[1]["role"], "assistant")
                    self.assertIn(model_name, session_state.messages[1]["content"])
                    self.assertIn("Fitbit Air", session_state.messages[1]["content"])
                    self.assertTrue(session_state.messages[2]["is_image"])
                    self.assertEqual(session_state.messages[2]["content"], gcs_asset_1)
                    self.assertTrue(session_state.messages[3]["is_image"])
                    self.assertEqual(session_state.messages[3]["content"], gcs_asset_2)
                    self.assertEqual(mock_st_image.call_count, 2)
                    status_container.update.assert_called_once_with(
                        label="✓ Campaign orchestration complete!",
                        state="complete",
                        expanded=False,
                    )

                    specialist_calls = [call.args[0] for call in status_container.write.call_args_list]
                    pdf_report_lines.extend(
                        [
                            "--------------------------------------------------------------------------",
                            f"GEMINI VERSION {version} ({model_name}) - SESSION: {session_state.agent_session_id}",
                            "--------------------------------------------------------------------------",
                            f"User Input: {session_state.messages[0]['content']}",
                            "Specialist Tool Calls:",
                            *[f"  {c}" for c in specialist_calls],
                            "",
                            "Generated Assistant Output:",
                            session_state.messages[1]["content"],
                            "Captured GCS Image Assets:",
                            f"  1. {session_state.messages[2]['content']} ({session_state.messages[2]['caption']})",
                            f"  2. {session_state.messages[3]['content']} ({session_state.messages[3]['caption']})",
                            "",
                        ]
                    )

        # Validate that downloaded GCS images exist and are valid PNGs
        existing_images = [(p, cap) for p, cap in DOWNLOADED_IMAGE_PATHS if p.exists()]
        self.assertGreaterEqual(len(existing_images), 1, "Expected at least one downloaded campaign image")
        for img_path, _ in existing_images:
            width, height, bit_depth, colors, idat = _extract_png_info_and_idat(img_path)
            self.assertGreater(width, 0)
            self.assertGreater(height, 0)
            self.assertEqual(bit_depth, 8)
            self.assertGreater(len(idat), 0)

        for pdf_path in PDF_OUTPUT_PATHS:
            write_pdf_document(pdf_report_lines, pdf_path, images=existing_images)
            self.assertTrue(pdf_path.exists())
            raw_pdf = pdf_path.read_bytes()
            self.assertTrue(raw_pdf.startswith(b"%PDF-1.4"))
            self.assertIn(b"create a campaign to promote fitbit air, for healthy lifestyle", raw_pdf)
            self.assertIn(b"/Subtype /Image", raw_pdf)
            self.assertIn(b"gemini-3.7-flash", raw_pdf)
            self.assertIn(b"gemini-3.8-flash", raw_pdf)
            self.assertIn(b"gemini-4.0-flash", raw_pdf)
            self.assertTrue(raw_pdf.rstrip().endswith(b"%%EOF"))

    def test_creator_web_session_init_variants_and_error_handling(self):
        # Object session with .id attribute
        state_obj = SessionStateDict()
        agent_obj = MagicMock()
        agent_obj.create_session.return_value = SimpleNamespace(id="obj-session-123")
        with patch.object(creator_web_app.st, "session_state", state_obj):
            creator_web_app.init_session_state(agent_obj)
            self.assertEqual(state_obj.agent_session_id, "obj-session-123")

        # Fallback string session when no .id
        state_str = SessionStateDict()
        agent_str = MagicMock()
        agent_str.create_session.return_value = "raw-session-456"
        with patch.object(creator_web_app.st, "session_state", state_str):
            creator_web_app.init_session_state(agent_str)
            self.assertEqual(state_str.agent_session_id, "raw-session-456")

        # Init exception triggers st.error and st.stop
        state_err = SessionStateDict()
        agent_err = MagicMock()
        agent_err.create_session.side_effect = RuntimeError("Engine unavailable")
        with patch.object(creator_web_app.st, "session_state", state_err), \
             patch.object(creator_web_app.st, "error") as mock_error, \
             patch.object(creator_web_app.st, "stop") as mock_stop:
            creator_web_app.init_session_state(agent_err)
            mock_error.assert_called_once()
            mock_stop.assert_called_once()

    def test_creator_web_empty_stream_warning_and_new_campaign_reset(self):
        session_state = SessionStateDict(
            user_uuid="user-1",
            agent_session_id="sess-1",
            messages=[
                {"role": "assistant", "content": "gs://my-bucket/prev.png", "is_image": True, "caption": "Generated Asset (GCS)"},
                {"role": "assistant", "content": "Previous response"},
            ],
        )
        mock_agent = MagicMock()
        mock_agent.stream_query.return_value = []
        status_container = MagicMock()

        with patch.dict(os.environ, {"GEMINI_MODEL": "gemini-4.0-flash"}, clear=False), \
             patch.object(creator_web_app, "get_remote_agent", return_value=mock_agent), \
             patch.object(creator_web_app.vertexai, "init"), \
             patch.object(creator_web_app.st, "session_state", session_state), \
             patch.object(creator_web_app.st, "chat_input", return_value="Empty test"), \
             patch.object(creator_web_app.st, "status", return_value=status_container), \
             patch.object(creator_web_app.st, "warning") as mock_warning, \
             patch.object(creator_web_app.st, "image") as mock_image, \
             patch.object(creator_web_app.st, "rerun") as mock_rerun, \
             patch.object(creator_web_app.st.sidebar, "button", return_value=True):

            creator_web_app.run_app()

            # Historical image was rendered
            mock_image.assert_called_once_with("gs://my-bucket/prev.png", caption="Generated Asset (GCS)")
            # Empty stream triggers warning
            status_container.update.assert_called_once_with(
                label="⚠️ Orchestrator completed with no direct response.",
                state="error",
                expanded=True,
            )
            mock_warning.assert_called_once_with("No text or media was extracted from the response stream.")
            # Sidebar button cleared session_state and triggered rerun
            self.assertEqual(len(session_state), 0)
            mock_rerun.assert_called_once()


class TestBackendAgentsAndGcsToolsAcrossGeminiVersions(unittest.TestCase):
    """Verify backend agents, deploy utilities, and GCS tools across Gemini 3.7, 3.8, and 4.0."""

    GEMINI_MODELS = ("gemini-3.7-flash", "gemini-3.8-flash", "gemini-4.0-flash")

    def setUp(self):
        self._install_backend_mocks()

    def _install_backend_mocks(self):
        """Provide mock google.adk, google.genai, google.cloud.storage, google.auth, and pydantic modules if not installed."""
        if "dotenv" not in sys.modules:
            dotenv_mod = types.ModuleType("dotenv")
            dotenv_mod.load_dotenv = MagicMock(return_value=True)
            sys.modules["dotenv"] = dotenv_mod

        if "pydantic" not in sys.modules:
            import json as _json

            pydantic_mod = types.ModuleType("pydantic")

            class FakeBaseModel:
                def __init__(self, **kwargs):
                    for k, v in kwargs.items():
                        setattr(self, k, v)

                def model_dump(self):
                    return dict(self.__dict__)

                @classmethod
                def model_validate_json(cls, raw_json: str):
                    return cls(**_json.loads(raw_json))

            pydantic_mod.BaseModel = FakeBaseModel
            pydantic_mod.Field = lambda *args, **kwargs: None
            sys.modules["pydantic"] = pydantic_mod

        if "google" not in sys.modules:
            sys.modules["google"] = types.ModuleType("google")

        if "google.auth" not in sys.modules:
            auth_mod = types.ModuleType("google.auth")
            auth_mod.default = MagicMock(return_value=(SimpleNamespace(service_account_email=None), "test-proj"))
            sys.modules["google.auth"] = auth_mod
            setattr(sys.modules["google"], "auth", auth_mod)

        if "google.genai" not in sys.modules:
            genai_mod = types.ModuleType("google.genai")
            types_mod = types.ModuleType("google.genai.types")

            class GenerateContentConfig:
                def __init__(self, **kwargs):
                    self.__dict__.update(kwargs)

            class HttpOptions:
                def __init__(self, **kwargs):
                    self.__dict__.update(kwargs)

            class HttpRetryOptions:
                def __init__(self, **kwargs):
                    self.__dict__.update(kwargs)

            class Part:
                @staticmethod
                def from_bytes(data: bytes, mime_type: str):
                    return SimpleNamespace(data=data, mime_type=mime_type)

                @staticmethod
                def from_uri(file_uri: str, mime_type: str):
                    return SimpleNamespace(file_uri=file_uri, mime_type=mime_type)

            types_mod.GenerateContentConfig = GenerateContentConfig
            types_mod.HttpOptions = HttpOptions
            types_mod.HttpRetryOptions = HttpRetryOptions
            types_mod.Part = Part
            genai_mod.types = types_mod
            genai_mod.Client = MagicMock()
            sys.modules["google.genai"] = genai_mod
            sys.modules["google.genai.types"] = types_mod
            setattr(sys.modules["google"], "genai", genai_mod)

        if "google.adk" not in sys.modules:
            adk_mod = types.ModuleType("google.adk")
            agents_mod = types.ModuleType("google.adk.agents")
            remote_a2a_mod = types.ModuleType("google.adk.agents.remote_a2a_agent")
            plugins_mod = types.ModuleType("google.adk.plugins.logging_plugin")
            tools_mod = types.ModuleType("google.adk.tools")
            agent_tool_mod = types.ModuleType("google.adk.tools.agent_tool")
            google_search_mod = types.ModuleType("google.adk.tools.google_search_tool")
            base_tool_mod = types.ModuleType("google.adk.tools.base_tool")
            tool_context_mod = types.ModuleType("google.adk.tools.tool_context")
            skills_mod = types.ModuleType("google.adk.skills")
            apps_mod = types.ModuleType("google.adk.apps")
            apps_app_mod = types.ModuleType("google.adk.apps.app")
            summarizer_mod = types.ModuleType("google.adk.apps.llm_event_summarizer")
            models_mod = types.ModuleType("google.adk.models")

            class FakeAgent:
                def __init__(self, **kwargs):
                    self.__dict__.update(kwargs)

            class FakeRemoteA2aAgent:
                def __init__(self, **kwargs):
                    self.__dict__.update(kwargs)

            class FakeFunctionTool:
                def __init__(self, func):
                    self.func = func

            class FakeAgentTool:
                def __init__(self, agent):
                    self.agent = agent

            class FakeApp:
                def __init__(self, **kwargs):
                    self.__dict__.update(kwargs)

            class FakeEventsCompactionConfig:
                def __init__(self, **kwargs):
                    self.__dict__.update(kwargs)

            class FakeLlmEventSummarizer:
                def __init__(self, llm):
                    self.llm = llm

            class FakeGemini:
                def __init__(self, model_id: str):
                    self.model_id = model_id

            agents_mod.Agent = FakeAgent
            remote_a2a_mod.RemoteA2aAgent = FakeRemoteA2aAgent
            plugins_mod.LoggingPlugin = MagicMock()
            tools_mod.FunctionTool = FakeFunctionTool
            tools_mod.ToolContext = MagicMock
            tools_mod.skill_toolset = SimpleNamespace(SkillToolset=MagicMock())
            agent_tool_mod.AgentTool = FakeAgentTool
            google_search_mod.google_search = MagicMock()
            base_tool_mod.BaseTool = object
            tool_context_mod.ToolContext = MagicMock
            skills_mod.load_skill_from_dir = MagicMock(return_value="instagram-copywriting-skill")
            apps_mod.App = FakeApp
            apps_app_mod.EventsCompactionConfig = FakeEventsCompactionConfig
            summarizer_mod.LlmEventSummarizer = FakeLlmEventSummarizer
            models_mod.Gemini = FakeGemini

            sys.modules["google.adk"] = adk_mod
            sys.modules["google.adk.agents"] = agents_mod
            sys.modules["google.adk.agents.remote_a2a_agent"] = remote_a2a_mod
            sys.modules["google.adk.plugins"] = types.ModuleType("google.adk.plugins")
            sys.modules["google.adk.plugins.logging_plugin"] = plugins_mod
            sys.modules["google.adk.tools"] = tools_mod
            sys.modules["google.adk.tools.agent_tool"] = agent_tool_mod
            sys.modules["google.adk.tools.google_search_tool"] = google_search_mod
            sys.modules["google.adk.tools.base_tool"] = base_tool_mod
            sys.modules["google.adk.tools.tool_context"] = tool_context_mod
            sys.modules["google.adk.skills"] = skills_mod
            sys.modules["google.adk.apps"] = apps_mod
            sys.modules["google.adk.apps.app"] = apps_app_mod
            sys.modules["google.adk.apps.llm_event_summarizer"] = summarizer_mod
            sys.modules["google.adk.models"] = models_mod
            setattr(sys.modules["google"], "adk", adk_mod)

        if "google.cloud" not in sys.modules:
            cloud_mod = types.ModuleType("google.cloud")
            storage_mod = types.ModuleType("google.cloud.storage")
            storage_mod.Client = MagicMock()
            cloud_mod.storage = storage_mod
            sys.modules["google.cloud"] = cloud_mod
            sys.modules["google.cloud.storage"] = storage_mod
            setattr(sys.modules["google"], "cloud", cloud_mod)

    def test_creative_director_and_specialists_configure_gemini_3_7_3_8_4_0(self):
        from creative_director import agent as cd_agent
        from brand_strategist import agent as bs_agent
        from copywriter import agent as cw_agent
        from designer import agent as ds_agent
        from critic import agent as cr_agent
        from project_manager import agent as pm_agent
        import env_utils

        for model_name in self.GEMINI_MODELS:
            with self.subTest(model_name=model_name):
                env_overrides = {
                    "GEMINI_MODEL": model_name,
                    "GOOGLE_CLOUD_PROJECT": "test-project",
                    "CLOUD_RUN_REGION": "us-central1",
                    "STRATEGIST_AGENT_URL": "https://strategist.run.app",
                    "COPYWRITER_AGENT_URL": "https://copywriter.run.app",
                    "DESIGNER_AGENT_URL": "https://designer.run.app",
                    "CRITIC_AGENT_URL": "https://critic.run.app",
                    "PM_AGENT_URL": "https://pm.run.app",
                }
                with patch.dict(os.environ, env_overrides, clear=False):
                    orchestrator_agent, orchestrator_app = cd_agent.create_creative_director()
                    self.assertEqual(orchestrator_agent.model, model_name)
                    self.assertEqual(
                        orchestrator_app.events_compaction_config.summarizer.llm.model_id,
                        model_name,
                    )
                    # 2 local image tools (display_image, get_image_links) + 5 specialist tools
                    self.assertEqual(len(orchestrator_agent.tools), 7)

                    importlib.reload(bs_agent)
                    importlib.reload(cw_agent)
                    importlib.reload(ds_agent)
                    importlib.reload(cr_agent)
                    pm_instance = pm_agent.create_project_manager_agent()

                    self.assertEqual(bs_agent.root_agent.model, model_name)
                    self.assertEqual(cw_agent.root_agent.model, model_name)
                    self.assertEqual(ds_agent.root_agent.model, model_name)
                    self.assertEqual(cr_agent.root_agent.model, model_name)
                    self.assertEqual(pm_instance.model, model_name)

                    with tempfile.TemporaryDirectory() as tmpdir:
                        missing_env = Path(tmpdir) / ".env.nonexistent"
                        cfg = env_utils.load_env_file(missing_env)
                        self.assertEqual(cfg["GEMINI_MODEL"], model_name)

    def test_designer_generate_image_uploads_to_gcs_and_integrates_with_creator_web(self):
        from designer.image_gen_tool import generate_image

        fake_image_bytes = b"\x89PNG\r\n\x1a\nfake-image-payload"
        fake_part = SimpleNamespace(
            inline_data=SimpleNamespace(data=fake_image_bytes, mime_type="image/png")
        )
        fake_response = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[fake_part]))]
        )

        mock_genai_client = MagicMock()
        mock_genai_client.models.generate_content.return_value = fake_response

        mock_blob = MagicMock()
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_storage_client = MagicMock()
        mock_storage_client.bucket.return_value = mock_bucket

        tool_context = MagicMock()
        tool_context.save_artifact = AsyncMock()

        for version in ("3.7", "3.8", "4.0"):
            with self.subTest(gemini_version=version):
                image_model = f"gemini-{version}-flash-image"
                env = {
                    "GCS_IMAGES_BUCKET": "ssd-instagram-campaign-images",
                    "GOOGLE_CLOUD_PROJECT": "ssd-instagram-campaign",
                    "GOOGLE_CLOUD_LOCATION": "global",
                    "GEMINI_IMAGE_MODEL": image_model,
                }
                with patch.dict(os.environ, env, clear=False), \
                     patch("designer.image_gen_tool.genai.Client", return_value=mock_genai_client), \
                     patch("google.cloud.storage.Client", return_value=mock_storage_client):

                    result = asyncio.run(
                        generate_image(
                            concept_name="post1_concept_a",
                            image_prompt="EcoFlow bottle on mountain trail",
                            aspect_ratio="1:1",
                            tool_context=tool_context,
                        )
                    )

                    self.assertEqual(result["status"], "success")
                    gcs_uri = result["gcs_uri"]
                    self.assertTrue(creator_web_app.is_valid_gcs_uri(gcs_uri))
                    self.assertTrue(
                        gcs_uri.startswith(
                            "gs://ssd-instagram-campaign-images/campaign-images/post1_concept_a-"
                        )
                    )
                    self.assertTrue(gcs_uri.endswith(".png"))

                    # Verify creator-web extracts this GCS path from both file_data and text
                    _, captured_from_text = creator_web_app.process_stream_events(
                        [f"Generated: {gcs_uri}"]
                    )
                    self.assertEqual(len(captured_from_text), 1)
                    self.assertEqual(captured_from_text[0]["content"], gcs_uri)

    def test_display_image_and_get_image_links_tools_handle_gcs_paths(self):
        from creative_director.display_image_tool import display_image
        from creative_director.get_image_links_tool import get_image_links

        gcs_uri = "gs://ssd-instagram-campaign-images/campaign-images/caption1_concept_a-1a2b3c4d.png"

        mock_blob = MagicMock()
        mock_blob.download_as_bytes.return_value = b"fake-png-bytes"
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_storage_client = MagicMock()
        mock_storage_client.bucket.return_value = mock_bucket

        tool_context = MagicMock()
        tool_context.save_artifact = AsyncMock()

        with patch("google.cloud.storage.Client", return_value=mock_storage_client):
            display_res = asyncio.run(
                display_image(gcs_uri=gcs_uri, concept_name="caption1_concept_a", tool_context=tool_context)
            )
            self.assertEqual(display_res, {"status": "success", "concept_name": "caption1_concept_a"})
            mock_storage_client.bucket.assert_called_with("ssd-instagram-campaign-images")
            mock_bucket.blob.assert_called_with("campaign-images/caption1_concept_a-1a2b3c4d.png")
            tool_context.save_artifact.assert_awaited_once()

        # Test get_image_links fallback unsigned HTTPS URL generation
        fake_google_auth = types.ModuleType("google.auth")
        fake_google_auth.default = MagicMock(return_value=(SimpleNamespace(service_account_email=None), "test-proj"))
        with patch.dict(sys.modules, {"google.auth": fake_google_auth}), \
             patch.dict(os.environ, {"SIGNING_SERVICE_ACCOUNT": ""}, clear=False), \
             patch("google.cloud.storage.Client", return_value=mock_storage_client):
            links_res = get_image_links([gcs_uri])
            self.assertEqual(links_res["status"], "success")
            self.assertFalse(links_res["signed"])
            self.assertEqual(
                links_res["links"],
                [
                    {
                        "concept": "caption1_concept_a",
                        "url": (
                            "https://storage.googleapis.com/ssd-instagram-campaign-images/"
                            "campaign-images/caption1_concept_a-1a2b3c4d.png"
                        ),
                    }
                ],
            )

    def test_critic_review_image_with_gcs_uri_across_gemini_3_7_3_8_4_0(self):
        import json as _json
        from critic.image_review_tool import review_image

        fake_review_json = _json.dumps(
            {
                "score": 9,
                "approval_status": "APPROVED",
                "what_works": "Sharp product focus and lighting",
                "issues": "None",
                "suggestions": "None",
            }
        )
        fake_response = SimpleNamespace(text=fake_review_json)
        mock_genai_client = MagicMock()
        mock_genai_client.models.generate_content.return_value = fake_response

        gcs_samples = [
            ("gemini-3.7-flash", "gs://ssd-instagram-campaign-images/campaign-images/concept1-11111111.png", "image/png"),
            ("gemini-3.8-flash", "gs://ssd-instagram-campaign-images/campaign-images/concept2-22222222.jpg", "image/jpeg"),
            ("gemini-4.0-flash", "gs://ssd-instagram-campaign-images/campaign-images/concept3-33333333.webp", "image/webp"),
        ]

        for model_name, gcs_uri, expected_mime in gcs_samples:
            with self.subTest(model_name=model_name, gcs_uri=gcs_uri):
                mock_genai_client.models.generate_content.reset_mock()
                env = {
                    "GOOGLE_CLOUD_PROJECT": "ssd-instagram-campaign",
                    "GOOGLE_CLOUD_LOCATION": "global",
                    "GEMINI_MODEL": model_name,
                }
                with patch.dict(os.environ, env, clear=False), \
                     patch("critic.image_review_tool.genai.Client", return_value=mock_genai_client):
                    res = review_image(
                        gcs_uri=gcs_uri,
                        concept_name="concept_test",
                        campaign_context="EcoFlow smart water bottle",
                    )
                    self.assertEqual(res.status, "success")
                    self.assertEqual(res.score, 9)
                    self.assertEqual(res.approval_status, "APPROVED")

                    call_kwargs = mock_genai_client.models.generate_content.call_args.kwargs
                    self.assertEqual(call_kwargs["model"], model_name)
                    image_part = call_kwargs["contents"][0]
                    self.assertEqual(image_part.file_uri, gcs_uri)
                    self.assertEqual(image_part.mime_type, expected_mime)

    def test_deploy_single_agent_propagates_gemini_versions_and_gcs_bucket(self):
        import deploy_all_specialists

        for version in ("3.7", "3.8", "4.0"):
            model_name = f"gemini-{version}-flash"
            image_model = f"gemini-{version}-flash-image"
            bucket_name = f"ssd-instagram-campaign-v{version.replace('.', '')}"
            with self.subTest(gemini_version=version):
                captured_cmds: list[list[str]] = []

                async def fake_run_cmd(cmd, cwd=None):
                    captured_cmds.append(cmd)
                    return 0, "https://designer-test.us-central1.run.app\n", ""

                env = {
                    "GEMINI_MODEL": model_name,
                    "GEMINI_IMAGE_MODEL": image_model,
                    "GCS_IMAGES_BUCKET": bucket_name,
                    "GOOGLE_CLOUD_LOCATION": "global",
                }
                with patch.dict(os.environ, env, clear=False), \
                     patch.object(deploy_all_specialists, "run_command_async", side_effect=fake_run_cmd):
                    url = asyncio.run(
                        deploy_all_specialists.deploy_single_agent(
                            {"name": "designer", "dir": "designer", "port": 8080},
                            project_id="ssd-instagram-campaign",
                            region="us-central1",
                        )
                    )
                    self.assertEqual(url, "https://designer-test.us-central1.run.app")
                    deploy_cmd = captured_cmds[0]
                    env_flags = [arg for arg in deploy_cmd if arg.startswith("--set-env-vars=")]
                    self.assertEqual(len(env_flags), 1)
                    self.assertIn(f"GEMINI_MODEL={model_name}", env_flags[0])
                    self.assertIn(f"GEMINI_IMAGE_MODEL={image_model}", env_flags[0])
                    self.assertIn(f"GCS_IMAGES_BUCKET={bucket_name}", env_flags[0])


if __name__ == "__main__":
    unittest.main()
