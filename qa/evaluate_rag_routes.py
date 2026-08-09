from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platform_db import connect
from rag.embeddings import LocalBGEEmbedding
from rag.hybrid_retriever import HybridRetriever
from rag.keyword_search import KeywordSearcher
from rag.query_router import classify_geography, detect_intent
from rag.rag_service import RAGService
from rag.vector_store import ChromaVectorStore


QUESTIONS = (
    "青岛港当前有哪些值得关注的风险？",
    "历史上有哪些中高风险需要留档？",
    "哪些风险已经解除？",
    "最近有哪些政策或流程便利机会？",
    "最近有哪些招标采购机会？",
    "青岛港最近有哪些港口运营动态？",
    "有哪些值得长期观察的行业趋势？",
    "本期重点事项的原始出处是什么？",
    "与上一期相比，本期风险发生了什么变化？",
)


def evaluate(db_path: Path, workspace_id: str, chroma_path: Path) -> dict[str, object]:
    retriever = HybridRetriever(
        KeywordSearcher(db_path),
        LocalBGEEmbedding(),
        ChromaVectorStore(chroma_path),
    )
    service = RAGService(db_path, retriever, answerer=None)
    answers: list[dict[str, object]] = []
    for question in QUESTIONS:
        answer = service.ask(question, workspace_id, "快速回答")
        citation_ids = [str(item.get("document_id") or "") for item in answer.citations]
        metadata: list[dict[str, object]] = []
        if citation_ids:
            placeholders = ",".join("?" for _ in citation_ids)
            with connect(db_path) as connection:
                metadata = [
                    dict(row)
                    for row in connection.execute(
                        f"""SELECT d.document_id,d.title,e.status,e.category,e.risk_level,
                        e.business_value,e.affected_area,e.evidence_verified
                        FROM documents d LEFT JOIN events e ON e.document_id=d.document_id
                        WHERE d.document_id IN ({placeholders})""",
                        citation_ids,
                    ).fetchall()
                ]
        answers.append(
            {
                "question": question,
                "intent": detect_intent(question),
                "answer": answer.answer,
                "citation_count": len(answer.citations),
                "citations": [
                    {
                        "document_id": item.get("document_id"),
                        "title": item.get("title"),
                        "quote": item.get("quote"),
                        "evidence_verified": item.get("evidence_verified"),
                        "verification_status": item.get("verification_status"),
                    }
                    for item in answer.citations
                ],
                "event_metadata": [
                    {
                        **item,
                        **classify_geography(
                            " ".join(
                                str(item.get(key) or "")
                                for key in ("title", "affected_area", "category")
                            )
                        ),
                    }
                    for item in metadata
                ],
            }
        )
    current = answers[0]
    current_rows = current["event_metadata"]
    active_section = str(current["answer"]).split("2. 已解除但需留档", 1)[0]
    resolved_titles = [
        str(item.get("title") or "")
        for item in current_rows
        if str(item.get("status") or "") in {"解除", "已结束"}
    ]
    checks = {
        "已解除事项不混入当前有效风险段": all(
            not title or title not in active_section for title in resolved_titles
        ),
        "当前风险不含活动宣传": all(
            not any(term in str(item.get("title") or "") for term in ("党建", "荣誉", "培训", "活动"))
            for item in current_rows
        ),
        "青岛问题不以其他山东城市为主": all(
            int(item.get("geography_level") or 5) != 4 for item in current_rows
        ),
        "当前风险引用均逐字绑定": all(
            item.get("evidence_verified") is True for item in current["citations"]
        ),
        "引用不从明显残句开始": all(
            not str(item.get("quote") or "").startswith(("设，", "动，", "的，", "了，"))
            for answer in answers for item in answer["citations"]
        ),
    }
    return {"questions": answers, "checks": checks, "all_checks_passed": all(checks.values())}


def main() -> int:
    parser = argparse.ArgumentParser(description="在不调用外部API的情况下验收确定性RAG路由。")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--chroma", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate(args.db, args.workspace_id, args.chroma)
    content = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(args.output)
    print(json.dumps(
        {
            "questions": [
                {
                    "intent": item["intent"],
                    "citation_count": item["citation_count"],
                    "evidence_sufficient": bool(item["citation_count"]),
                }
                for item in result["questions"]
            ],
            "checks": result["checks"],
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0 if result["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
