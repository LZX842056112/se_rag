"""带请求级超时且无写操作的作用域检索工具。"""
from __future__ import annotations

import asyncio
import json
import math
import time
from types import SimpleNamespace

import pymongo
from pydantic import ValidationError
from pymilvus import MilvusClient, WeightedRanker

from app.rag.agentic.contracts import EvidenceRef, RetrievalScope, ToolResult
from app.rag.agentic.evidence import docs_to_evidence, evidence_is_valid, evidence_to_docs, product_in_scope
from app.rag.item_name import catalog
from app.rag.item_name.config import GLOBAL_ITEM_NAME
from app.rag.query.chunk_search import CHUNK_OUTPUT_FIELDS, to_chunks
from app.rag.query.config import CHUNK_DENSE_METRIC, PARENT_MAX_CHARS, PARENT_TOTAL_BUDGET_CHARS
from app.shared.clients import milvus_gateway as milvus_module
from app.shared.clients.milvus_gateway import eq_expr, in_expr, milvus_gateway
from app.shared.clients.mongo import get_collection
from app.shared.config import settings
from app.shared.models import llm_providers
from app.shared.utils.text import is_same_entity, split_subjects


def remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("retrieval deadline exceeded")
    return remaining


def expand_scope_names(names: list[str], deadline: float) -> list[str]:
    """使用已加载的产品目录。切勿在预算之外刷新它。"""
    remaining_seconds(deadline)
    cached = list(catalog._CACHE.get("raws") or [])
    aliases = [str(raw) for raw in cached if product_in_scope(raw, names)]
    return list(dict.fromkeys([*names, *aliases]))


def fetch_scoped_parents(docs: list[dict], deadline: float) -> dict[str, dict]:
    """Mongo 的 CSOT 同样约束服务器选择与套接字等待。"""
    pairs = [{"parent_id": doc["parent_id"], "doc_id": doc["doc_id"]}
             for doc in docs if doc.get("parent_id") and doc.get("doc_id")]
    if not pairs:
        return {}
    with pymongo.timeout(remaining_seconds(deadline)):
        collection = get_collection(settings.mongo.parent_chunks_collection)
        rows = list(collection.find({"$or": pairs}, {"_id": 0}).limit(len(pairs)))
    remaining_seconds(deadline)
    return {str(row["parent_id"]): row for row in rows}


