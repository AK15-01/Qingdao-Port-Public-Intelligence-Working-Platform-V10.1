from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping, Optional

from platform_db import new_id, now_iso, transaction

from .citation_builder import build_citations
from .context_builder import build_context
from .query_router import QueryRoute, opportunity_bucket, route_query, route_results


NO_EVIDENCE = "当前知识库中没有足够的公开来源支持这一结论。"


@dataclass(frozen=True)
class RAGAnswer:
    answer: str
    citations: list[dict[str, object]]
    mode: str
    model_name: str = ""
    evidence_sufficient: bool = False


class RAGService:
    def __init__(self, db_path, retriever, answerer=None) -> None:
        self.db_path = db_path
        self.retriever = retriever
        self.answerer = answerer

    def ask(self, question: str, workspace_id: str = "", mode: str = "快速回答", filters: Optional[Mapping[str, object]] = None) -> RAGAnswer:
        route = route_query(question, mode, filters)
        results = route_results(
            self.retriever.retrieve(question, workspace_id, limit=8, filters=route.filters),
            route,
        )
        if route.intent == "当前风险":
            resolved_filters = dict(filters or {})
            resolved_filters["statuses"] = ["解除", "已结束"]
            resolved_route = QueryRoute("已解除风险", resolved_filters, route.qingdao_focus)
            resolved = route_results(
                self.retriever.retrieve(question, workspace_id, limit=4, filters=resolved_filters),
                resolved_route,
            )
            for item in resolved:
                item["answer_section"] = "已解除但需留档"
            seen_chunks = {str(item.get("chunk_id") or "") for item in results}
            results.extend(
                item for item in resolved
                if str(item.get("chunk_id") or "") not in seen_chunks
            )
        context, document_ids = build_context(results)
        if not context or not document_ids:
            answer = RAGAnswer(NO_EVIDENCE, [], mode, evidence_sufficient=False)
            self._log(question, answer, workspace_id, [])
            return answer
        snippets: dict[str, str] = {}
        for item in results:
            document_id = str(item.get("document_id") or (item.get("metadata") or {}).get("document_id") or "")
            snippets.setdefault(document_id, str(item.get("chunk_text") or ""))
        citations = build_citations(document_ids, snippets, self.db_path)
        valid_ids = {str(item["document_id"]) for item in citations}
        if not valid_ids:
            answer = RAGAnswer(NO_EVIDENCE, [], mode, evidence_sufficient=False)
            self._log(question, answer, workspace_id, [])
            return answer
        model_name = "本地证据整理"
        fixed_business_intents = {
            "当前风险", "历史风险", "已解除风险", "政策机会",
            "招标采购", "港口运营动态", "长期行业趋势",
            "来源核验", "时间对比",
        }
        if self.answerer is not None and route.intent not in fixed_business_intents:
            try:
                generated = self.answerer(question=question, context=context, mode=mode, allowed_document_ids=sorted(valid_ids))
                cited = set(generated.get("document_ids") or [])
                if not cited or not cited.issubset(valid_ids):
                    raise ValueError("回答引用了知识库中不存在或未检索到的 document_id")
                text = str(generated.get("answer") or "").strip()
                if not text:
                    raise ValueError("回答为空")
                model_name = str(generated.get("model_name") or "DeepSeek")
            except Exception:
                text = self._deterministic_answer(results, mode, route.intent)
        else:
            text = self._deterministic_answer(results, mode, route.intent)
        answer = RAGAnswer(text, citations, mode, model_name, True)
        self._log(question, answer, workspace_id, sorted(valid_ids))
        return answer

    @staticmethod
    def _display_title(item: Mapping[str, object]) -> str:
        title = str(item.get("title") or "").strip()
        if title and title != "未提取标题":
            return title
        publisher = str(
            item.get("publisher")
            or (item.get("metadata") or {}).get("source_name")
            or "公开来源"
        ).strip()
        published = str(
            item.get("published_at")
            or (item.get("metadata") or {}).get("published_at")
            or ""
        )[:10]
        return f"{publisher} {published} 公开资料".strip()

    @staticmethod
    def _item_line(item: Mapping[str, object]) -> str:
        document_id = str(item.get("document_id") or (item.get("metadata") or {}).get("document_id") or "")
        title = RAGService._display_title(item)
        summary = str(item.get("summary") or item.get("chunk_text") or "").strip().replace("\n", " ")[:180]
        geography = str(item.get("geography_scope") or "地域待核对")
        relation = str(item.get("qingdao_relation") or "与青岛港关系待核对")
        evidence = "证据已逐字定位" if bool(item.get("evidence_verified")) else "证据待逐字核验"
        review = (
            "真人已核验"
            if bool(item.get("human_verified"))
            and str(item.get("reviewer_type") or "") in {"human_user", "industry_reviewer"}
            else "待真人核验"
        )
        return (
            f"- {title}：{summary}｜地域：{geography}｜与青岛港关系：{relation}｜"
            f"{evidence}｜{review}（document_id: {document_id}）"
        )

    @classmethod
    def _deterministic_answer(cls, results, mode: str, intent: str = "") -> str:
        if intent == "历史风险":
            lines = ["1. 历史中高风险"]
            lines.extend(cls._item_line(item) for item in results[:5])
            if not results:
                lines.append("- 当前知识库没有具备逐字证据的历史中高风险。")
            lines.extend(
                [
                    "\n2. 当前状态说明",
                    "- 历史记录不等于当前仍有效；使用前必须查看同一事件是否已有解除或更新记录。",
                    "\n3. 证据不足项",
                    "- 引用未定位、低业务价值或仅为活动宣传的记录未用于补足列表。",
                ]
            )
            return "\n".join(lines)
        if intent in {"当前风险", "已解除风险"}:
            active_all = [
                item for item in results
                if str(item.get("answer_section") or "") != "已解除但需留档"
                and str(item.get("status") or "") not in {"解除", "已结束"}
            ]
            resolved_all = [
                item for item in results
                if str(item.get("answer_section") or "") == "已解除但需留档"
                or str(item.get("status") or "") in {"解除", "已结束"}
            ]
            active = [item for item in active_all if bool(item.get("evidence_verified"))]
            resolved = [item for item in resolved_all if bool(item.get("evidence_verified"))]
            uncertain = [
                item for item in [*active_all, *resolved_all]
                if not bool(item.get("evidence_verified"))
            ]
            lines = ["1. 当前有效风险"]
            lines.extend(cls._item_line(item) for item in active[:5])
            if not active:
                lines.append("- 当前知识库中没有足够的公开来源支持存在仍有效的中高风险。")
            lines.append("\n2. 已解除但需留档")
            lines.extend(cls._item_line(item) for item in resolved[:5])
            if not resolved:
                lines.append("- 本次检索未发现可关联的已解除事项。")
            lines.append("\n3. 可能影响的区域和业务环节")
            impact_items = active[:3] or resolved[:2]
            lines.extend(
                f"- {item.get('geography_scope', '地域待核对')}："
                f"{item.get('impact') or '应结合船期、靠离泊、装卸和陆运衔接核对。'}"
                for item in impact_items
            )
            if not impact_items:
                lines.append("- 证据不足，暂不推断具体业务影响。")
            lines.append("\n4. 建议核对动作")
            lines.extend(
                f"- {item.get('recommended_action') or '打开原文并核对最新状态、影响范围和解除信息。'}"
                for item in active[:3]
            )
            if not active:
                lines.append("- 继续检查权威来源，不用历史活动或宣传稿填充当前风险。")
            lines.append("\n5. 证据不足项")
            lines.extend(cls._item_line(item) for item in uncertain[:5])
            if not uncertain:
                lines.append("- 未在检索结果中出现或无法映射到真实 document_id 的结论均未纳入。")
            return "\n".join(lines)
        if intent in {"政策机会", "招标采购"}:
            buckets = {
                "直接商业机会": [],
                "政策或流程便利": [],
                "长期行业趋势": [],
                "尚无明确商业动作的信息": [],
            }
            for item in results:
                buckets[opportunity_bucket(item)].append(item)
            lines: list[str] = []
            for index, heading in enumerate(buckets, 1):
                lines.append(f"{index}. {heading}")
                values = buckets[heading]
                lines.extend(cls._item_line(item) for item in values[:5])
                if not values:
                    lines.append("- 当前知识库没有足够证据。")
                lines.append("")
            return "\n".join(lines).strip()
        if intent == "来源核验":
            lines = ["1. 可核验来源"]
            for item in results[:5]:
                document_id = str(item.get("document_id") or (item.get("metadata") or {}).get("document_id") or "")
                lines.append(
                    f"- {cls._display_title(item)}｜发布机构："
                    f"{item.get('publisher') or (item.get('metadata') or {}).get('source_name') or '待核对'}｜"
                    f"日期：{item.get('published_at') or (item.get('metadata') or {}).get('published_at') or '待核对'}｜"
                    f"document_id: {document_id}"
                )
            lines.append("\n2. 核验规则")
            lines.append("- 仅列出当前文档版本中已逐字绑定证据的事项；点击来源卡片打开原文复核。")
            return "\n".join(lines)
        if intent == "时间对比":
            active = [item for item in results if str(item.get("status") or "") not in {"解除", "已结束"}]
            resolved = [item for item in results if str(item.get("status") or "") in {"解除", "已结束"}]
            lines = [
                "1. 本期可核验变化",
                *([cls._item_line(item) for item in active[:3]] or ["- 没有足够证据确认新增的有效风险或机会。"]),
                "\n2. 已解除变化",
                *([cls._item_line(item) for item in resolved[:3]] or ["- 未检索到可逐字核验的解除事项。"]),
                "\n3. 对比限制",
                "- 问题未给出两个明确起止日期时，系统不计算虚假的环比数量；请补充周期后再做严格对比。",
            ]
            return "\n".join(lines)
        lines = [f"基于当前知识库检索到的公开来源，以下为{mode}结果："]
        seen: set[str] = set()
        for item in results[:5]:
            document_id = str(item.get("document_id") or (item.get("metadata") or {}).get("document_id") or "")
            if not document_id or document_id in seen:
                continue
            seen.add(document_id)
            lines.append(cls._item_line(item))
        lines.append("以上仅整理已检索公开资料；影响判断和业务动作仍需核对原文并由人工确认。")
        return "\n".join(lines)

    def _log(self, question: str, answer: RAGAnswer, workspace_id: str, document_ids: list[str]) -> None:
        with transaction(self.db_path) as connection:
            connection.execute(
                "INSERT INTO qa_logs(qa_id,workspace_id,question,answer,retrieved_document_ids,citations_json,model_name,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (new_id("QA"), workspace_id, question, answer.answer, json.dumps(document_ids, ensure_ascii=False),
                 json.dumps(answer.citations, ensure_ascii=False, default=str), answer.model_name, now_iso()),
            )
