"""KnowledgeService: Mac course knowledge store, ingestion, retrieval, and async job lifecycle."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .ingest import CourseIngestor, compute_file_stat_hash, compute_recording_stat_hash, ensure_course_id
from .jobs import JobManager
from .store import KnowledgeStore


class KnowledgeService:
    def __init__(
        self,
        settings: Any,
        recording_store: Any,
        course_files: Any = None,
        db_path: str | Path | None = None,
        embedder: Any = None,
    ) -> None:
        self.settings = settings
        self.recording_store = recording_store

        if course_files is None:
            try:
                from ..course_files import CourseFiles
                self.course_files = CourseFiles(settings)
            except Exception:
                self.course_files = None
        else:
            self.course_files = course_files

        self._custom_db = db_path is not None
        self._storage_root = Path(settings.storage_root).resolve()
        if db_path is None:
            storage_root = Path(getattr(settings, "storage_root", Path.home() / "Documents" / "QwenASR资料库"))
            db_path = storage_root / ".knowledge.db"

        self.db_path = Path(db_path)
        self.store = KnowledgeStore(self.db_path)
        self.store.exclude_legacy_course_file_variants()
        self.embedder = embedder
        self.last_retrieval_warning = ''
        self.last_vector_success = False

        self.ingestor = CourseIngestor(
            settings=self.settings,
            recording_store=self.recording_store,
            course_files=self.course_files,
            store=self.store,
        )

        self.job_manager = JobManager(store=self.store, sync_fn=self.sync_course,
                                      defer=self._defer_indexing)

    def _defer_indexing(self, category: str) -> bool:
        # Background reindexing must not repeatedly consume changing partial
        # transcripts or time out behind the ASR worker's Metal lease.
        from ..inference_providers import inference_settings
        if inference_settings.for_task('knowledge').get('knowledge_provider', 'local') != 'local':
            return False
        items = getattr(self.recording_store, 'iter_list_items', None)
        if items is not None:
            return any(item.get('status') in {'active', 'recording', 'processing'}
                       for item in items())
        records = getattr(self.recording_store, 'list_records', None)
        if records is None:
            return False
        return any(record.metadata.get('status') in {'active', 'recording', 'processing'}
                   for record in records())

    def _ensure_storage(self):
        root=Path(self.settings.storage_root).resolve()
        if self._custom_db or root==self._storage_root:
            return
        if self.job_manager._active_job_id or self.job_manager._queue.qsize():
            raise ValueError('旧资料库的索引仍在处理，请完成或取消后再切换资料库位置')
        store=KnowledgeStore(root/'.knowledge.db')
        store.exclude_legacy_course_file_variants()
        self.store=store
        self.db_path=store.db_path
        self.ingestor.store=store
        self.job_manager.store=store
        self._storage_root=root
        self.last_vector_success=False
        self.last_retrieval_warning=''

    def _get_embedder(self) -> Any | None:
        """Return injected or lazily imported embedder."""
        if self.embedder is not None:
            return self.embedder
        try:
            from .embeddings import get_embedder
            return get_embedder()
        except (ImportError, AttributeError):
            return None

    def course_id(self, category: str) -> str:
        """Return stable root-scoped course ID, persisted in a sidecar in course directory."""
        self._ensure_storage()
        storage_root = Path(getattr(self.settings, "storage_root", Path.home() / "Documents" / "QwenASR资料库"))
        if category not in self.settings.categories or not category or '/' in category or '\\' in category or category in {'.','..'}:
            raise ValueError('请先选择或新建有效课程')
        return ensure_course_id(storage_root, category, self.store)

    def sync_course(
        self,
        category: str,
        stop: Callable[[], bool] | None = None,
        progress_cb: Callable[[str, float], None] | None = None,
    ) -> dict[str, Any]:
        """Blocking incremental indexing of all files and recordings for category."""
        cid = self.course_id(category)
        embedder = self._get_embedder()
        return self.ingestor.ingest_course(
            category=category,
            course_id=cid,
            embedder=embedder,
            stop=stop,
            progress_cb=progress_cb,
        )

    def search(
        self,
        category: str,
        query: str,
        limit: int = 6,
        lesson_id: str | None = None,
        source_types: list[str] | None = None,
        include_derived: bool = False,
    ) -> list[dict[str, Any]]:
        """Hybrid search with FTS char ngrams, exact NumPy vectors, RRF, deduplication, and neighbor context."""
        self.course_id(category)
        # 1. Verify freshness and hide modified/deleted sources before search
        self._check_freshness_before_search(category)

        # 2. Lexical search
        lexical_candidates = self.store.search_lexical(
            category=category,
            query=query,
            limit=limit * 4,
            lesson_id=lesson_id,
            source_types=source_types,
            include_derived=include_derived,
        )
        lexical_ranks: dict[str, int] = {
            c["chunk_id"]: idx + 1 for idx, c in enumerate(lexical_candidates)
        }

        # 3. Vector search
        embedder = self._get_embedder()
        vector_candidates: list[dict[str, Any]] = []
        if embedder and hasattr(embedder, "encode_query") and hasattr(embedder, "contract"):
            try:
                from ..inference_providers import inference_settings
                if inference_settings.for_task('knowledge').get('knowledge_provider') == 'remote_only' and not getattr(embedder,'remote',False):
                    raise RuntimeError('课堂省电模式禁止本机向量检索')
                if getattr(embedder,'remote',False):
                    vector_candidates = embedder.search(self.store,self.course_id(category),query,limit=limit*4,
                        lesson_id=lesson_id,source_types=source_types,include_derived=include_derived)
                else:
                    q_vec = embedder.encode_query(query)
                    vector_candidates = self.store.search_vectors(
                        category=category,
                        query_vector=q_vec,
                        embedding_contract=embedder.contract,
                        limit=limit * 4,
                        lesson_id=lesson_id,
                        source_types=source_types,
                        include_derived=include_derived,
                    )
                self.last_vector_success = True
                self.last_retrieval_warning = ''
            except Exception as exc:
                self.last_vector_success = False
                self.last_retrieval_warning = '向量检索暂不可用，已使用中文词法检索：'+str(exc)
                vector_candidates = []

        vector_ranks: dict[str, int] = {
            c["chunk_id"]: idx + 1 for idx, c in enumerate(vector_candidates)
        }

        # 4. Reciprocal Rank Fusion (RRF)
        all_candidate_dicts: dict[str, dict[str, Any]] = {}
        for c in lexical_candidates:
            all_candidate_dicts[c["chunk_id"]] = c
        for c in vector_candidates:
            all_candidate_dicts[c["chunk_id"]] = c

        scored_candidates: list[tuple[float, dict[str, Any]]] = []
        for chunk_id, c_data in all_candidate_dicts.items():
            rrf_score = 0.0
            if chunk_id in lexical_ranks:
                rrf_score += 1.0 / (60.0 + lexical_ranks[chunk_id])
            if chunk_id in vector_ranks:
                rrf_score += 1.0 / (60.0 + vector_ranks[chunk_id])
            scored_candidates.append((rrf_score, c_data))

        # Sort descending by RRF score
        scored_candidates.sort(key=lambda item: item[0], reverse=True)

        # 5. Content deduplication
        deduped_results: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()

        for score, cand in scored_candidates:
            c_hash = cand.get("content_hash") or cand["chunk_id"]
            if c_hash in seen_hashes:
                continue
            seen_hashes.add(c_hash)

            # Build result contract format
            seg_ids_raw = cand.get("segment_ids", "[]")
            if isinstance(seg_ids_raw, str):
                try:
                    seg_ids = json.loads(seg_ids_raw)
                except (ValueError, TypeError):
                    seg_ids = []
            else:
                seg_ids = list(seg_ids_raw or [])

            contract_raw = cand.get("embedding_contract", "")
            if isinstance(contract_raw, str) and contract_raw.startswith("{"):
                try:
                    contract_val = json.loads(contract_raw)
                except (ValueError, TypeError):
                    contract_val = contract_raw
            else:
                contract_val = contract_raw

            item_result = {
                "citation_id": cand.get("citation_id"),
                "chunk_id": cand["chunk_id"],
                "source_id": cand["source_id"],
                "revision": cand["revision"],
                "content_hash": cand["content_hash"],
                "course_id": cand["course_id"],
                "category": cand["category"],
                "title": cand["title"],
                "kind": cand["kind"],
                "origin": cand["origin"],
                "text": cand["text"],
                "heading": cand.get("heading"),
                "page": cand.get("page"),
                "start": cand.get("start"),
                "end": cand.get("end"),
                "segment_ids": seg_ids,
                "lesson_id": cand.get("lesson_id"),
                "parser_version": cand.get("parser_version"),
                "chunker_version": cand.get("chunker_version"),
                "embedding_contract": contract_val,
                "open_url": cand.get("open_url", ""),
                "stale": False,
                "score": round(score, 6),
                "vector_score": cand.get('vector_score'),
                "remote_index_version": cand.get('remote_index_version'),
                "neighbor_context": self.store.get_neighbor_context(
                    cand["source_id"], cand["revision"], cand["chunk_index"]
                ),
            }

            meta=self.store.get_source(item_result['source_id']) or {}
            lineage=meta.get('lineage') or {}
            if isinstance(lineage,str):
                try:lineage=json.loads(lineage)
                except ValueError:lineage={}
            item_result['lineage']=lineage
            item_result['canonical_source_id']=lineage.get('parent_source_id') or item_result['source_id']
            item_result['source_type']=meta.get('source_type')
            # A refined transcript and its raw segments are one evidence lineage.
            duplicate=None
            ids=set(item_result['segment_ids'])
            for previous in deduped_results:
                previous_ids=set(previous.get('segment_ids') or [])
                if ids and previous_ids and previous.get('canonical_source_id')==item_result['canonical_source_id'] and len(ids & previous_ids)/max(1,len(ids | previous_ids)) >= .8:
                    duplicate=previous
                    break
            if duplicate:
                if item_result.get('source_type')=='recording_refined' and duplicate.get('source_type')!='recording_refined':
                    deduped_results.remove(duplicate)
                else:
                    continue
            if item_result.get('lesson_id'):
                item_result['open_url'] = f"/?recording={item_result['lesson_id']}&start={float(item_result.get('start') or 0)}"
            else:
                item_result['open_url'] = f"/api/knowledge/source/{item_result['source_id']}?revision={item_result['revision']}"
                if item_result.get('page'):
                    item_result['open_url'] += f"#page={item_result['page']}"
            deduped_results.append(item_result)
            if len(deduped_results) >= limit:
                break

        return deduped_results

    def evidence_pack(self, results, limit=8, char_budget=12000):
        primary=[];extra=[];seen=set();used=0
        for source in results:
            if source.get('stale') or source.get('chunk_id') in seen:
                continue
            if used+len(source.get('text',''))>char_budget:
                continue
            item=dict(source)
            item['lineage']={k:v for k,v in (item.get('lineage') or {}).items() if k in {'parent_source_id','type'}}
            item.pop('embedding_contract',None)
            item.pop('neighbor_context',None)
            primary.append(item);seen.add(item['chunk_id']);used+=len(item.get('text',''))
            if not any(term in source.get('text','') for term in ('公式','其中','条件','定义','题','$','|')):
                continue
            for neighbor in (source.get('neighbor_context') or {}).values():
                if not neighbor or not neighbor.get('chunk_id'):
                    continue
                row=self.store.get_chunk(neighbor['chunk_id'])
                if not row or not row['is_active'] or row['revision']!=source['revision'] or row['source_id']!=source['source_id']:
                    continue
                adjoining={**item,**{k:row[k] for k in ('chunk_id','citation_id','text','heading','page','start','end','content_hash')}}
                adjoining['segment_ids']=json.loads(row['segment_ids'] or '[]')
                if adjoining.get('lesson_id'):
                    adjoining['open_url']=f"/?recording={adjoining['lesson_id']}&start={adjoining.get('start') or 0}"
                else:
                    adjoining['open_url']=f"/api/knowledge/source/{row['source_id']}?revision={row['revision']}"+(f"#page={row['page']}" if row.get('page') else '')
                extra.append(adjoining)
        packed=primary[:limit]
        for item in extra:
            if len(packed)>=limit:break
            if item['chunk_id'] in seen or used+len(item['text'])>char_budget:continue
            packed.append(item);seen.add(item['chunk_id']);used+=len(item['text'])
        return packed

    def classroom_evidence(self, record, query, limit=6, **kwargs):
        from ..classroom_intelligence.store import read_notes_doc
        hint=read_notes_doc(record).get('source_hint') or {}
        category=record.metadata.get('category','')
        contextual=[]
        if hint.get('source_id'):
            contextual=self.source_context(category,hint['source_id'],hint.get('page'),limit=4)
            contextual=[dict(e,manual_context=True) for e in contextual if e['revision']==hint.get('revision')]
        query=(str(query)+' '+str(hint.get('heading') or '')).strip()
        recalled=self.search(category,query,limit=limit,**kwargs)
        unique={}
        for item in contextual+recalled:unique.setdefault(item['chunk_id'],item)
        return list(unique.values())[:limit+2]

    def source_context(self, category, source_id, page=None, limit=6):
        self.course_id(category)
        self._check_freshness_before_search(category)
        with self.store._connect() as conn:
            sql='SELECT c.*,s.source_type FROM chunks c JOIN sources s USING(source_id) WHERE c.category=? AND c.source_id=? AND c.is_active=1 AND s.status=\'active\''
            args=[category,source_id]
            if page is not None:
                sql+=' AND c.page=?';args.append(page)
            sql+=' ORDER BY c.chunk_index LIMIT ?';args.append(min(12,max(1,limit)))
            rows=conn.execute(sql,args).fetchall()
        result=[]
        for row in rows:
            item=dict(row)
            item['segment_ids']=json.loads(item.get('segment_ids') or '[]')
            try:item['embedding_contract']=json.loads(item.get('embedding_contract') or '{}')
            except ValueError:pass
            item.update(valid=True,stale=False,score=0,manual_hint=True)
            if item.get('lesson_id'):
                item['open_url']=f"/?recording={item['lesson_id']}&start={item.get('start') or 0}"
            else:
                item['open_url']=f"/api/knowledge/source/{source_id}?revision={item['revision']}"+(f"#page={item['page']}" if item.get('page') else '')
            result.append(item)
        return result

    @staticmethod
    def _parent_fresh(source):
        lineage=source.get('lineage') or {}
        if isinstance(lineage,str):
            try:lineage=json.loads(lineage)
            except ValueError:return False
        parent=lineage.get('parent_file_path')
        if parent:
            path=Path(parent)
            return path.is_file() and compute_file_stat_hash(path)[2]==lineage.get('parent_stat_hash')
        return True

    def _check_freshness_before_search(self, category: str) -> None:
        """Verify physical existence and stat freshness of source files.
        Tombstone deleted sources and hide outdated chunks of modified sources immediately."""
        self.store.exclude_legacy_course_file_variants(category)
        active_sources = self.store.list_sources(category, active_only=True)
        for s in active_sources:
            if not self._parent_fresh(s):
                lineage=json.loads(s.get('lineage') or '{}')
                if lineage.get('parent_file_path') and not Path(lineage['parent_file_path']).exists():
                    self.store.tombstone_source(s['source_id'])
                    continue
                self.store.mark_source_stale_and_hide_chunks(s['source_id'],'原件已更新，旧转换结果不可作为当前证据')
                continue
            path_str = s.get("file_path")
            if not path_str:
                if s.get('source_type')=='course_terminology':
                    from .ingest import terminology_stat
                    if terminology_stat(self.settings,category)[2]!=s.get('stat_hash'):
                        self.store.mark_source_stale_and_hide_chunks(s['source_id'],'课程术语已更新')
                continue
            p = Path(path_str)
            if not p.exists():
                self.store.tombstone_source(s["source_id"])
                continue

            # Verify stat_hash proof
            stored_stat_hash = s.get("stat_hash")
            if not stored_stat_hash:
                continue

            stype = s.get("source_type", "")
            if stype == "recording_refined":
                rec_dir = p.parent if p.is_file() else p
                _, _, cur_stat_hash = compute_recording_stat_hash(rec_dir)
            else:
                _, _, cur_stat_hash = compute_file_stat_hash(p)

            if cur_stat_hash and cur_stat_hash != stored_stat_hash:
                # Underlying file was modified on disk! Immediately hide chunks before reindex
                self.store.mark_source_stale_and_hide_chunks(s["source_id"], "file_stat_changed")

    def status(self, category: str) -> dict[str, Any]:
        """Return knowledge status for category."""
        cid = self.course_id(category)
        self._check_freshness_before_search(category)
        sources = self.store.list_sources(category, active_only=True)
        embedder = self._get_embedder()
        contract = embedder.contract if embedder and hasattr(embedder, "contract") else None

        with self.store._connect() as conn:
            chunk_count_row = conn.execute(
                "SELECT count(*) as cnt FROM chunks WHERE category = ? AND is_active = 1",
                (category,),
            ).fetchone()
            chunk_count = chunk_count_row["cnt"] if chunk_count_row else 0

            tombstone_row = conn.execute(
                "SELECT count(*) as cnt FROM sources WHERE category = ? AND status = 'tombstoned'",
                (category,),
            ).fetchone()
            tombstone_count = tombstone_row["cnt"] if tombstone_row else 0

        contract_key=json.dumps(contract,sort_keys=True) if isinstance(contract,dict) else str(contract or '')
        with self.store._connect() as conn:
            row=conn.execute('SELECT count(*) FROM chunk_vectors v JOIN chunks c USING(chunk_id) WHERE c.category=? AND c.is_active=1 AND v.embedding_contract=?',(category,contract_key)).fetchone()
            current_vectors=row[0]
        recent_jobs = [j for j in self.job_manager.jobs(limit=10) if j.get("category") == category]

        all_sources = [r for r in self.store.list_sources(category, active_only=False) if r['status'] != 'excluded']
        sources_info = []
        for r in all_sources:
            state = r['status']
            label = {'active': '仅词法可用' if r.get('needs_vectors') else '已就绪',
                     'waiting_conversion': '等待 MinerU 转换', 'tombstoned': '已删除',
                     'stale': '等待重新转换' if r['source_type'] == 'course_file_pdf' else '原件已更新，等待索引'}
            sources_info.append({'id': r['source_id'], 'source_id': r['source_id'], 'name': r['title'],
                                 'title': r['title'], 'kind': r['kind'], 'origin': r['origin'],
                                 'state': state, 'source_type': r['source_type'], 'status': label.get(state, '需要核对解析'),
                                 'error': r.get('error'), 'revision': r.get('active_revision')})
        return {
            "category": category,
            "course_id": cid,
            "sources":sources_info,
            "warning":self.last_retrieval_warning,
            "index_policy":"converted-pdf-only-v1",
            "index_policy_label":"课程资料仅索引 MinerU 转换后 PDF；课堂转录、速记与术语继续参与检索",
            "waiting_conversion_count":sum(r['source_type']=='course_file_pdf' and r['status'] in {'waiting_conversion','stale'} for r in all_sources),
            "vector_ready":bool(current_vectors) and not self.last_retrieval_warning,
            "current_model_vectors_count":current_vectors,
            "model_rebuild_required":chunk_count>current_vectors,
            "sources_needing_vectors":sum(bool(x.get('needs_vectors')) for x in all_sources if x['status']=='active'),
            "active_sources_count": len(sources),
            "active_chunks_count": chunk_count,
            "tombstoned_sources_count": tombstone_count,
            "embedding_contract": contract,
            "lexical_only": not bool(current_vectors) or bool(self.last_retrieval_warning) or embedder is None,
            "recent_jobs": recent_jobs,
        }

    def validate_evidence(self, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Validate evidence copies with current source version freshness and content hash checks."""
        results: list[dict[str, Any]] = []

        for item in evidence:
            copy_item = dict(item)
            chunk_id = item.get("chunk_id")
            source_id = item.get("source_id")
            revision = item.get("revision")
            content_hash = item.get("content_hash")

            if not chunk_id or not source_id:
                copy_item["valid"] = False
                copy_item["stale"] = True
                copy_item["reason"] = "missing_identifiers"
                results.append(copy_item)
                continue

            chunk = self.store.get_chunk(chunk_id)
            source = self.store.get_source(source_id)
            if source:
                self._check_freshness_before_search(source['category'])
                source = self.store.get_source(source_id)

            if not chunk or not source:
                copy_item["valid"] = False
                copy_item["stale"] = True
                copy_item["reason"] = "not_found"
                results.append(copy_item)
                continue

            if source["status"] != "active" or chunk["is_active"] != 1:
                copy_item["valid"] = False
                copy_item["stale"] = True
                copy_item["reason"] = source.get('error') if source['status']=='stale' else "inactive_or_tombstoned"
                results.append(copy_item)
                continue

            if revision and source["active_revision"] != revision:
                copy_item["valid"] = False
                copy_item["stale"] = True
                copy_item["reason"] = "revision_mismatch"
                results.append(copy_item)
                continue

            if content_hash and chunk.get("content_hash") != content_hash:
                copy_item["valid"] = False
                copy_item["stale"] = True
                copy_item["reason"] = "content_hash_mismatch"
                results.append(copy_item)
                continue

            if not self._parent_fresh(source):
                copy_item.update(valid=False,stale=True,reason='parent_source_changed')
                results.append(copy_item)
                continue
            # Verify file stat on disk if file_path is present
            file_path_str = source.get("file_path")
            if file_path_str:
                p = Path(file_path_str)
                if not p.exists():
                    copy_item["valid"] = False
                    copy_item["stale"] = True
                    copy_item["reason"] = "source_file_missing"
                    results.append(copy_item)
                    continue

                stype = source.get("source_type", "")
                if stype == "recording_refined":
                    rec_dir = p.parent if p.is_file() else p
                    _, _, current_stat_hash = compute_recording_stat_hash(rec_dir)
                else:
                    _, _, current_stat_hash = compute_file_stat_hash(p)

                if source.get("stat_hash") and current_stat_hash != source["stat_hash"]:
                    copy_item["valid"] = False
                    copy_item["stale"] = True
                    copy_item["reason"] = "file_stat_changed"
                    results.append(copy_item)
                    continue

            copy_item["valid"] = True
            copy_item["stale"] = False
            copy_item["reason"] = "current"
            results.append(copy_item)

        return results

    async def start(self) -> None:
        """Async lifecycle start."""
        await self.job_manager.start()

    async def close(self) -> None:
        """Async lifecycle close."""
        await self.job_manager.close()

    def schedule(self, category: str) -> str:
        """Schedule indexing job for category."""
        self.course_id(category)
        return self.job_manager.schedule(category)

    def jobs(self) -> list[dict[str, Any]]:
        """List background jobs."""
        return self.job_manager.jobs()

    def cancel(self, job_id: str) -> bool:
        """Cancel a job."""
        return self.job_manager.cancel(job_id)