class RetrievalTools:
    """模型可以缩小服务器的检索范围，但不能授予产品访问权限。"""

    names = frozenset({"semantic_search", "keyword_search", "read_chunk", "search_approved_faq", "web_search"})

    def __init__(self, state: dict):
        self.state = state
        self._owned_client = None

    def _config(self, key: str, default):
        return getattr(getattr(settings, "agentic", SimpleNamespace()), key, default)

    def _scope(self, arguments: dict) -> RetrievalScope:
        item_names = self.state.get("item_names") or []
        if not isinstance(item_names, list) or not item_names or any(not isinstance(x, str) or not x.strip() for x in item_names):
            raise ValueError("a server-confirmed product scope is required")
        server = RetrievalScope(item_names=item_names,
                                corpus_version=self.state.get("corpus_version") or None,
                                allowed_document_kinds=self.state.get("allowed_document_kinds") or [])
        if self.state.get("business_tag") in {"pricing", "after_sales"}:
            server.allowed_document_kinds = ["policy"]
        requested = arguments.get("scope")
        if requested is None:
            return server
        narrowed = RetrievalScope.model_validate(requested)
        if narrowed.item_names:
            # 逐一校验每个产品，然后仅保留服务器拥有的名称。
            # 相似性搜索（某个 token 前缀或一个命中的复合成员）
            # 不能授权访问其他产品或更宽泛的型号系列。
            canonical = list(dict.fromkeys(subject for name in server.item_names for subject in split_subjects(name)))
            selected = []
            for name in narrowed.item_names:
                subjects = split_subjects(name)
                if not subjects:
                    raise ValueError("requested scope exceeds the server product scope")
                for subject in subjects:
                    match = next((item for item in canonical if is_same_entity(subject, item)), None)
                    if match is None or subject == GLOBAL_ITEM_NAME and subject not in canonical:
                        raise ValueError("requested scope exceeds the server product scope")
                    selected.append(match)
            server.item_names = list(dict.fromkeys(selected))
        if narrowed.corpus_version:
            if server.corpus_version and narrowed.corpus_version != server.corpus_version:
                raise ValueError("requested corpus version differs from server scope")
            server.corpus_version = narrowed.corpus_version
        if narrowed.allowed_document_kinds:
            if server.allowed_document_kinds and not set(narrowed.allowed_document_kinds) <= set(server.allowed_document_kinds):
                raise ValueError("requested document kinds exceed server scope")
            server.allowed_document_kinds = narrowed.allowed_document_kinds
        return server

    @staticmethod
    def _limit(arguments: dict) -> int:
        value = arguments.get("limit", 5)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 20:
            raise ValueError("limit must be between 1 and 20")
        return value

    @staticmethod
    def _query(arguments: dict) -> str:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 4096:
            raise ValueError("query must contain 1 to 4096 characters")
        return query.strip()

    def _client(self, deadline: float):
        # 复用已建立的客户端，但将新连接绑定到本次调用的预算。
        # 标准网关的惰性属性具有全局连接超时。
        if self._owned_client is not None:
            remaining_seconds(deadline)
            return self._owned_client
        if isinstance(milvus_gateway, milvus_module.MilvusGateway):
            client = milvus_module._milvus_client
            if client is None:
                if not settings.milvus.url:
                    raise ConnectionError("Milvus is not configured")
                client = MilvusClient(uri=settings.milvus.url, timeout=self._rpc_timeout(deadline))
                self._owned_client = client
        else:
            client = milvus_gateway.milvus_client
        if client is None:
            raise ConnectionError("Milvus is unavailable")
        remaining_seconds(deadline)
        return client

    @staticmethod
    def _rpc_timeout(deadline: float) -> float:
        return min(float(settings.milvus.timeout_seconds), remaining_seconds(deadline))

    def _expression(self, scope: RetrievalScope, deadline: float) -> str:
        names = expand_scope_names(scope.item_names, deadline)
        expression = f"({in_expr('item_name', names)} or {eq_expr('item_name', GLOBAL_ITEM_NAME)})"
        if scope.corpus_version:
            expression += f" and {eq_expr('document_version', scope.corpus_version)}"
        if scope.allowed_document_kinds:
            expression += f" and {in_expr('document_kind', scope.allowed_document_kinds)}"
        return expression

    def execute(self, name: str, arguments: dict, timeout_seconds: float) -> ToolResult:
        started = time.monotonic()
        status, refs, error = "invalid", [], None
        try:
            if not isinstance(arguments, dict) or name not in self.names:
                raise ValueError("unknown tool or invalid arguments")
            if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds):
                raise ValueError("timeout must be finite")
            deadline = started + timeout_seconds
            remaining_seconds(deadline)
            scope = self._scope(arguments)
            limit = self._limit(arguments)
            docs = getattr(self, f"_{name}")(arguments, scope, limit, deadline)
            remaining_seconds(deadline)
            refs = docs_to_evidence(docs, scope=scope)
            status = "ok" if refs else "empty"
        except (TimeoutError, asyncio.TimeoutError) as exc:
            status, error = "timeout", str(exc)
        except (ValueError, TypeError, ValidationError) as exc:
            status, error = "invalid", str(exc)
        except Exception as exc:  # 后端故障绝不能成为一次成功的空搜索结果。
            status = "timeout" if getattr(exc, "timeout", False) or "timeout" in type(exc).__name__.lower() else "unavailable"
            error = type(exc).__name__
        finally:
            self._close_client()
        # UTF-8 字节数作为 token 的保守上界，无需额外加载一个模型。
        tokens = sum(len(ref.text.encode("utf-8")) for ref in refs)
        return ToolResult(status=status, evidence=refs, elapsed_ms=(time.monotonic()-started)*1000,
                          token_count=tokens, error=error)

    def _close_client(self):
        client, self._owned_client = self._owned_client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass  # 本地通道清理失败不能掩盖请求的结果。

    def revalidate(self, evidence: list[dict | EvidenceRef], timeout_seconds: float) -> list[EvidenceRef]:
        """在起草或发布答案前，移除已撤回或已变更的来源。

        使用相同的产品过滤条件校验存储的 ID。它从不执行新的搜索，
        也从不以新检索到的文本来替换已选中的证据。
        网络证据始终保留为带时间戳的检索快照。
        """
        started = time.monotonic()
        deadline = started + timeout_seconds
        result, status = [], "ok"
        try:
            remaining_seconds(deadline)
            scope = self._scope({})
            refs = [ref if isinstance(ref, EvidenceRef) else EvidenceRef.model_validate(ref) for ref in evidence]
            refs = [ref for ref in refs if ref.metadata.get("read", True) and evidence_is_valid(ref, scope)]
            expression = self._expression(scope, deadline)
            current = {}
            for source in ("kb", "evolution"):
                ids = list(dict.fromkeys(ref.chunk_id for ref in refs if ref.source == source and ref.chunk_id))
                for offset in range(0, len(ids), 20):
                    field = "chunk_id" if source == "kb" else "evo_doc_id"
                    expr = f"({expression}) and {in_expr(field, ids[offset:offset+20])}"
                    if source == "evolution":
                        from app.evolution.flags import recall_enabled
                        if not recall_enabled():
                            continue
                        expr = f"({expr}) and {eq_expr('status', 'active')}"
                    client = self._client(deadline)
                    rows = client.query(collection_name=milvus_gateway.chunk_collection_name if source == "kb" else milvus_gateway.evolution_collection_name,
                        filter=expr, output_fields=["*"], limit=min(20, len(ids)-offset), timeout=self._rpc_timeout(deadline))
                    if rows is None:
                        raise ConnectionError("source revalidation unavailable")
                    if source == "evolution":
                        rows = self._approved_faq_docs(list(rows), scope, deadline)
                    for row in rows:
                        identity = str(row.get(field) or "")
                        if identity not in ids[offset:offset+20]:
                            continue
                        row = dict(row)
                        if source == "evolution":
                            row.update(source="evolution", chunk_id=identity, content=row.get("faq_answer"), title=row.get("faq_question"))
                        valid = docs_to_evidence([row], scope=scope)
                        if valid:
                            current[(source, identity)] = valid[0]
            parent_docs = [ref.metadata for ref in refs if ref.source == "kb" and ref.metadata.get("parent_content")]
            parents = fetch_scoped_parents(parent_docs, deadline) if parent_docs else {}
            for ref in refs:
                if ref.source == "web":
                    if self.state.get("business_tag") not in {"pricing", "after_sales"} and self._config("allow_web", True):
                        result.append(ref)
                    continue
                fresh = current.get((ref.source, ref.chunk_id))
                if fresh is None or fresh.document_version != ref.document_version or fresh.doc_id != ref.doc_id:
                    continue
                if fresh.document_kind != ref.document_kind or fresh.source_version != ref.source_version or fresh.valid_until != ref.valid_until:
                    continue
                if fresh.metadata["child_text"] != ref.metadata.get("child_text", ref.text):
                    continue
                parent_text = ref.metadata.get("parent_content")
                if parent_text:
                    parent = parents.get(ref.parent_id)
                    if not parent or parent.get("doc_id") != ref.doc_id or parent.get("document_version") != ref.document_version:
                        continue
                    if parent.get("item_name") and not product_in_scope(parent["item_name"], scope.item_names):
                        continue
                    if str(parent.get("content") or "")[:len(parent_text)] != parent_text:
                        continue
                result.append(ref)
            remaining_seconds(deadline)
        except Exception as exc:
            result = []
            status = "timeout" if isinstance(exc, TimeoutError) or getattr(exc, "timeout", False) else "unavailable"
        finally:
            self._close_client()
            self.state.setdefault("agentic_revalidation", []).append({"status": status,
                "elapsed_ms": (time.monotonic()-started)*1000, "input_count": len(evidence), "retained_count": len(result)})
        return result

    def _hybrid(self, query: str, scope: RetrievalScope, limit: int, deadline: float, *, faq=False) -> list[dict]:
        expression = self._expression(scope, deadline)
        if faq:
            expression = f"{eq_expr('status', 'active')} and ({expression})"
        remaining_seconds(deadline)
        vectors = llm_providers.embed_text(query)
        # 本地 BGE 推理是同步的，不要以为线程取消能够阻止它。
        remaining_seconds(deadline)
        reqs = milvus_gateway.create_requests(dense_vector=vectors["dense"][0], sparse_vector=vectors["sparse"][0],
            dense_params={"metric_type": "IP" if faq else CHUNK_DENSE_METRIC}, expr=expression, limit=max(limit*2, 10))
        client = self._client(deadline)
        result = client.hybrid_search(collection_name=milvus_gateway.evolution_collection_name if faq else milvus_gateway.chunk_collection_name,
            reqs=reqs, ranker=WeightedRanker(0.6, 0.4, norm_score=True), limit=limit,
            output_fields=["*"] if faq else CHUNK_OUTPUT_FIELDS, timeout=self._rpc_timeout(deadline))
        if result is None:
            raise ConnectionError("hybrid retrieval returned no service result")
        remaining_seconds(deadline)
        if not faq:
            return to_chunks(result[0] if result else [])
        docs = []
        for hit in result[0] if result else []:
            entity = dict(hit.get("entity") or {})
            entity.update(source="evolution", type="milvus", chunk_id=entity.get("evo_doc_id"),
                          content=entity.get("faq_answer"), title=entity.get("faq_question"), score=hit.get("distance"))
            docs.append(entity)
        return self._approved_faq_docs(docs, scope, deadline)

    def _approved_faq_docs(self, docs: list[dict], scope: RetrievalScope, deadline: float) -> list[dict]:
        """即使索引删除失败，Mongo 中的发布状态仍是权威依据。"""
        from app.evolution.governance import is_retrievable
        expression = self._expression(scope, deadline)
        source_cache, resolver_errors = {}, []

        def resolve_source(identity: str):
            if identity in source_cache:
                return source_cache[identity]
            try:
                client = self._client(deadline)
                rows = client.query(collection_name=milvus_gateway.chunk_collection_name,
                    filter=f"({expression}) and {eq_expr('chunk_id', identity)}", output_fields=["*"], limit=1,
                    timeout=self._rpc_timeout(deadline))
                if rows is None:
                    raise ConnectionError("FAQ source validation unavailable")
                source = next((row for row in rows if str(row.get("chunk_id")) == identity), None)
                if source and not docs_to_evidence([source], scope=scope):
                    source = None
                source_cache[identity] = source
                return source
            except Exception as exc:
                resolver_errors.append(exc)
                raise

        approved = []
        for doc in docs:
            identity = str(doc.get("evo_doc_id") or doc.get("chunk_id") or "")
            with pymongo.timeout(remaining_seconds(deadline)):
                record = get_collection(settings.mongo.k_candidates_collection).find_one({"evo_doc_id": identity})
            if not record or not any(product_in_scope(name, scope.item_names) for name in record.get("item_names") or []):
                continue
            valid = is_retrievable(record, source_resolver=resolve_source)
            remaining_seconds(deadline)
            if resolver_errors:
                raise resolver_errors[0]
            if not valid:
                continue
            # 从 Mongo 读取已批准的正文。过期或篡改的向量负载，
            # 绝不能仅仅因为 ID 匹配就被提升为权威证据。
            approved.append({**doc, "source": "evolution", "chunk_id": identity, "doc_id": None,
                "parent_id": None, "content": record["faq_answer"], "text": record["faq_answer"],
                "title": record.get("faq_question"), "item_name": ",".join(record["item_names"]),
                "approval_status": "active", "source_version": record["content_sha256"],
                "document_version": str(record.get("content_version", 1)),
                "source_versions": record["source_versions"], "publication_status": "active"})
            approved[-1]["knowledge_origin"] = record.get("knowledge_origin", "document")
        return approved

    def _semantic_search(self, arguments, scope, limit, deadline):
        return self._hybrid(self._query(arguments), scope, limit, deadline)

    def _keyword_search(self, arguments, scope, limit, deadline):
        terms = arguments.get("terms")
        if not isinstance(terms, list) or not 1 <= len(terms) <= 10 or any(
                not isinstance(term, str) or not term.strip() or len(term) > 256 for term in terms):
            raise ValueError("terms must contain 1 to 10 literal search terms")
        scan_limit = max(1, int(self._config("keyword_scan_limit", 500)))
        expression = self._expression(scope, deadline)
        client = self._client(deadline)
        rows = client.query(collection_name=milvus_gateway.chunk_collection_name, filter=expression,
                            output_fields=CHUNK_OUTPUT_FIELDS, limit=scan_limit+1, timeout=self._rpc_timeout(deadline))
        if rows is None or len(rows) > scan_limit:
            raise ConnectionError("scoped corpus exceeds the bounded keyword scan; a lexical index is required")
        terms = [term.strip().casefold() for term in terms]
        matches = [dict(row) for row in rows if any(term in str(row.get("content") or "").casefold() for term in terms)]
        matches = evidence_to_docs(docs_to_evidence(matches, scope=scope))[:limit]
        for row in matches:
            row["metadata"] = {"read": False}
            row["content"] = str(row.get("content") or "")[:400]
            row["text"] = row["content"]
        return matches

    def _read_chunk(self, arguments, scope, limit, deadline):
        ids = arguments.get("chunk_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 20 or any(
                not isinstance(cid, str) or not cid or len(cid) > 128 for cid in ids):
            raise ValueError("chunk_ids must contain 1 to 20 bounded identifiers")
        window = arguments.get("neighbor_window", self._config("neighbor_window", 1))
        if isinstance(window, bool) or not isinstance(window, int) or not 0 <= window <= 2:
            raise ValueError("neighbor_window must be between 0 and 2")
        expression = self._expression(scope, deadline)
        client = self._client(deadline)
        rows = client.query(collection_name=milvus_gateway.chunk_collection_name,
            filter=f"({expression}) and {in_expr('chunk_id', list(dict.fromkeys(ids)))}",
            output_fields=CHUNK_OUTPUT_FIELDS, limit=len(ids), timeout=self._rpc_timeout(deadline))
        if rows is None:
            raise ConnectionError("chunk read returned no service result")
        # 除作用域外还重新校验标识符：后端结果是不可信数据。
        rows = [row for row in rows if str(row.get("chunk_id")) in ids]
        docs = evidence_to_docs(docs_to_evidence(rows, scope=scope))
        requested_versions = arguments.get("document_versions") or {}
        if not isinstance(requested_versions, dict):
            raise ValueError("document_versions must map chunk IDs to versions")
        docs = [doc for doc in docs if doc.get("chunk_id") not in requested_versions or
                doc.get("document_version") == requested_versions[doc["chunk_id"]]]
        neighbors = []
        if window:
            for doc in docs:
                if not doc.get("doc_id") or not isinstance(doc.get("seq"), int):
                    continue
                seq = doc["seq"]
                neighbor_expr = (f"({expression}) and {eq_expr('doc_id', doc['doc_id'])} and "
                    f"{eq_expr('document_version', doc.get('document_version') or '')} and seq >= {max(0, seq-window)} and seq <= {seq+window}")
                results = client.query(collection_name=milvus_gateway.chunk_collection_name, filter=neighbor_expr,
                    output_fields=CHUNK_OUTPUT_FIELDS, limit=window*2+1, timeout=self._rpc_timeout(deadline))
                if results is None:
                    raise ConnectionError("neighbor read returned no service result")
                neighbors.extend(row for row in results if row.get("doc_id") == doc["doc_id"]
                    and row.get("document_version") == doc.get("document_version")
                    and isinstance(row.get("seq"), int) and abs(row["seq"]-seq) <= window)
        docs = evidence_to_docs(docs_to_evidence([*docs, *neighbors], scope=scope))
        parents = fetch_scoped_parents(docs, deadline)
        remaining_chars, injected = PARENT_TOTAL_BUDGET_CHARS, set()
        for doc in docs:
            parent = parents.get(doc.get("parent_id"))
            if not parent or parent.get("parent_id") in injected or parent.get("doc_id") != doc.get("doc_id"):
                continue
            if parent.get("document_version") != doc.get("document_version"):
                continue
            if parent.get("item_name") and not product_in_scope(parent["item_name"], scope.item_names):
                continue
            text = str(parent.get("content") or "")[:min(PARENT_MAX_CHARS, remaining_chars)]
            if text:
                doc["parent_content"], doc["parent_title"] = text, parent.get("title") or ""
                remaining_chars -= len(text)
                injected.add(parent["parent_id"])
        return docs

    def _search_approved_faq(self, arguments, scope, limit, deadline):
        from app.evolution.flags import recall_enabled
        if not recall_enabled():
            raise ConnectionError("approved FAQ recall is disabled")
        if self.state.get("business_tag") in {"pricing", "after_sales"}:
            raise ValueError("business policy requires original policy evidence")
        return self._hybrid(self._query(arguments), scope, limit, deadline, faq=True)

    def _web_search(self, arguments, scope, limit, deadline):
        if self.state.get("business_tag") in {"pricing", "after_sales"}:
            raise ValueError("web results cannot support business policy promises")
        if not self._config("allow_web", True) or not settings.mcp.base_url or not settings.mcp.api_key:
            raise ConnectionError("web retrieval is not configured or enabled")
        query = self._query(arguments)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise ConnectionError("synchronous web tool must run outside the event loop")
        result = asyncio.run(self._request_web(query, limit, deadline))
        pages = json.loads(result.content[0].text).get("pages", [])
        if not isinstance(pages, list):
            raise ValueError("invalid web search output")
        fetched = time.time()
        return [{**page, "text": page.get("snippet") or page.get("content") or "", "source": "web", "type": "web",
                 "fetched_at": fetched} for page in pages[:limit] if isinstance(page, dict)]

    @staticmethod
    async def _request_web(query, limit, deadline):
        from agents.mcp import MCPServerStreamableHttp
        timeout = min(float(settings.mcp.timeout_seconds), remaining_seconds(deadline))
        server = MCPServerStreamableHttp(name="Agentic read-only web search", params={
            "url": settings.mcp.base_url, "headers": {"Authorization": f"Bearer {settings.mcp.api_key}"},
            "timeout": timeout, "sse_read_timeout": timeout}, cache_tools_list=True,
            client_session_timeout_seconds=timeout, max_retry_attempts=0)
        async with asyncio.timeout(remaining_seconds(deadline)):
            async with server:
                return await server.call_tool(tool_name="bailian_web_search", arguments={"query": query, "count": limit})
