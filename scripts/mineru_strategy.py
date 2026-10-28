"""Choose a local MinerU tier from a document's cheap PDF signals.

The automatic mode is deliberately conservative: native-text documents use
Standard, while scans and pages dominated by raster images use Advanced. An
explicit tier always wins, and a failed probe falls back to Advanced so a
quality setting cannot silently become a fast setting because classification
was unavailable.
"""
from __future__ import annotations

from pathlib import Path


def choose_local_tier(source: Path, requested: str) -> tuple[str, dict]:
    requested = str(requested or "standard").strip().lower()
    if requested != "auto":
        return requested, {"mode": "manual", "requested_tier": requested}

    evidence = {"mode": "auto", "requested_tier": "auto"}
    document = None
    try:
        from docvortex.document.pdf import PDFDocument

        document = PDFDocument(Path(source).read_bytes())
        classification = document.classify()
        sample_count = min(document.page_count, 10)
        image_heavy_pages = 0
        image_coverages = []
        for page_index in range(sample_count):
            width, height = document.page_size(page_index)
            page_area = max(1.0, float(width) * float(height))
            boxes = document.get_page_image_bboxes(page_index)
            image_area = sum(
                max(0.0, float(x2) - float(x1)) * max(0.0, float(y2) - float(y1))
                for x1, y1, x2, y2 in boxes
            )
            coverage = min(1.0, image_area / page_area)
            image_coverages.append(coverage)
            if coverage >= 0.35 or len(boxes) >= 20:
                image_heavy_pages += 1

        average_image_coverage = sum(image_coverages) / max(1, len(image_coverages))
        image_heavy_ratio = image_heavy_pages / max(1, sample_count)
        evidence.update(
            classification=classification,
            sampled_pages=sample_count,
            average_image_coverage=round(average_image_coverage, 4),
            image_heavy_ratio=round(image_heavy_ratio, 4),
        )
        if classification == "ocr" or image_heavy_ratio >= 0.5 or average_image_coverage >= 0.35:
            evidence["reason"] = "scan_or_visual_density"
            return "advanced", evidence
        evidence["reason"] = "native_text_layer"
        return "standard", evidence
    except Exception as exc:
        evidence.update(reason="probe_failed", probe_error=type(exc).__name__)
        return "advanced", evidence
    finally:
        if document is not None:
            try:
                document.close()
            except Exception:
                pass
