import json
import re
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List
from uuid import uuid4

from app.config import REVIEW_TASKS_FILE

from .schemas import (
    AnalyzeOptions,
    CandidateSnippet,
    ConsistencyAnalyzeRequest,
    ConsistencyAnalyzeResponse,
    EvidencePath,
    EvidencePathNode,
    EvidenceRef,
    GraphEvidenceBundle,
    GraphEvidenceStepInput,
    ItemJudgement,
    LlmEvidenceGraphNode,
    LlmEvidencePack,
    LlmEvidencePath,
    LlmEvidenceRequirementItem,
    LlmEvidenceSnippet,
    LlmRequestPreview,
    RequirementSpec,
    ReviewDashboardResponse,
    ReviewDashboardStats,
    ReviewFeedback,
    ReviewFeedbackRequest,
    ReviewHistoryRecord,
    ReviewHistoryResponse,
    ReviewReport,
    ReviewTaskCreateRequest,
    ReviewTaskDetail,
    ReviewTaskListResponse,
    ReviewTaskSummary,
    ToolFinding,
)


class ConsistencyService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: Dict[str, dict] = {}
        self._ensure_storage()
        self._load_tasks()
        if not self._tasks:
            self._bootstrap_demo_data()

    def get_dashboard(self) -> ReviewDashboardResponse:
        tasks = [self._build_task_summary(task) for task in self._tasks.values()]
        tasks.sort(key=lambda item: item.updated_at, reverse=True)
        completed = len([task for task in tasks if task.status == 'completed'])
        needs_review = len([task for task in tasks if task.status == 'needs_review'])
        avg_score = round(sum(task.overall_score for task in tasks) / len(tasks), 3) if tasks else 0.0
        return ReviewDashboardResponse(
            stats=ReviewDashboardStats(
                total_tasks=len(tasks),
                needs_review_tasks=needs_review,
                completed_tasks=completed,
                avg_score=avg_score,
            ),
            tasks=tasks,
        )

    def list_tasks(self) -> ReviewTaskListResponse:
        tasks = [self._build_task_summary(task) for task in self._tasks.values()]
        tasks.sort(key=lambda item: item.updated_at, reverse=True)
        return ReviewTaskListResponse(tasks=tasks)

    def get_task(self, task_id: str) -> ReviewTaskDetail:
        task = self._get_task_or_raise(task_id)
        return self._build_task_detail(task)

    def delete_task(self, task_id: str) -> None:
        with self._lock:
            self._get_task_or_raise(task_id)
            del self._tasks[task_id]
            self._save_tasks()

    def create_task(self, request: ReviewTaskCreateRequest) -> ReviewTaskDetail:
        now = self._now()
        task_id = f'task-{uuid4().hex[:8]}'
        report = self.analyze(
            ConsistencyAnalyzeRequest(
                requirement_text=request.requirement_text,
                acceptance_criteria=request.acceptance_criteria,
                candidate_snippets=request.candidate_snippets,
                graph_evidence=request.graph_evidence,
                options=request.options,
            )
        )
        task = {
            'task_id': task_id,
            'requirement_id': request.requirement_id,
            'title': request.title,
            'requirement_text': request.requirement_text,
            'acceptance_criteria': request.acceptance_criteria,
            'repo_name': request.repo_name,
            'snapshot': request.snapshot,
            'business_tag': request.business_tag,
            'priority': request.priority,
            'owner': request.owner,
            'notes': request.notes,
            'candidate_snippets': [snippet.model_dump() for snippet in request.candidate_snippets],
            'graph_evidence': request.graph_evidence.model_dump() if request.graph_evidence else None,
            'options': request.options.model_dump(),
            'report': report.model_dump(),
            'feedback_entries': [],
            'history': [
                {
                    'record_id': f'hist-{uuid4().hex[:8]}',
                    'label': f'{request.snapshot} 初次审阅',
                    'status': report.status,
                    'overall_score': report.overall_score,
                    'summary': report.summary,
                    'changed_points': ['创建审阅任务并生成首版报告'],
                    'created_at': now,
                }
            ],
            'updated_at': now,
        }
        with self._lock:
            self._tasks[task_id] = task
            self._save_tasks()
        return self._build_task_detail(task)

    def analyze_task(self, task_id: str) -> ReviewTaskDetail:
        with self._lock:
            task = self._get_task_or_raise(task_id)
            request = ConsistencyAnalyzeRequest(
                requirement_text=task['requirement_text'],
                acceptance_criteria=task['acceptance_criteria'],
                candidate_snippets=[CandidateSnippet(**snippet) for snippet in task['candidate_snippets']],
                graph_evidence=GraphEvidenceBundle(**task['graph_evidence']) if task.get('graph_evidence') else None,
                options=AnalyzeOptions(**task['options']),
            )
            report = self.analyze(request)
            task['report'] = report.model_dump()
            task['updated_at'] = self._now()
            task['history'].insert(
                0,
                {
                    'record_id': f'hist-{uuid4().hex[:8]}',
                    'label': f"{task['snapshot']} 重新审阅",
                    'status': report.status,
                    'overall_score': report.overall_score,
                    'summary': report.summary,
                    'changed_points': ['基于当前候选代码和图证据重新生成报告'],
                    'created_at': task['updated_at'],
                },
            )
            self._save_tasks()
            return self._build_task_detail(task)

    def get_history(self, task_id: str) -> ReviewHistoryResponse:
        task = self._get_task_or_raise(task_id)
        return ReviewHistoryResponse(
            task_id=task_id,
            records=[ReviewHistoryRecord(**record) for record in task['history']],
        )

    def submit_feedback(self, task_id: str, request: ReviewFeedbackRequest) -> ReviewTaskDetail:
        with self._lock:
            task = self._get_task_or_raise(task_id)
            feedback = ReviewFeedback(
                feedback_id=f'fb-{uuid4().hex[:8]}',
                judgement_item=request.judgement_item,
                decision=request.decision,
                comment=request.comment,
                reviewer=request.reviewer,
                created_at=self._now(),
            )
            task['feedback_entries'].insert(0, feedback.model_dump())
            task['history'].insert(
                0,
                {
                    'record_id': f'hist-{uuid4().hex[:8]}',
                    'label': f'{request.reviewer} 提交复核',
                    'status': self._task_status(task),
                    'overall_score': self._task_score(task),
                    'summary': f'人工复核：{request.judgement_item}',
                    'changed_points': [f'{request.reviewer} 将结论标记为 {request.decision}'],
                    'created_at': feedback.created_at,
                },
            )
            task['updated_at'] = feedback.created_at
            self._save_tasks()
            return self._build_task_detail(task)

    def analyze(self, request: ConsistencyAnalyzeRequest) -> ConsistencyAnalyzeResponse:
        requirement_spec = self._build_requirement_spec(request.requirement_text, request.acceptance_criteria)
        requirement_items = self._build_requirement_items(request.requirement_text, request.acceptance_criteria)
        selected_snippets = [snippet for snippet in request.candidate_snippets if snippet.selected]
        judgements: List[ItemJudgement] = []

        for item in requirement_items:
            evidence = self._find_evidence(item, selected_snippets, request.options)
            snippet_ratio = self._calculate_match_ratio(item, evidence)
            graph_ratio, graph_path_count, graph_note = self._find_graph_support(item, request.graph_evidence)
            status, score, confidence, notes = self._to_status_payload(
                snippet_ratio=snippet_ratio,
                evidence_count=len(evidence),
                graph_ratio=graph_ratio,
                graph_path_count=graph_path_count,
            )
            if graph_note:
                notes = f'{notes} {graph_note}'.strip()
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
                    item='未提取到可检验要点',
                    status='not_satisfied',
                    score=0.0,
                    confidence=0.1,
                    evidence=[],
                    notes='当前任务缺少可用于审阅的需求要点。',
                )
            )

        overall_score = round(sum(item.score for item in judgements) / len(judgements), 3)
        overall_confidence = round(sum(item.confidence for item in judgements) / len(judgements), 3)
        missing_items = [item.item for item in judgements if item.status == 'not_satisfied']
        evidence_paths = self._resolve_evidence_paths(request.graph_evidence, request.requirement_text, requirement_items, selected_snippets)
        if request.graph_evidence and request.graph_evidence.summary.matched_seed_count:
            overall_confidence = round(min(0.98, overall_confidence + 0.08), 3)

        tool_findings = self._build_tool_findings(request.options.enable_tool_evidence, judgements, request.graph_evidence)
        structural_gaps = self._build_structural_gaps(judgements, evidence_paths, request.graph_evidence)
        review_focuses = self._build_review_focuses(selected_snippets, missing_items, request.graph_evidence)
        evidence_pack = self._build_evidence_pack(
            request=request,
            judgements=judgements,
            evidence_paths=evidence_paths,
            structural_gaps=structural_gaps,
            tool_findings=tool_findings,
        )
        llm_request_preview = self._build_llm_request_preview(evidence_pack)
        status = 'needs_review' if overall_confidence < 0.6 or missing_items else 'completed'
        summary = self._build_summary(overall_score, overall_confidence, judgements, missing_items)

        return ConsistencyAnalyzeResponse(
            overall_score=overall_score,
            overall_confidence=overall_confidence,
            status=status,
            requirement_spec=requirement_spec,
            judgements=judgements,
            missing_items=missing_items,
            tool_findings=tool_findings,
            evidence_paths=evidence_paths,
            structural_gaps=structural_gaps,
            review_focuses=review_focuses,
            evidence_pack=evidence_pack,
            llm_request_preview=llm_request_preview,
            summary=summary,
        )

    def _build_requirement_spec(self, requirement_text: str, criteria: List[str]) -> RequirementSpec:
        raw_items = [requirement_text, *criteria]
        intents = [item for item in raw_items if any(token in item for token in ('支持', '实现', '允许', '提供', '提交', '上传'))]
        constraints = [item for item in raw_items if re.search(r'(不超过|至少|最大|必须|应当|<=|>=|=)', item)]
        exceptions = [item for item in raw_items if any(token in item for token in ('异常', '失败', '错误', '重试', '阻断', '回滚'))]
        return RequirementSpec(intents=intents, constraints=constraints, exceptions=exceptions)

    def _build_requirement_items(self, requirement_text: str, criteria: List[str]) -> List[str]:
        base_items = [line.strip() for line in re.split(r'[\n。；;]', requirement_text) if line.strip()]
        structured = [item.strip() for item in criteria if item.strip()]
        merged: List[str] = []
        seen = set()
        for item in [*base_items, *structured]:
            if item not in seen:
                seen.add(item)
                merged.append(item)
        return merged

    def _tokenize(self, text: str) -> List[str]:
        cleaned = re.sub(r'[^\w\u4e00-\u9fff]+', ' ', text.lower())
        return [token.strip() for token in cleaned.split() if len(token.strip()) > 1]

    def _find_evidence(self, requirement_item: str, snippets: List[CandidateSnippet], options: AnalyzeOptions) -> List[EvidenceRef]:
        requirement_tokens = set(self._tokenize(requirement_item))
        if not requirement_tokens:
            return []

        evidences: List[EvidenceRef] = []
        for snippet in snippets[: options.top_k]:
            searchable = f'{snippet.filename}\n{snippet.code}\n{snippet.recall_reason}'
            snippet_tokens = set(self._tokenize(searchable))
            if not snippet_tokens:
                continue
            overlap = requirement_tokens.intersection(snippet_tokens)
            ratio = len(overlap) / len(requirement_tokens)
            if ratio >= options.keyword_min_overlap:
                matched = ', '.join(sorted(overlap)[:6]) if overlap else '语义相关'
                evidences.append(
                    EvidenceRef(
                        snippet_id=snippet.snippet_id,
                        filename=snippet.filename,
                        start_line=snippet.start_line,
                        end_line=snippet.end_line,
                        reason=f'关键词命中：{matched}',
                    )
                )
        return evidences

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

    def _find_graph_support(
        self,
        requirement_item: str,
        graph_evidence: GraphEvidenceBundle | None,
    ) -> tuple[float, int, str]:
        if not graph_evidence:
            return 0.0, 0, ''

        requirement_tokens = set(self._tokenize(requirement_item))
        if not requirement_tokens:
            return 0.0, 0, ''

        matched_overlaps: List[set[str]] = []
        best_ratio = 0.0

        for path in graph_evidence.paths:
            path_fragments = [path.path_id]
            for node in path.nodes:
                path_fragments.extend(
                    [
                        node.node_type,
                        node.name,
                        node.path,
                        node.relation_from_prev or '',
                    ]
                )
            path_tokens = set(self._tokenize(' '.join(fragment for fragment in path_fragments if fragment)))
            if not path_tokens:
                continue
            overlap = requirement_tokens.intersection(path_tokens)
            if not overlap:
                continue
            matched_overlaps.append(overlap)
            best_ratio = max(best_ratio, len(overlap) / len(requirement_tokens))

        if graph_evidence.hints:
            hint_tokens = set(self._tokenize(' '.join(graph_evidence.hints)))
            hint_overlap = requirement_tokens.intersection(hint_tokens)
            if hint_overlap:
                matched_overlaps.append(hint_overlap)
                best_ratio = max(best_ratio, len(hint_overlap) / len(requirement_tokens))

        if not matched_overlaps:
            return 0.0, 0, '图证据未直接命中该要点。'

        merged_overlap = sorted({token for overlap in matched_overlaps for token in overlap})
        preview = '、'.join(merged_overlap[:6])
        return best_ratio, len(matched_overlaps), f'图证据命中 {len(matched_overlaps)} 条路径/提示，关键词包括：{preview}。'

    def _to_status_payload(
        self,
        snippet_ratio: float,
        evidence_count: int,
        graph_ratio: float,
        graph_path_count: int,
    ) -> tuple[str, float, float, str]:
        if evidence_count == 0 and graph_path_count == 0:
            return 'not_satisfied', 0.0, 0.35, '未检索到直接支持该要点的代码证据，建议结合图扩展继续补充。'

        combined_ratio = max(
            snippet_ratio,
            min(1.0, snippet_ratio * 0.7 + graph_ratio * 0.3),
        )
        if evidence_count == 0 and graph_path_count > 0:
            combined_ratio = max(combined_ratio, graph_ratio * 0.8)

        if combined_ratio >= 0.65 or (snippet_ratio >= 0.45 and graph_path_count > 0):
            confidence = min(0.96, 0.58 + evidence_count * 0.05 + graph_path_count * 0.03)
            return 'satisfied', 1.0, confidence, '候选代码与图扩展路径共同支持该要点。'

        confidence = min(0.84, 0.42 + evidence_count * 0.05 + graph_path_count * 0.03)
        if evidence_count == 0 and graph_path_count > 0:
            return 'partially_satisfied', 0.5, confidence, '当前主要依赖图扩展证据，仍需补充直接代码片段以确认实现细节。'
        return 'partially_satisfied', 0.5, confidence, '已有部分证据，但仍需补充异常路径或约束校验节点。'

    def _resolve_evidence_paths(
        self,
        graph_evidence: GraphEvidenceBundle | None,
        requirement_text: str,
        requirement_items: List[str],
        snippets: List[CandidateSnippet],
    ) -> List[EvidencePath]:
        if graph_evidence and graph_evidence.paths:
            return self._build_graph_evidence_paths(graph_evidence, requirement_items)
        return self._build_placeholder_paths(requirement_text, requirement_items, snippets)

    def _build_placeholder_paths(
        self,
        requirement_text: str,
        requirement_items: List[str],
        snippets: List[CandidateSnippet],
    ) -> List[EvidencePath]:
        paths: List[EvidencePath] = []
        for index, snippet in enumerate(snippets[:3]):
            first_item = requirement_items[index] if index < len(requirement_items) else requirement_text
            filename = snippet.filename.split('/')[-1]
            function_label = self._guess_function_label(snippet.code, filename)
            paths.append(
                EvidencePath(
                    path_id=f'path-{index + 1}',
                    title=f'证据路径 {index + 1}',
                    summary=f'从候选种子 {filename} 出发，扩展到关键实现节点以支持要点“{first_item}”。',
                    supports_items=[first_item],
                    nodes=[
                        EvidencePathNode(
                            node_id=f'req-{index + 1}',
                            label='需求要点',
                            node_type='requirement',
                            detail=first_item,
                        ),
                        EvidencePathNode(
                            node_id=f'file-{index + 1}',
                            label=filename,
                            node_type='file',
                            relation_from_prev='LOCATES',
                            detail='候选种子代码所在文件',
                        ),
                        EvidencePathNode(
                            node_id=f'func-{index + 1}',
                            label=function_label,
                            node_type='function',
                            relation_from_prev='CONTAINS',
                            detail='由文件节点扩展出的关键函数',
                        ),
                    ],
                )
            )
        if paths:
            return paths
        return [
            EvidencePath(
                path_id='path-empty',
                title='证据路径占位',
                summary='当前未导入候选代码，系统未能构造示例证据路径。',
                supports_items=[],
                nodes=[],
            )
        ]

    def _build_graph_evidence_paths(self, graph_evidence: GraphEvidenceBundle, requirement_items: List[str]) -> List[EvidencePath]:
        paths: List[EvidencePath] = []
        for index, path in enumerate(graph_evidence.paths):
            supports_items = [requirement_items[index % len(requirement_items)]] if requirement_items else []
            nodes = [self._to_evidence_path_node(node) for node in path.nodes]
            if not nodes:
                continue
            preview = ' -> '.join(node.label for node in nodes[:3])
            paths.append(
                EvidencePath(
                    path_id=path.path_id,
                    title=f'图证据路径 {index + 1}',
                    summary=f'基于 {graph_evidence.source} 扩展得到的 {path.hop_count} 跳路径：{preview}',
                    supports_items=supports_items,
                    nodes=nodes,
                )
            )
        return paths or [
            EvidencePath(
                path_id='path-empty',
                title='图证据未命中',
                summary='已执行图扩展，但当前未形成可展示的证据路径。',
                supports_items=[],
                nodes=[],
            )
        ]

    def _to_evidence_path_node(self, node: GraphEvidenceStepInput) -> EvidencePathNode:
        node_type_map = {
            'Repository': 'service',
            'File': 'file',
            'Class': 'class',
            'Function': 'function',
        }
        return EvidencePathNode(
            node_id=node.node_id,
            label=node.name,
            node_type=node_type_map.get(node.node_type, 'service'),
            relation_from_prev=node.relation_from_prev,
            detail=node.path or node.name,
        )

    def _build_tool_findings(
        self,
        enabled: bool,
        judgements: Iterable[ItemJudgement],
        graph_evidence: GraphEvidenceBundle | None,
    ) -> List[ToolFinding]:
        if not enabled:
            return []

        findings: List[ToolFinding] = []
        for item in judgements:
            if item.status == 'not_satisfied':
                findings.append(
                    ToolFinding(
                        level='warning',
                        message='建议对该要点补充 lint、typecheck 或规则检查结果。',
                        related_item=item.item,
                    )
                )
            elif item.status == 'partially_satisfied':
                findings.append(
                    ToolFinding(
                        level='info',
                        message='建议人工确认图扩展得到的关键调用链是否完整。',
                        related_item=item.item,
                    )
                )
        if graph_evidence and graph_evidence.hints:
            findings.append(
                ToolFinding(
                    level='info',
                    message=f'图查询提示：{graph_evidence.hints[0]}',
                    related_item=None,
                )
            )
        return findings

    def _build_structural_gaps(
        self,
        judgements: List[ItemJudgement],
        evidence_paths: List[EvidencePath],
        graph_evidence: GraphEvidenceBundle | None,
    ) -> List[str]:
        gaps = [f'缺少支持“{item.item}”的约束或异常处理节点' for item in judgements if item.status == 'not_satisfied']
        if not evidence_paths or not evidence_paths[0].nodes:
            gaps.append('尚未形成可供复核的关键证据路径。')
        if graph_evidence and graph_evidence.summary.matched_seed_count == 0:
            gaps.append('图扩展未命中任何种子节点，需要检查 seed 路径、名称或签名条件。')
        if not gaps:
            gaps.append('当前报告未发现明显结构性断点，但仍建议人工确认关键路径。')
        return gaps

    def _build_review_focuses(
        self,
        snippets: List[CandidateSnippet],
        missing_items: List[str],
        graph_evidence: GraphEvidenceBundle | None,
    ) -> List[str]:
        focuses = [f'优先复核 {snippet.filename}:{snippet.start_line}' for snippet in snippets[:2]]
        if graph_evidence:
            for path in graph_evidence.paths[:2]:
                if path.nodes:
                    focuses.append(f'复核图路径终点：{path.nodes[-1].name}')
        focuses.extend([f'补查要点：{item}' for item in missing_items[:2]])
        if not focuses:
            focuses.append('建议先补充候选代码，再开展审阅。')
        return focuses

    def _build_summary(
        self,
        overall_score: float,
        overall_confidence: float,
        judgements: List[ItemJudgement],
        missing_items: List[str],
    ) -> str:
        total = len(judgements)
        satisfied = len([item for item in judgements if item.status == 'satisfied'])
        partial = len([item for item in judgements if item.status == 'partially_satisfied'])
        return (
            f'共审阅 {total} 条要点，满足 {satisfied} 条，部分满足 {partial} 条，不满足 {len(missing_items)} 条。'
            f' 当前综合得分 {overall_score:.3f}，置信度 {overall_confidence:.3f}。'
        )

    def _build_evidence_pack(
        self,
        request: ConsistencyAnalyzeRequest,
        judgements: List[ItemJudgement],
        evidence_paths: List[EvidencePath],
        structural_gaps: List[str],
        tool_findings: List[ToolFinding],
    ) -> LlmEvidencePack:
        snippets = [
            LlmEvidenceSnippet(
                snippet_id=snippet.snippet_id,
                source='diff_seed' if snippet.source == 'workitem_diff' else 'candidate',
                filename=snippet.filename,
                start_line=snippet.start_line,
                end_line=snippet.end_line,
                code=snippet.code,
                reason=snippet.recall_reason,
            )
            for snippet in request.candidate_snippets
            if snippet.selected
        ]

        graph_paths: List[LlmEvidencePath] = []
        if request.graph_evidence:
            for path in request.graph_evidence.paths:
                supports_items = self._infer_supported_items(path.path_id, path.nodes, judgements)
                graph_paths.append(
                    LlmEvidencePath(
                        path_id=path.path_id,
                        title=f'图路径 {path.path_id}',
                        summary=self._summarize_graph_path(path.nodes),
                        supports_items=supports_items,
                        nodes=[
                            LlmEvidenceGraphNode(
                                node_id=node.node_id,
                                label=node.name,
                                node_type=self._map_graph_node_type(node.node_type),
                                path=node.path,
                                start_line=node.start_line,
                                end_line=node.end_line,
                                signature=node.signature,
                                code_excerpt=node.code_excerpt,
                                relation_from_prev=node.relation_from_prev,
                            )
                            for node in path.nodes
                        ],
                    )
                )
        else:
            for path in evidence_paths:
                graph_paths.append(
                    LlmEvidencePath(
                        path_id=path.path_id,
                        title=path.title,
                        summary=path.summary,
                        supports_items=path.supports_items,
                        nodes=[
                            LlmEvidenceGraphNode(
                                node_id=node.node_id,
                                label=node.label,
                                node_type=node.node_type,
                                path=node.detail if node.node_type in {'file', 'class', 'function', 'service'} else '',
                                relation_from_prev=node.relation_from_prev,
                            )
                            for node in path.nodes
                        ],
                    )
                )

        requirement_items = [
            LlmEvidenceRequirementItem(
                item=judgement.item,
                status_hint=judgement.status,
                snippet_ids=[evidence.snippet_id for evidence in judgement.evidence],
                path_ids=[path.path_id for path in graph_paths if judgement.item in path.supports_items],
                negative_signals=self._build_negative_signals(judgement, graph_paths, structural_gaps),
            )
            for judgement in judgements
        ]

        return LlmEvidencePack(
            requirement_text=request.requirement_text,
            acceptance_criteria=request.acceptance_criteria,
            snippets=snippets,
            graph_paths=graph_paths,
            requirement_items=requirement_items,
            structural_gaps=structural_gaps,
            tool_findings=tool_findings,
        )

    def _infer_supported_items(
        self,
        path_id: str,
        nodes: List[GraphEvidenceStepInput],
        judgements: List[ItemJudgement],
    ) -> List[str]:
        node_tokens = set()
        for node in nodes:
            node_tokens.update(self._tokenize(f'{node.name} {node.path} {node.signature}'))
        if not node_tokens:
            return []
        supported_items: List[str] = []
        for judgement in judgements:
            item_tokens = set(self._tokenize(judgement.item))
            if not item_tokens:
                continue
            overlap = item_tokens.intersection(node_tokens)
            if overlap:
                supported_items.append(judgement.item)
        if not supported_items and judgements:
            supported_items.append(judgements[hash(path_id) % len(judgements)].item)
        return supported_items

    def _summarize_graph_path(self, nodes: List[GraphEvidenceStepInput]) -> str:
        if not nodes:
            return '图查询未返回有效节点。'
        preview = ' -> '.join(node.name for node in nodes[:4])
        return f'结构路径：{preview}'

    def _map_graph_node_type(self, node_type: str) -> str:
        node_type_map = {
            'Repository': 'service',
            'File': 'file',
            'Class': 'class',
            'Function': 'function',
        }
        return node_type_map.get(node_type, 'service')

    def _build_negative_signals(
        self,
        judgement: ItemJudgement,
        graph_paths: List[LlmEvidencePath],
        structural_gaps: List[str],
    ) -> List[str]:
        signals: List[str] = []
        if not judgement.evidence:
            signals.append('未命中直接代码片段')
        if not any(judgement.item in path.supports_items for path in graph_paths):
            signals.append('未形成对应图路径')
        if judgement.status != 'satisfied':
            signals.extend(gap for gap in structural_gaps if judgement.item in gap)
        return signals[:4]

    def _build_llm_request_preview(self, evidence_pack: LlmEvidencePack) -> LlmRequestPreview:
        request_body = {
            'provider': 'pending',
            'model': 'pending',
            'messages': [
                {
                    'role': 'system',
                    'content': (
                        '你是需求实现审阅助手。请基于提供的需求、验收标准、代码片段、'
                        '图路径和缺口信号，判断每条需求要点是否满足，并给出可复核理由。'
                    ),
                },
                {
                    'role': 'user',
                    'content': {
                        'requirement_text': evidence_pack.requirement_text,
                        'acceptance_criteria': evidence_pack.acceptance_criteria,
                        'requirement_items': [item.model_dump() for item in evidence_pack.requirement_items],
                        'snippets': [snippet.model_dump() for snippet in evidence_pack.snippets],
                        'graph_paths': [path.model_dump() for path in evidence_pack.graph_paths],
                        'structural_gaps': evidence_pack.structural_gaps,
                        'tool_findings': [finding.model_dump() for finding in evidence_pack.tool_findings],
                    },
                },
            ],
            'response_format': {
                'type': 'json_object',
                'expected_fields': [
                    'summary',
                    'item_assessments',
                    'overall_verdict',
                    'manual_review_needed',
                ],
            },
        }
        return LlmRequestPreview(
            summary='当前尚未实际调用大模型，以下内容为该任务拟提交给大模型的请求体。',
            request_body=request_body,
        )

    def _build_task_summary(self, task: dict) -> ReviewTaskSummary:
        report = ReviewReport(**task['report']) if task.get('report') else None
        return ReviewTaskSummary(
            task_id=task['task_id'],
            requirement_id=task['requirement_id'],
            title=task['title'],
            repo_name=task['repo_name'],
            snapshot=task['snapshot'],
            business_tag=task['business_tag'],
            priority=task['priority'],
            status=self._task_status(task),
            overall_score=report.overall_score if report else 0.0,
            updated_at=task['updated_at'],
        )

    def _build_task_detail(self, task: dict) -> ReviewTaskDetail:
        report = ReviewReport(**task['report']) if task.get('report') else None
        feedback_entries = [ReviewFeedback(**entry) for entry in task['feedback_entries']]
        return ReviewTaskDetail(
            task=self._build_task_summary(task),
            requirement_text=task['requirement_text'],
            acceptance_criteria=task['acceptance_criteria'],
            owner=task['owner'],
            notes=task['notes'],
            candidate_snippets=[CandidateSnippet(**snippet) for snippet in task['candidate_snippets']],
            graph_evidence=GraphEvidenceBundle(**task['graph_evidence']) if task.get('graph_evidence') else None,
            report=report,
            feedback_entries=feedback_entries,
        )

    def _task_status(self, task: dict) -> str:
        return task['report']['status'] if task.get('report') else 'draft'

    def _task_score(self, task: dict) -> float:
        return float(task['report']['overall_score']) if task.get('report') else 0.0

    def _get_task_or_raise(self, task_id: str) -> dict:
        task = self._tasks.get(task_id)
        if not task:
            raise ValueError(f'task {task_id} not found')
        return task

    def _guess_function_label(self, code: str, filename: str) -> str:
        function_match = re.search(r'(function|async function|class)\s+([A-Za-z_][A-Za-z0-9_]*)', code)
        if function_match:
            return function_match.group(2)
        return filename.replace('.ts', '').replace('.ets', '')

    def _bootstrap_demo_data(self) -> None:
        demo_request = ReviewTaskCreateRequest(
            requirement_id='R-AVATAR-01',
            title='头像上传前压缩与失败处理',
            requirement_text='用户上传头像时，应先压缩图片，再执行上传，并在失败时提供清晰提示。',
            acceptance_criteria=[
                '图片长边不超过 1024px',
                '压缩后文件大小不超过 300KB',
                '压缩失败时提示用户并阻断上传',
                '上传失败时允许最多重试 3 次',
            ],
            repo_name='profile-center',
            snapshot='main@7f32ab1',
            business_tag='用户资料',
            priority='high',
            owner='demo-user',
            notes='该任务用于展示需求审阅工作台的完整链路。',
            candidate_snippets=[
                CandidateSnippet(
                    snippet_id='seed-1',
                    filename='src/profile/ProfilePage.ets',
                    code='async onSelectAvatar(file) { const compressed = await avatarService.compress(file); await avatarService.upload(compressed) }',
                    start_line=18,
                    end_line=24,
                    recall_reason='页面入口逻辑',
                    source='retrieval',
                    selected=True,
                ),
                CandidateSnippet(
                    snippet_id='seed-2',
                    filename='src/profile/AvatarService.ets',
                    code='async upload(file) { return retryUpload(3, () => api.upload(file)) }',
                    start_line=6,
                    end_line=12,
                    recall_reason='头像上传服务',
                    source='retrieval',
                    selected=True,
                ),
            ],
            graph_evidence=GraphEvidenceBundle(
                source='artifact',
                hints=['演示任务使用占位图证据'],
                summary={
                    'matched_seed_count': 2,
                    'expanded_node_count': 5,
                    'expanded_edge_count': 4,
                    'evidence_path_count': 2,
                },
                paths=[
                    {
                        'path_id': 'path-demo-1',
                        'hop_count': 2,
                        'nodes': [
                            {'node_id': 'file:ProfilePage', 'node_type': 'File', 'name': 'ProfilePage.ets', 'path': 'src/profile/ProfilePage.ets'},
                            {'node_id': 'func:onSelectAvatar', 'node_type': 'Function', 'name': 'onSelectAvatar', 'path': 'src/profile/ProfilePage.ets', 'relation_from_prev': 'CONTAINS'},
                            {'node_id': 'func:compress', 'node_type': 'Function', 'name': 'compress', 'path': 'src/profile/AvatarService.ets', 'relation_from_prev': 'CALLS'},
                        ],
                    },
                    {
                        'path_id': 'path-demo-2',
                        'hop_count': 2,
                        'nodes': [
                            {'node_id': 'func:onSelectAvatar', 'node_type': 'Function', 'name': 'onSelectAvatar', 'path': 'src/profile/ProfilePage.ets'},
                            {'node_id': 'func:upload', 'node_type': 'Function', 'name': 'upload', 'path': 'src/profile/AvatarService.ets', 'relation_from_prev': 'CALLS'},
                            {'node_id': 'func:retryUpload', 'node_type': 'Function', 'name': 'retryUpload', 'path': 'src/profile/UploadRetry.ets', 'relation_from_prev': 'CALLS'},
                        ],
                    },
                ],
            ),
        )
        self.create_task(demo_request)

    def _ensure_storage(self) -> None:
        REVIEW_TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)

    def _load_tasks(self) -> None:
        if not REVIEW_TASKS_FILE.exists():
            return
        payload = json.loads(REVIEW_TASKS_FILE.read_text(encoding='utf-8'))
        self._tasks = payload.get('tasks', {}) if isinstance(payload, dict) else {}

    def _save_tasks(self) -> None:
        REVIEW_TASKS_FILE.write_text(
            json.dumps({'tasks': self._tasks}, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )

    def _now(self) -> str:
        return datetime.now().isoformat(timespec='seconds')
