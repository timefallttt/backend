import json
import re
import threading
from datetime import datetime
from typing import Dict, List
from uuid import uuid4

from app.config import REVIEW_TASKS_FILE

from .llm_gateway import LlmReviewGateway
from .schemas import (
    AnalyzeOptions,
    CandidateSnippet,
    ConsistencyAnalyzeRequest,
    ConsistencyAnalyzeResponse,
    EvidencePath,
    EvidencePathNode,
    GraphEvidenceBundle,
    GraphEvidenceStepInput,
    LlmEvidenceGraphNode,
    LlmEvidencePack,
    LlmEvidencePath,
    LlmEvidenceSnippet,
    LlmItemAssessment,
    LlmRequestPreview,
    LlmReviewExecuteRequest,
    LlmReviewResult,
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
        self._llm_gateway = LlmReviewGateway()
        self._ensure_storage()
        self._load_tasks()

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
                    'label': f'{request.snapshot} 证据整理',
                    'status': report.status,
                    'overall_score': report.overall_score,
                    'summary': report.summary,
                    'changed_points': ['创建审阅任务并生成证据包与 LLM 请求预览'],
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
                    'label': f"{task['snapshot']} 重新整理证据",
                    'status': report.status,
                    'overall_score': report.overall_score,
                    'summary': report.summary,
                    'changed_points': ['基于当前候选代码和图证据重新生成请求预览'],
                    'created_at': task['updated_at'],
                },
            )
            self._save_tasks()
            return self._build_task_detail(task)

    def execute_llm_review(self, task_id: str, request: LlmReviewExecuteRequest) -> ReviewTaskDetail:
        with self._lock:
            task = self._get_task_or_raise(task_id)
            report = ReviewReport(**task['report']) if task.get('report') else None
            if not report or not report.llm_request_preview:
                raise ValueError('task does not have an LLM request preview')

            gateway_response = self._llm_gateway.submit_review(
                report.llm_request_preview,
                provider=request.provider,
                api_url=request.api_url,
                api_key=request.api_key,
                model_name=request.model_name,
            )
            llm_result = self._parse_llm_response(
                gateway_response.response_text,
                provider=gateway_response.provider,
                model_name=gateway_response.model_name,
                requirement_items=self._build_requirement_items(task['requirement_text'], task['acceptance_criteria']),
                error_message=gateway_response.error_message,
            )
            updated_report = self._apply_llm_result(report, llm_result)
            task['report'] = updated_report.model_dump()
            task['updated_at'] = self._now()
            task['history'].insert(
                0,
                {
                    'record_id': f'hist-{uuid4().hex[:8]}',
                    'label': '执行 LLM 审阅',
                    'status': updated_report.status,
                    'overall_score': updated_report.overall_score,
                    'summary': updated_report.summary,
                    'changed_points': [llm_result.summary],
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
                    'changed_points': [f'{request.reviewer} 提交了人工复核意见'],
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
        evidence_paths = self._resolve_evidence_paths(request.graph_evidence)
        tool_findings, structural_gaps = self._build_objective_signals(
            selected_snippets=selected_snippets,
            graph_evidence=request.graph_evidence,
        )
        review_focuses = self._build_review_focuses(selected_snippets, request.graph_evidence)
        evidence_pack = self._build_evidence_pack(
            request=request,
            tool_findings=tool_findings,
            structural_gaps=structural_gaps,
        )
        llm_request_preview = self._build_llm_request_preview(evidence_pack)
        summary = f'已整理 {len(requirement_items)} 条需求要点的证据，尚未执行大模型审阅。'

        return ConsistencyAnalyzeResponse(
            overall_score=0.0,
            overall_confidence=0.0,
            status='needs_review',
            requirement_spec=requirement_spec,
            judgements=[],
            missing_items=[],
            tool_findings=tool_findings,
            evidence_paths=evidence_paths,
            structural_gaps=structural_gaps,
            review_focuses=review_focuses,
            evidence_pack=evidence_pack,
            llm_request_preview=llm_request_preview,
            llm_result=None,
            summary=summary,
        )

    def _build_requirement_spec(self, requirement_text: str, criteria: List[str]) -> RequirementSpec:
        raw_items = [requirement_text, *criteria]
        intents = [item for item in raw_items if any(token in item for token in ('支持', '实现', '允许', '提供', '提交', '上传'))]
        constraints = [item for item in raw_items if re.search(r'(不超过|至少|最大|必须|应当|<=|>=|=)', item)]
        exceptions = [item for item in raw_items if any(token in item for token in ('异常', '失败', '错误', '重试', '阻断', '回滚'))]
        return RequirementSpec(intents=intents, constraints=constraints, exceptions=exceptions)

    def _build_requirement_items(self, requirement_text: str, criteria: List[str]) -> List[str]:
        structured = [item.strip() for item in criteria if item.strip()]
        if structured:
            return structured

        base_items = [line.strip() for line in re.split(r'[\n。；;]', requirement_text) if line.strip()]
        merged: List[str] = []
        seen = set()
        for item in base_items:
            if item not in seen:
                seen.add(item)
                merged.append(item)
        return merged

    def _resolve_evidence_paths(self, graph_evidence: GraphEvidenceBundle | None) -> List[EvidencePath]:
        if not graph_evidence or not graph_evidence.paths:
            return []
        paths: List[EvidencePath] = []
        for index, path in enumerate(graph_evidence.paths):
            nodes = [self._to_evidence_path_node(node) for node in path.nodes]
            if not nodes:
                continue
            preview = ' -> '.join(node.label for node in nodes[:3])
            paths.append(
                EvidencePath(
                    path_id=path.path_id,
                    title=f'图证据路径 {index + 1}',
                    summary=f'基于 {graph_evidence.source} 扩展得到的 {path.hop_count} 跳路径：{preview}',
                    supports_items=[],
                    nodes=nodes,
                )
            )
        return paths

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

    def _build_objective_signals(
        self,
        selected_snippets: List[CandidateSnippet],
        graph_evidence: GraphEvidenceBundle | None,
    ) -> tuple[List[ToolFinding], List[str]]:
        findings: List[ToolFinding] = []
        signals: List[str] = []

        if not selected_snippets:
            findings.append(ToolFinding(level='warning', message='当前未提供候选代码片段。'))
            signals.append('当前未提供候选代码片段。')

        if not graph_evidence:
            findings.append(ToolFinding(level='warning', message='当前未提供图证据。'))
            signals.append('当前未提供图证据。')
        else:
            if graph_evidence.summary.matched_seed_count == 0:
                findings.append(ToolFinding(level='warning', message='图扩展未命中任何种子节点。'))
                signals.append('图扩展未命中任何种子节点。')
            if not graph_evidence.paths:
                findings.append(ToolFinding(level='warning', message='当前未形成图路径。'))
                signals.append('当前未形成图路径。')

        if not signals:
            findings.append(ToolFinding(level='info', message='当前原始证据已整理完成，可提交大模型审阅。'))

        unique_signals: List[str] = []
        for signal in signals:
            if signal not in unique_signals:
                unique_signals.append(signal)
        return findings, unique_signals

    def _build_review_focuses(
        self,
        snippets: List[CandidateSnippet],
        graph_evidence: GraphEvidenceBundle | None,
    ) -> List[str]:
        focuses = [f'优先查看 {snippet.filename}:{snippet.start_line}' for snippet in snippets[:2]]
        if graph_evidence:
            for path in graph_evidence.paths[:2]:
                if path.nodes:
                    focuses.append(f'优先查看图路径终点：{path.nodes[-1].name}')
        if not focuses:
            focuses.append('当前尚无可优先定位的证据节点。')
        return focuses

    def _build_evidence_pack(
        self,
        request: ConsistencyAnalyzeRequest,
        tool_findings: List[ToolFinding],
        structural_gaps: List[str],
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
                graph_path = LlmEvidencePath(
                    path_id=path.path_id,
                    title=f'图路径 {path.path_id}',
                    summary=self._summarize_graph_path(path.nodes),
                    supports_items=[],
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
                graph_paths.append(graph_path)

        return LlmEvidencePack(
            requirement_text=request.requirement_text,
            acceptance_criteria=request.acceptance_criteria,
            snippets=snippets,
            graph_paths=graph_paths,
            structural_gaps=structural_gaps,
            tool_findings=tool_findings,
        )

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

    def _build_llm_request_preview(self, evidence_pack: LlmEvidencePack) -> LlmRequestPreview:
        request_body = {
            'system_prompt': (
                '你是需求实现审阅助手。请仅基于给定的原始证据进行判断，不要参考任何先验结论，'
                '不要臆造未提供的实现。你需要对每条需求要点给出 '
                'satisfied/partially_satisfied/not_satisfied 结论、可复核理由、'
                '引用的证据片段和图路径，并指出是否需要人工复核。'
            ),
            'task_context': {
                'requirement_text': evidence_pack.requirement_text,
                'acceptance_criteria': evidence_pack.acceptance_criteria,
                'evidence_summary': {
                    'snippet_count': len(evidence_pack.snippets),
                    'graph_path_count': len(evidence_pack.graph_paths),
                    'acceptance_criteria_count': len(evidence_pack.acceptance_criteria),
                    'tool_finding_count': len(evidence_pack.tool_findings),
                },
                'notes': [
                    '以下输入不包含系统先验判定结果。',
                    '若证据不足，请明确指出缺失证据类型，而不是依据猜测下结论。',
                    '请你自行判断每条验收标准与哪些证据相关，不要假设系统已经完成证据归因。',
                ],
            },
            'evidence_pool': {
                'snippets': [snippet.model_dump() for snippet in evidence_pack.snippets],
                'graph_paths': [path.model_dump() for path in evidence_pack.graph_paths],
                'objective_signals': {
                    'structural_gaps': evidence_pack.structural_gaps,
                    'tool_findings': [finding.model_dump() for finding in evidence_pack.tool_findings],
                },
            },
            'output_contract': {
                'type': 'json_object',
                'required_fields': [
                    'summary',
                    'overall_verdict',
                    'manual_review_needed',
                    'item_assessments',
                ],
                'item_assessment_fields': [
                    'item',
                    'verdict',
                    'reasoning',
                    'supporting_snippet_ids',
                    'supporting_path_ids',
                    'manual_review_needed',
                ],
            },
        }
        return LlmRequestPreview(
            summary=(
                '当前尚未实际调用大模型。请求体已整理为需求上下文与证据池，'
                f'包含 {len(evidence_pack.snippets)} 个代码片段和 {len(evidence_pack.graph_paths)} 条图路径。'
            ),
            request_body=request_body,
        )

    def _parse_llm_response(
        self,
        response_text: str,
        *,
        provider: str,
        model_name: str,
        requirement_items: List[str],
        error_message: str = "",
    ) -> LlmReviewResult:
        if error_message:
            return self._build_llm_error_result(
                provider=provider,
                model_name=model_name,
                requirement_items=requirement_items,
                response_text=response_text,
                error_message=error_message,
            )

        try:
            payload = json.loads(response_text)
        except Exception as exc:
            return self._build_llm_error_result(
                provider=provider,
                model_name=model_name,
                requirement_items=requirement_items,
                response_text=response_text,
                error_message=f'LLM 返回结果不是合法 JSON：{exc}',
            )

        try:
            summary = str(payload['summary']).strip()
            overall_verdict = str(payload['overall_verdict']).strip()
            manual_review_needed = bool(payload['manual_review_needed'])
            raw_assessments = payload['item_assessments']
            if overall_verdict not in {'satisfied', 'partially_satisfied', 'not_satisfied'}:
                raise ValueError('overall_verdict 不在允许范围内')
            if not isinstance(raw_assessments, list):
                raise ValueError('item_assessments 必须为数组')

            item_assessments = [
                LlmItemAssessment(
                    item=str(item['item']),
                    verdict=str(item['verdict']),
                    reasoning=str(item.get('reasoning', '')),
                    supporting_snippet_ids=[str(value) for value in item.get('supporting_snippet_ids', [])],
                    supporting_path_ids=[str(value) for value in item.get('supporting_path_ids', [])],
                    manual_review_needed=bool(item.get('manual_review_needed', manual_review_needed)),
                )
                for item in raw_assessments
            ]
            for item in item_assessments:
                if item.verdict not in {'satisfied', 'partially_satisfied', 'not_satisfied'}:
                    raise ValueError(f'item verdict 不在允许范围内：{item.verdict}')

            return LlmReviewResult(
                status='success',
                provider=provider,
                model_name=model_name,
                summary=summary or 'LLM 审阅已完成。',
                overall_verdict=overall_verdict,
                manual_review_needed=manual_review_needed,
                item_assessments=item_assessments,
                response_text=response_text,
                response_body=payload if isinstance(payload, dict) else {},
                error_message='',
            )
        except Exception as exc:
            return self._build_llm_error_result(
                provider=provider,
                model_name=model_name,
                requirement_items=requirement_items,
                response_text=response_text,
                error_message=f'LLM 返回结果结构不符合约定：{exc}',
            )

    def _build_llm_error_result(
        self,
        *,
        provider: str,
        model_name: str,
        requirement_items: List[str],
        response_text: str,
        error_message: str,
    ) -> LlmReviewResult:
        return LlmReviewResult(
            status='error',
            provider=provider,
            model_name=model_name,
            summary='LLM 审阅未完成，返回结果已按 error 处理。',
            overall_verdict='error',
            manual_review_needed=True,
            item_assessments=[
                LlmItemAssessment(
                    item=item,
                    verdict='error',
                    reasoning=error_message,
                    supporting_snippet_ids=[],
                    supporting_path_ids=[],
                    manual_review_needed=True,
                )
                for item in requirement_items
            ],
            response_text=response_text,
            response_body={},
            error_message=error_message,
        )

    def _apply_llm_result(self, report: ReviewReport, llm_result: LlmReviewResult) -> ReviewReport:
        judgement_map = {
            'satisfied': (1.0, False),
            'partially_satisfied': (0.5, True),
            'not_satisfied': (0.0, True),
            'error': (0.0, True),
        }
        snippet_index = {
            snippet.snippet_id: snippet
            for snippet in (report.evidence_pack.snippets if report.evidence_pack else [])
        }

        judgements = []
        for assessment in llm_result.item_assessments:
            score, manual_review_needed = judgement_map[assessment.verdict]
            evidence = []
            for snippet_id in assessment.supporting_snippet_ids:
                snippet = snippet_index.get(snippet_id)
                if not snippet:
                    continue
                evidence.append(
                    {
                        'snippet_id': snippet.snippet_id,
                        'filename': snippet.filename,
                        'start_line': snippet.start_line,
                        'end_line': snippet.end_line,
                        'reason': snippet.reason,
                    }
                )
            judgements.append(
                {
                    'item': assessment.item,
                    'status': assessment.verdict,
                    'score': score,
                    'confidence': 0.0,
                    'evidence': evidence,
                    'notes': assessment.reasoning,
                }
            )

        success_scores = [item['score'] for item in judgements if item['status'] != 'error']
        overall_score = round(sum(success_scores) / len(success_scores), 3) if success_scores else 0.0
        status = 'completed' if llm_result.status == 'success' else 'needs_review'
        summary = llm_result.summary

        return report.model_copy(
            update={
                'overall_score': overall_score,
                'overall_confidence': 0.0,
                'status': status,
                'judgements': judgements,
                'llm_result': llm_result,
                'summary': summary,
            }
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
