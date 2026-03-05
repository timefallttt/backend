import re
from collections import Counter
from typing import Iterable, List

from .schemas import (
    ConsistencyAnalyzeRequest,
    ConsistencyAnalyzeResponse,
    EvidenceRef,
    ItemJudgement,
    RequirementSpec,
    ToolFinding,
)


class ConsistencyService:
    def analyze(self, request: ConsistencyAnalyzeRequest) -> ConsistencyAnalyzeResponse:
        requirement_spec = self._build_requirement_spec(
            request.requirement_text,
            request.acceptance_criteria,
        )

        requirement_items = self._build_requirement_items(
            request.requirement_text,
            request.acceptance_criteria,
        )
        judgements: List[ItemJudgement] = []

        for item in requirement_items:
            evidence = self._find_evidence(item, request)
            match_ratio = self._calculate_match_ratio(item, evidence)
            status, score, confidence, notes = self._to_status_payload(
                match_ratio=match_ratio,
                evidence_count=len(evidence),
            )
            judgements.append(
                ItemJudgement(
                    item=item,
                    status=status,
                    score=score,
                    confidence=confidence,
                    evidence=evidence,
                    notes=notes,
                )
            )

        if not judgements:
            judgements.append(
                ItemJudgement(
                    item="未提供可检验要点",
                    status="not_satisfied",
                    score=0.0,
                    confidence=0.1,
                    evidence=[],
                    notes="请补充验收标准或需求要点后再分析。",
                )
            )

        overall_score = round(sum(item.score for item in judgements) / len(judgements), 3)
        overall_confidence = round(
            sum(item.confidence for item in judgements) / len(judgements),
            3,
        )
        missing_items = [item.item for item in judgements if item.status == "not_satisfied"]
        tool_findings = self._build_tool_findings(request, judgements)
        status = "needs_review" if overall_confidence < 0.6 or len(missing_items) > 0 else "completed"
        summary = self._build_summary(overall_score, overall_confidence, judgements, missing_items)

        return ConsistencyAnalyzeResponse(
            overall_score=overall_score,
            overall_confidence=overall_confidence,
            status=status,
            requirement_spec=requirement_spec,
            judgements=judgements,
            missing_items=missing_items,
            tool_findings=tool_findings,
            summary=summary,
        )

    def _build_requirement_spec(self, requirement_text: str, criteria: List[str]) -> RequirementSpec:
        raw_items = [requirement_text, *criteria]
        intents = [item for item in raw_items if any(token in item for token in ("支持", "实现", "允许", "提供"))]
        constraints = [item for item in raw_items if re.search(r"(不超过|至少|最多|必须|应当|<=|>=|=)", item)]
        exceptions = [item for item in raw_items if any(token in item for token in ("异常", "失败", "错误", "重试", "阻断"))]
        return RequirementSpec(intents=intents, constraints=constraints, exceptions=exceptions)

    def _build_requirement_items(self, requirement_text: str, criteria: List[str]) -> List[str]:
        base_items = [line.strip() for line in re.split(r"[\n。；;]", requirement_text) if line.strip()]
        structured = [item.strip() for item in criteria if item.strip()]
        merged: List[str] = []
        seen = set()
        for item in [*base_items, *structured]:
            if item not in seen:
                seen.add(item)
                merged.append(item)
        return merged

    def _tokenize(self, text: str) -> List[str]:
        cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text.lower())
        tokens = [token.strip() for token in cleaned.split() if len(token.strip()) > 1]
        return tokens

    def _calculate_match_ratio(self, requirement_item: str, evidence: List[EvidenceRef]) -> float:
        if not evidence:
            return 0.0
        item_tokens = self._tokenize(requirement_item)
        if not item_tokens:
            return 0.0
        counter = Counter()
        for ev in evidence:
            counter.update(self._tokenize(ev.reason))
        hit = sum(1 for token in item_tokens if counter[token] > 0)
        return hit / len(item_tokens)

    def _find_evidence(
        self,
        requirement_item: str,
        request: ConsistencyAnalyzeRequest,
    ) -> List[EvidenceRef]:
        requirement_tokens = set(self._tokenize(requirement_item))
        if not requirement_tokens:
            return []

        evidences: List[EvidenceRef] = []
        for snippet in request.candidate_snippets[: request.options.top_k]:
            searchable = f"{snippet.filename}\n{snippet.code}"
            snippet_tokens = set(self._tokenize(searchable))
            if not snippet_tokens:
                continue
            overlap = requirement_tokens.intersection(snippet_tokens)
            ratio = len(overlap) / len(requirement_tokens)
            if ratio >= request.options.keyword_min_overlap:
                matched = ", ".join(sorted(overlap)[:6]) if overlap else "语义相近"
                evidences.append(
                    EvidenceRef(
                        snippet_id=snippet.snippet_id,
                        filename=snippet.filename,
                        start_line=snippet.start_line,
                        end_line=snippet.end_line,
                        reason=f"关键词命中: {matched}",
                    )
                )
        return evidences

    def _to_status_payload(self, match_ratio: float, evidence_count: int) -> tuple[str, float, float, str]:
        if evidence_count == 0:
            return "not_satisfied", 0.0, 0.35, "未检索到支持该要点的代码证据。"
        if match_ratio >= 0.6:
            return "satisfied", 1.0, min(0.95, 0.6 + evidence_count * 0.05), "关键要点与候选代码匹配较高。"
        return "partially_satisfied", 0.5, min(0.8, 0.45 + evidence_count * 0.05), "存在部分证据，建议人工复核边界与异常路径。"

    def _build_tool_findings(
        self,
        request: ConsistencyAnalyzeRequest,
        judgements: Iterable[ItemJudgement],
    ) -> List[ToolFinding]:
        findings: List[ToolFinding] = []
        if not request.options.enable_tool_evidence:
            return findings

        for item in judgements:
            if item.status == "not_satisfied":
                findings.append(
                    ToolFinding(
                        level="warning",
                        message="建议触发 lint/typecheck 或规则检查作为二次证据。",
                        related_item=item.item,
                    )
                )
            if "异常" in item.item and item.status != "satisfied":
                findings.append(
                    ToolFinding(
                        level="info",
                        message="建议补充异常路径的测试用例并回填报告。",
                        related_item=item.item,
                    )
                )
        return findings

    def _build_summary(
        self,
        overall_score: float,
        overall_confidence: float,
        judgements: List[ItemJudgement],
        missing_items: List[str],
    ) -> str:
        total = len(judgements)
        satisfied = len([item for item in judgements if item.status == "satisfied"])
        partial = len([item for item in judgements if item.status == "partially_satisfied"])
        not_ok = len(missing_items)
        return (
            f"共分析 {total} 条要点：满足 {satisfied} 条，部分满足 {partial} 条，不满足 {not_ok} 条。"
            f" 综合得分 {overall_score:.3f}，置信度 {overall_confidence:.3f}。"
        )
