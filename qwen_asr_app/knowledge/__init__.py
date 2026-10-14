"""Production Mac knowledge store, ingestion, retrieval, chunking, and routes."""
from __future__ import annotations

from .chunking import (
    CHUNKER_VERSION,
    PARSER_VERSION,
    Chunk,
    chunk_markdown_text,
    chunk_pdf_document,
    chunk_transcript_segments,
)
from .routes import knowledge_router
from .service import KnowledgeService
from .store import KnowledgeStore

__all__ = [
    "CHUNKER_VERSION",
    "PARSER_VERSION",
    "Chunk",
    "KnowledgeService",
    "KnowledgeStore",
    "chunk_markdown_text",
    "chunk_pdf_document",
    "chunk_transcript_segments",
    "knowledge_router",
]
