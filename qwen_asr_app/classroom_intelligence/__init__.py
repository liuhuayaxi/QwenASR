"""Classroom Intelligence Package: Scribbles, Background Note Enhancement, and Evidence Verification."""
from __future__ import annotations

from .export import export_notes_markdown
from .routes import classroom_intelligence_router
from .schema import verify_claims
from .service import (
    ClassroomIntelligenceService,
    configure_intelligence,
    on_stable_transcript,
)
from .settings import (
    DEFAULT_INTELLIGENCE_PROVIDER,
    get_intelligence_settings,
    save_intelligence_settings,
)
from .store import ClassroomNoteStore

__all__ = [
    'classroom_intelligence_router',
    'ClassroomIntelligenceService',
    'ClassroomNoteStore',
    'get_intelligence_settings',
    'save_intelligence_settings',
    'verify_claims',
    'export_notes_markdown',
    'DEFAULT_INTELLIGENCE_PROVIDER',
    'configure_intelligence',
    'on_stable_transcript',
]
