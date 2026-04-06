import json
import logging
import re
import threading
from datetime import datetime
from typing import Dict, List
from uuid import uuid4

from app.config import (
    LLM_REVIEW_MODEL_NAME,
    LLM_REVIEW_PROVIDER,
    REVIEW_TASKS_FILE,
)

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
    LlmDimensionAssessment,
    LlmDimensionAssessmentSet,
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

logger = logging.getLogger(__name__)


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

    def delete_tasks_by_scope(self, repo_name: str, snapshot: str) -> int:
        with self._lock:
            target_ids = [
                task_id
                for task_id, task in self._tasks.items()
                if task.get('repo_name') == repo_name and task.get('snapshot') == snapshot
            ]
            if not target_ids:
                return 0
            for task_id in target_ids:
                del self._tasks[task_id]
            self._save_tasks()
            return len(target_ids)

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
            if report.status == 'reviewing':
                return self._build_task_detail(task)

            updated_report = report.model_copy(
                update={
                    'status': 'reviewing',
                    'summary': 'LLM 审阅进行中，请稍候刷新结果。',
                }
            )
            task['report'] = updated_report.model_dump()
            task['updated_at'] = self._now()
            task['history'].insert(
                0,
                {
                    'record_id': f'hist-{uuid4().hex[:8]}',
                    'label': '发起 LLM 审阅',
                    'status': updated_report.status,
                    'overall_score': updated_report.overall_score,
                    'summary': updated_report.summary,
                    'changed_points': ['已提交到后端执行，等待 LLM 返回结果'],
                    'created_at': task['updated_at'],
                },
            )
            self._save_tasks()
            task_detail = self._build_task_detail(task)

        threading.Thread(
            target=self._execute_llm_review_background,
            args=(task_id,),
            daemon=True,
        ).start()
        return task_detail

    def _execute_llm_review_background(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            report = ReviewReport(**task['report']) if task.get('report') else None
            if not report or not report.llm_request_preview:
                return
            requirement_text = task['requirement_text']
            acceptance_criteria = list(task['acceptance_criteria'])
            evidence_pack = report.evidence_pack

        try:
            llm_result = self._execute_multi_round_review(
                preview=report.llm_request_preview.model_copy(deep=True),
                evidence_pack=evidence_pack,
                requirement_items=self._build_requirement_items(requirement_text, acceptance_criteria),
            )
        except Exception as exc:
            llm_result = self._build_llm_error_result(
                provider=LLM_REVIEW_PROVIDER or 'bigmodel',
                model_name=LLM_REVIEW_MODEL_NAME or 'glm-4.7-flash',
                requirement_items=self._build_requirement_items(requirement_text, acceptance_criteria),
                response_text='',
                error_message=f'LLM 后台审阅执行失败：{exc}',
            )

        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            report = ReviewReport(**task['report']) if task.get('report') else None
            if not report:
                return
            updated_report = self._apply_llm_result(report, llm_result)
            task['report'] = updated_report.model_dump()
            task['updated_at'] = self._now()
            task['history'].insert(
                0,
                {
                    'record_id': f'hist-{uuid4().hex[:8]}',
                    'label': 'LLM 审阅完成' if llm_result.status == 'success' else 'LLM 审阅失败',
                    'status': updated_report.status,
                    'overall_score': updated_report.overall_score,
                    'summary': updated_report.summary,
                    'changed_points': [llm_result.summary],
                    'created_at': task['updated_at'],
                },
            )
            self._save_tasks()

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
        graph_evidence = request.graph_evidence or self._auto_expand_graph_evidence(
            repo_name=request.repo_name,
            snapshot=request.snapshot,
            snippets=selected_snippets,
        )
        evidence_paths = self._resolve_evidence_paths(request.graph_evidence)
        tool_findings, structural_gaps = self._build_objective_signals(
            selected_snippets=selected_snippets,
            graph_evidence=graph_evidence,
        )
        review_focuses = self._build_review_focuses(selected_snippets, graph_evidence)
        evidence_pack = self._build_evidence_pack(
            request=request.model_copy(update={'graph_evidence': graph_evidence}),
            tool_findings=tool_findings,
            structural_gaps=structural_gaps,
        )
        llm_request_preview = self._build_llm_request_preview(evidence_pack)
        summary = f'已整理 {len(requirement_items)} 条需求要点的证据，尚未执行大模型审阅。'
        logger.info(
            'consistency_analyze_prepared repo=%s snapshot=%s requirement_items=%d selected_snippets=%d graph_paths=%d tool_findings=%d structural_gaps=%d',
            request.repo_name or '',
            request.snapshot or '',
            len(requirement_items),
            len(selected_snippets),
            len(graph_evidence.paths if graph_evidence and graph_evidence.paths else []),
            len(tool_findings),
            len(structural_gaps),
        )

        return ConsistencyAnalyzeResponse(
            overall_score=0.0,
            overall_confidence=0.0,
            status='needs_review',
            requirement_spec=requirement_spec,
            judgements=[],
            missing_items=[],
            tool_findings=tool_findings,
            evidence_paths=self._resolve_evidence_paths(graph_evidence),
            structural_gaps=structural_gaps,
            review_focuses=review_focuses,
            evidence_pack=evidence_pack,
            llm_request_preview=llm_request_preview,
            llm_result=None,
            summary=summary,
        )

    def _auto_expand_graph_evidence(
        self,
        *,
        repo_name: str,
        snapshot: str,
        snippets: List[CandidateSnippet],
    ) -> GraphEvidenceBundle | None:
        if not repo_name or not snapshot or not snippets:
            return None
        try:
            from app.services.indexing.runtime import indexing_service
            from app.services.workitems.runtime import workitem_service

            job = indexing_service.find_job_by_scope(repo_name, snapshot)
            if not job:
                return None
            return workitem_service.expand_graph_evidence_for_snippets(job, snippets)
        except Exception:
            return None

    def review(self, request: ConsistencyAnalyzeRequest) -> LlmReviewResult:
        report = self.analyze(request)
        preview = report.llm_request_preview
        requirement_items = self._build_requirement_items(request.requirement_text, request.acceptance_criteria)
        if not requirement_items:
            logger.warning(
                'consistency_review_rejected repo=%s snapshot=%s reason=no_requirement_items',
                request.repo_name or '',
                request.snapshot or '',
            )
            return self._build_llm_error_result(
                provider=LLM_REVIEW_PROVIDER or 'bigmodel',
                model_name=LLM_REVIEW_MODEL_NAME or 'glm-4.7-flash',
                requirement_items=[],
                response_text='',
                error_message='No reviewable acceptance criteria were extracted. Add requirement items before running review.',
            )
        if not preview:
            return self._build_llm_error_result(
                provider=LLM_REVIEW_PROVIDER or 'bigmodel',
                model_name=LLM_REVIEW_MODEL_NAME or 'glm-4.7-flash',
                requirement_items=requirement_items,
                response_text='',
                error_message='LLM request preview was not generated. Review cannot be executed.',
            )

        try:
            logger.info(
                'consistency_review_started repo=%s snapshot=%s requirement_items=%d snippets=%d graph_paths=%d',
                request.repo_name or '',
                request.snapshot or '',
                len(requirement_items),
                len(report.evidence_pack.snippets if report.evidence_pack else []),
                len(report.evidence_pack.graph_paths if report.evidence_pack and report.evidence_pack.graph_paths else []),
            )
            return self._execute_multi_round_review(
                preview=preview,
                evidence_pack=report.evidence_pack,
                requirement_items=requirement_items,
            )
        except Exception as exc:
            logger.exception(
                'consistency_review_failed repo=%s snapshot=%s error=%s',
                request.repo_name or '',
                request.snapshot or '',
                exc,
            )
            return self._build_llm_error_result(
                provider=LLM_REVIEW_PROVIDER or 'bigmodel',
                model_name=LLM_REVIEW_MODEL_NAME or 'glm-4.7-flash',
                requirement_items=requirement_items,
                response_text='',
                error_message=f'LLM review execution failed: {exc}',
            )

    def _execute_multi_round_review(
        self,
        *,
        preview: LlmRequestPreview,
        evidence_pack: LlmEvidencePack | None,
        requirement_items: List[str],
    ) -> LlmReviewResult:
        if not requirement_items:
            logger.warning('consistency_multi_round_review_skipped reason=no_requirement_items')
            return self._build_llm_error_result(
                provider=LLM_REVIEW_PROVIDER or 'bigmodel',
                model_name=LLM_REVIEW_MODEL_NAME or 'glm-4.7-flash',
                requirement_items=[],
                response_text='',
                error_message='No reviewable acceptance criteria were extracted. Multi-round review was skipped.',
            )
        logger.info(
            'consistency_multi_round_review_started requirement_items=%d snippets=%d graph_paths=%d',
            len(requirement_items),
            len(evidence_pack.snippets if evidence_pack else []),
            len(evidence_pack.graph_paths if evidence_pack and evidence_pack.graph_paths else []),
        )
        primary_result = self._run_llm_review_round(
            preview=preview,
            requirement_items=requirement_items,
            round_name='primary',
        )
        if primary_result.status != 'success':
            logger.warning(
                'consistency_multi_round_review_primary_failed error=%s',
                primary_result.error_message,
            )
            return primary_result

        challenge_preview = self._build_llm_request_preview(
            evidence_pack or LlmEvidencePack(
                requirement_text="",
                acceptance_criteria=requirement_items,
            ),
            review_mode='challenge',
            prior_result=primary_result,
        )
        challenge_result = self._run_llm_review_round(
            preview=challenge_preview,
            requirement_items=requirement_items,
            round_name='challenge',
        )
        if challenge_result.status != 'success':
            logger.warning(
                'consistency_multi_round_review_challenge_failed error=%s',
                challenge_result.error_message,
            )
            downgraded = primary_result.model_copy(deep=True)
            downgraded.conflicts.append('Challenge round did not complete successfully; final result is based on the primary round only.')
            downgraded.evidence_gaps.append('Challenge round result is unavailable. Key conclusions should be spot-checked manually.')
            downgraded.manual_review_needed = True
            downgraded.confidence = round(min(primary_result.confidence, 0.45), 3)
            downgraded.summary = f"{primary_result.summary} Challenge round did not complete; manual review is recommended."
            return downgraded

        merged = self._merge_llm_results(primary_result, challenge_result, requirement_items)
        logger.info(
            'consistency_multi_round_review_completed overall_verdict=%s confidence=%.3f conflicts=%d evidence_gaps=%d',
            merged.overall_verdict,
            merged.confidence,
            len(merged.conflicts),
            len(merged.evidence_gaps),
        )
        return merged

    def _run_llm_review_round(
        self,
        *,
        preview: LlmRequestPreview,
        requirement_items: List[str],
        round_name: str,
    ) -> LlmReviewResult:
        logger.info(
            'consistency_review_round_started round=%s requirement_items=%d',
            round_name,
            len(requirement_items),
        )
        gateway_response = self._llm_gateway.submit_review(preview)
        result = self._parse_llm_response(
            gateway_response.response_text,
            provider=gateway_response.provider,
            model_name=gateway_response.model_name,
            requirement_items=requirement_items,
            error_message=gateway_response.error_message,
        )
        logger.info(
            'consistency_review_round_finished round=%s status=%s overall_verdict=%s error=%s',
            round_name,
            result.status,
            result.overall_verdict,
            result.error_message or '',
        )
        return result

    def _merge_llm_results(
        self,
        primary: LlmReviewResult,
        challenge: LlmReviewResult,
        requirement_items: List[str],
    ) -> LlmReviewResult:
        primary_map = {item.item: item for item in primary.item_assessments}
        challenge_map = {item.item: item for item in challenge.item_assessments}

        merged_items: List[LlmItemAssessment] = []
        conflicts = list(dict.fromkeys([*primary.conflicts, *challenge.conflicts]))
        missing_items = list(dict.fromkeys([*primary.missing_items, *challenge.missing_items]))
        evidence_gaps = list(dict.fromkeys([*primary.evidence_gaps, *challenge.evidence_gaps]))

        for item in requirement_items:
            base = primary_map.get(item) or self._build_error_assessment(item, '初审缺少该验收项结果。')
            challenger = challenge_map.get(item) or self._build_error_assessment(item, '复核缺少该验收项结果。')
            if base.verdict != challenger.verdict:
                conflicts.append(f'验收项《{item}》初审为 {base.verdict}，复核为 {challenger.verdict}。')

            merged_dimension_assessments = self._merge_dimension_sets(
                base.dimension_assessments,
                challenger.dimension_assessments,
            )

            base_rank = self._verdict_rank(base.verdict)
            challenger_rank = self._verdict_rank(challenger.verdict)
            chosen = challenger if challenger_rank > base_rank else base
            agreement_ratio = self._dimension_agreement_ratio(
                base.dimension_assessments,
                challenger.dimension_assessments,
            )
            merged_confidence = self._merge_item_confidence(base, challenger, agreement_ratio)
            manual_review_needed = (
                base.manual_review_needed
                or challenger.manual_review_needed
                or base.verdict != challenger.verdict
                or merged_confidence < 0.6
            )
            merged_missing_evidence = list(dict.fromkeys([*base.missing_evidence, *challenger.missing_evidence]))

            merged_items.append(
                LlmItemAssessment(
                    item=item,
                    verdict=chosen.verdict,
                    score=min(base.score, challenger.score),
                    confidence=merged_confidence,
                    reasoning=self._combine_reasoning(base.reasoning, challenger.reasoning),
                    supporting_snippet_ids=list(dict.fromkeys([*base.supporting_snippet_ids, *challenger.supporting_snippet_ids])),
                    supporting_path_ids=list(dict.fromkeys([*base.supporting_path_ids, *challenger.supporting_path_ids])),
                    missing_evidence=merged_missing_evidence,
                    dimension_assessments=merged_dimension_assessments,
                    manual_review_needed=manual_review_needed,
                )
            )
            evidence_gaps.extend(merged_missing_evidence)

        evidence_gaps = list(dict.fromkeys(evidence_gaps))
        overall_score_raw = round(
            sum(item.score for item in merged_items) / len(merged_items),
            3,
        ) if merged_items else 0.0
        overall_confidence = round(
            sum(item.confidence for item in merged_items) / len(merged_items),
            3,
        ) if merged_items else 0.0
        overall_verdict = self._derive_overall_verdict(merged_items)
        manual_review_needed = (
            primary.manual_review_needed
            or challenge.manual_review_needed
            or bool(conflicts)
            or overall_confidence < 0.65
        )
        summary = self._build_merged_summary(merged_items, conflicts, evidence_gaps)

        return LlmReviewResult(
            status='success',
            provider=primary.provider,
            model_name=primary.model_name,
            summary=summary,
            overall_verdict=overall_verdict,
            overall_score_raw=overall_score_raw,
            confidence=overall_confidence,
            manual_review_needed=manual_review_needed,
            item_assessments=merged_items,
            missing_items=missing_items,
            conflicts=conflicts,
            evidence_gaps=evidence_gaps,
            response_text=json.dumps(
                {
                    'primary': primary.response_body,
                    'challenge': challenge.response_body,
                },
                ensure_ascii=False,
            ),
            response_body={
                'primary': primary.response_body,
                'challenge': challenge.response_body,
                'merged': {
                    'summary': summary,
                    'overall_verdict': overall_verdict,
                    'overall_score_raw': overall_score_raw,
                    'confidence': overall_confidence,
                },
            },
            error_message='',
        )

    def _build_merged_summary(
        self,
        items: List[LlmItemAssessment],
        conflicts: List[str],
        evidence_gaps: List[str],
    ) -> str:
        if not items:
            parts = ['Two-round review did not produce any valid item-level conclusions.']
            if conflicts:
                parts.append(f'Found {len(conflicts)} disagreements between primary and challenge rounds.')
            if evidence_gaps:
                parts.append(f'Identified {len(evidence_gaps)} evidence gaps.')
            return ' '.join(parts)
        satisfied_count = sum(1 for item in items if item.verdict == 'satisfied')
        partial_count = sum(1 for item in items if item.verdict == 'partially_satisfied')
        not_satisfied_count = sum(1 for item in items if item.verdict == 'not_satisfied')
        error_count = sum(1 for item in items if item.verdict == 'error')
        if error_count == len(items):
            parts = ['Two-round review did not produce a valid verdict; the result is treated as error.']
            if evidence_gaps:
                parts.append(f'Identified {len(evidence_gaps)} evidence gaps.')
            if conflicts:
                parts.append(f'Found {len(conflicts)} disagreements between primary and challenge rounds.')
            return ' '.join(parts)
        parts = [
            f'Two-round review completed: satisfied {satisfied_count}, partially satisfied {partial_count}, not satisfied {not_satisfied_count}.'
        ]
        if conflicts:
            parts.append(f'Found {len(conflicts)} disagreements between primary and challenge rounds; the stricter conclusion was kept.')
        if evidence_gaps:
            parts.append(f'Identified {len(evidence_gaps)} evidence gaps.')
        return ' '.join(parts)

    def _combine_reasoning(self, primary_reasoning: str, challenge_reasoning: str) -> str:
        normalized_primary = (primary_reasoning or '').strip()
        normalized_challenge = (challenge_reasoning or '').strip()
        if normalized_primary and normalized_primary == normalized_challenge:
            return normalized_primary
        parts = []
        if normalized_primary:
            parts.append(f'初审：{normalized_primary}')
        if normalized_challenge:
            parts.append(f'复核：{normalized_challenge}')
        return ' '.join(parts) if parts else '模型未返回详细理由。'

    def _merge_item_confidence(
        self,
        primary: LlmItemAssessment,
        challenge: LlmItemAssessment,
        agreement_ratio: float,
    ) -> float:
        base = round((primary.confidence + challenge.confidence) / 2, 3)
        if primary.verdict == challenge.verdict:
            return round(min(1.0, base * 0.7 + agreement_ratio * 0.3), 3)
        return round(min(base, 0.45) * 0.7 + agreement_ratio * 0.3, 3)

    def _dimension_agreement_ratio(
        self,
        primary: LlmDimensionAssessmentSet,
        challenge: LlmDimensionAssessmentSet,
    ) -> float:
        primary_dump = primary.model_dump()
        challenge_dump = challenge.model_dump()
        matched = 0
        total = 0
        for key, value in primary_dump.items():
            total += 1
            if value.get('verdict') == challenge_dump.get(key, {}).get('verdict'):
                matched += 1
        return round(matched / total, 3) if total else 0.0

    def _merge_dimension_sets(
        self,
        primary: LlmDimensionAssessmentSet,
        challenge: LlmDimensionAssessmentSet,
    ) -> LlmDimensionAssessmentSet:
        merged: dict = {}
        for key in primary.model_dump().keys():
            merged[key] = self._merge_dimension_assessment(
                getattr(primary, key),
                getattr(challenge, key),
            )
        return LlmDimensionAssessmentSet(**merged)

    def _merge_dimension_assessment(
        self,
        primary: LlmDimensionAssessment,
        challenge: LlmDimensionAssessment,
    ) -> LlmDimensionAssessment:
        chosen = challenge if self._dimension_verdict_rank(challenge.verdict) > self._dimension_verdict_rank(primary.verdict) else primary
        score = min(primary.score, challenge.score)
        reasoning = self._combine_reasoning(primary.reasoning, challenge.reasoning)
        return LlmDimensionAssessment(
            verdict=chosen.verdict,
            score=score,
            reasoning=reasoning,
        )

    def _build_error_assessment(self, item: str, message: str) -> LlmItemAssessment:
        return LlmItemAssessment(
            item=item,
            verdict='error',
            score=0.0,
            confidence=0.0,
            reasoning=message,
            supporting_snippet_ids=[],
            supporting_path_ids=[],
            missing_evidence=[message],
            dimension_assessments=self._build_uniform_dimension_set('error', 0.0, message),
            manual_review_needed=True,
        )

    def _build_uniform_dimension_set(
        self,
        verdict: str,
        score: float,
        reasoning: str,
    ) -> LlmDimensionAssessmentSet:
        payload = {
            key: {
                'verdict': verdict,
                'score': score,
                'reasoning': reasoning,
            }
            for key in (
                'functional_implementation',
                'scenario_fit',
                'constraint_compliance',
                'exception_handling',
                'non_functional_compliance',
                'evidence_sufficiency',
            )
        }
        return LlmDimensionAssessmentSet(**payload)

    def _derive_overall_verdict(self, items: List[LlmItemAssessment]) -> str:
        verdicts = {item.verdict for item in items}
        if 'not_satisfied' in verdicts:
            return 'not_satisfied'
        if 'partially_satisfied' in verdicts:
            return 'partially_satisfied'
        if verdicts == {'satisfied'}:
            return 'satisfied'
        return 'error'

    def _verdict_rank(self, verdict: str) -> int:
        rank_map = {
            'satisfied': 0,
            'partially_satisfied': 1,
            'not_satisfied': 2,
            'error': 3,
        }
        return rank_map.get(verdict, 3)

    def _dimension_verdict_rank(self, verdict: str) -> int:
        rank_map = {
            'satisfied': 0,
            'not_applicable': 0,
            'partially_satisfied': 1,
            'insufficient_evidence': 2,
            'not_satisfied': 3,
            'error': 4,
        }
        return rank_map.get(verdict, 4)

    def _build_requirement_spec(self, requirement_text: str, criteria: List[str]) -> RequirementSpec:
        raw_items = [requirement_text, *criteria]
        intents = [item for item in raw_items if any(token in item for token in ('支持', '实现', '允许', '提供', '提交', '上传'))]
        constraints = [item for item in raw_items if re.search(r'(不超过|至少|最大|必须|应当|<=|>=|=)', item)]
        exceptions = [item for item in raw_items if any(token in item for token in ('异常', '失败', '错误', '重试', '阻断', '回滚'))]
        return RequirementSpec(intents=intents, constraints=constraints, exceptions=exceptions)

    def _build_requirement_items(self, requirement_text: str, criteria: List[str]) -> List[str]:
        structured = [item.strip() for item in criteria if item.strip()]
        if structured:
            logger.debug('consistency_requirement_items_from_criteria count=%d', len(structured))
            return structured

        base_items = [
            line.strip()
            for line in re.split('[\n\u3002\uff1b;]', requirement_text)
            if line.strip()
        ]
        merged: List[str] = []
        seen = set()
        for item in base_items:
            if item not in seen:
                seen.add(item)
                merged.append(item)
        logger.debug('consistency_requirement_items_from_text count=%d', len(merged))
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
            graph_paths = self._sort_graph_paths_for_llm(graph_paths)

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


    def _sort_graph_paths_for_llm(self, paths: List[LlmEvidencePath]) -> List[LlmEvidencePath]:
        def score(path: LlmEvidencePath) -> tuple[int, int, int]:
            direct_call_count = sum(1 for node in path.nodes if node.relation_from_prev == 'CALLS')
            reverse_call_count = sum(1 for node in path.nodes if node.relation_from_prev == 'CALLED_BY')
            call_count = sum(
                1 for node in path.nodes if node.relation_from_prev in {'CALLS', 'CALLED_BY'}
            )
            function_count = sum(1 for node in path.nodes if node.node_type == 'function')
            file_count = sum(1 for node in path.nodes if node.node_type == 'file')
            anonymous_penalty = sum(1 for node in path.nodes if node.label.startswith('%')) * 8
            constructor_penalty = sum(1 for node in path.nodes if node.label.endswith('.constructor') or node.label == 'constructor') * 6
            reverse_penalty = reverse_call_count * 3
            terminal = path.nodes[-1] if path.nodes else None
            terminal_penalty = 0
            if terminal and terminal.node_type == 'file':
                terminal_penalty -= 8
            if terminal and terminal.label.startswith('%'):
                terminal_penalty -= 3
            weighted = (
                direct_call_count * 24
                + reverse_call_count * 16
                + function_count * 6
                - file_count * 2
                - anonymous_penalty
                - constructor_penalty
                - reverse_penalty
                - len(path.nodes) * 2
                + terminal_penalty
            )
            return (weighted, direct_call_count, call_count)

        return sorted(paths, key=score, reverse=True)

    def _build_llm_request_preview(
        self,
        evidence_pack: LlmEvidencePack,
        *,
        review_mode: str = 'primary',
        prior_result: LlmReviewResult | None = None,
    ) -> LlmRequestPreview:
        if review_mode == 'challenge':
            system_message = (
                '你是需求实现审阅复核助手。请仅基于给定原始证据和提供的初审结果进行复核。'
                '你的目标不是复述初审，而是主动寻找证据不足、过度推断、忽略约束、忽略异常分支和结论过宽的问题。'
                '仍然必须返回完整 JSON 对象，字段必须齐全，不能省略任何字段。'
            )
        else:
            system_message = (
                '你是需求实现审阅助手。请仅基于给定的原始证据进行判断，不要参考任何先验结论，'
                '不要臆造未提供的实现。代码片段证据通常是实现细节的主证据，'
                '图路径证据通常用于补充调用关系、上下文和影响范围；'
                '如果某些图路径与需求明显无关或只是通用日志/框架细节，你可以忽略它们。'
                '你需要按固定维度审阅每条验收标准，并返回完整、字段齐全的 JSON 对象。'
            )
        user_message = self._build_llm_user_message(
            evidence_pack,
            review_mode=review_mode,
            prior_result=prior_result,
        )
        request_body = {
            'messages': [
                {
                    'role': 'system',
                    'content': system_message,
                },
                {
                    'role': 'user',
                    'content': user_message,
                },
            ],
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
                    '代码片段优先用于判断具体实现是否存在；图路径优先用于判断调用关系和上下文。',
                    '如果图路径只体现通用日志、框架回调或与验收标准无直接关系的内部细节，可以降低其权重或忽略。',
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
                    'overall_score_raw',
                    'confidence',
                    'manual_review_needed',
                    'item_assessments',
                    'missing_items',
                    'conflicts',
                    'evidence_gaps',
                ],
                'item_assessment_fields': [
                    'item',
                    'verdict',
                    'score',
                    'confidence',
                    'reasoning',
                    'supporting_snippet_ids',
                    'supporting_path_ids',
                    'missing_evidence',
                    'dimension_assessments',
                    'manual_review_needed',
                ],
                'dimension_assessment_fields': {
                    'functional_implementation': ['verdict', 'score', 'reasoning'],
                    'scenario_fit': ['verdict', 'score', 'reasoning'],
                    'constraint_compliance': ['verdict', 'score', 'reasoning'],
                    'exception_handling': ['verdict', 'score', 'reasoning'],
                    'non_functional_compliance': ['verdict', 'score', 'reasoning'],
                    'evidence_sufficiency': ['verdict', 'score', 'reasoning'],
                },
            },
        }
        return LlmRequestPreview(
            provider=LLM_REVIEW_PROVIDER or 'bigmodel',
            model_name=LLM_REVIEW_MODEL_NAME or 'glm-4.7-flash',
            summary=(
                f'当前尚未实际调用大模型（模式：{review_mode}）。请求体已整理为需求上下文与证据池，'
                f'包含 {len(evidence_pack.snippets)} 个代码片段和 {len(evidence_pack.graph_paths)} 条图路径。'
            ),
            system_message=system_message,
            user_message=user_message,
            request_body=request_body,
        )

    def _build_llm_user_message(
        self,
        evidence_pack: LlmEvidencePack,
        *,
        review_mode: str = 'primary',
        prior_result: LlmReviewResult | None = None,
    ) -> str:
        lines: List[str] = []
        if review_mode == 'challenge':
            lines.append('请基于下面的需求、原始证据和初审结果，执行复核审阅。')
            lines.append('复核时请重点检查：证据不足、忽略约束、忽略异常分支、过度自信、以及初审结论是否过宽。')
        else:
            lines.append('请基于下面的需求与原始证据，逐条审阅每条验收标准是否已被当前实现满足。')
            lines.append('判断时请优先依据代码片段确认具体实现，再结合图路径理解调用关系和上下文。')
            lines.append('如果某条图路径只是日志、框架回调或其他与需求无直接关系的内部细节，可以忽略。')
        lines.append('')
        lines.append('一、需求描述')
        lines.append(evidence_pack.requirement_text or '未提供。')
        lines.append('')
        lines.append('二、验收标准')
        if evidence_pack.acceptance_criteria:
            for index, item in enumerate(evidence_pack.acceptance_criteria, start=1):
                lines.append(f'{index}. {item}')
        else:
            lines.append('未提供验收标准。')
        lines.append('')
        lines.append('三、代码片段证据')
        if evidence_pack.snippets:
            for snippet in evidence_pack.snippets:
                lines.append(
                    f'- 片段 {snippet.snippet_id} | {snippet.filename}:{snippet.start_line}-{snippet.end_line} | {snippet.reason}'
                )
                lines.append('```ts')
                lines.append(snippet.code.strip())
                lines.append('```')
        else:
            lines.append('未提供代码片段证据。')
        lines.append('')
        lines.append('四、图路径证据')
        if evidence_pack.graph_paths:
            selected_paths = self._select_graph_paths_for_prompt(evidence_pack.graph_paths)
            seen_excerpt_nodes: set[str] = set()
            for path in selected_paths:
                lines.append(f'- 路径 {path.path_id}')
                lines.append(f'  摘要：{self._render_graph_path_summary(path)}')
                has_non_seed_excerpt = any(self._trim_excerpt(node.code_excerpt) for node in path.nodes[1:])
                for node in path.nodes[:4]:
                    node_header = f'  节点：{node.label} [{node.node_type}]'
                    if node.path:
                        node_header += f' @ {node.path}'
                    if node.relation_from_prev:
                        node_header += f' <{node.relation_from_prev}>'
                    lines.append(node_header)
                    excerpt = self._trim_excerpt(node.code_excerpt)
                    if has_non_seed_excerpt and node is path.nodes[0]:
                        continue
                    if excerpt and node.node_id not in seen_excerpt_nodes:
                        seen_excerpt_nodes.add(node.node_id)
                        lines.append('  代码摘录：')
                        lines.append('  ```ts')
                        lines.extend([f'  {line}' for line in excerpt.splitlines()])
                        lines.append('  ```')
        else:
            lines.append('未提供图路径证据。')
        lines.append('')
        lines.append('五、客观信号')
        if evidence_pack.structural_gaps:
            lines.append('结构信号：')
            for signal in evidence_pack.structural_gaps:
                lines.append(f'- {signal}')
        if evidence_pack.tool_findings:
            lines.append('工具信号：')
            for finding in evidence_pack.tool_findings:
                lines.append(f'- [{finding.level}] {finding.message}')
        if not evidence_pack.structural_gaps and not evidence_pack.tool_findings:
            lines.append('无额外客观信号。')
        lines.append('')
        if review_mode == 'challenge' and prior_result:
            lines.append('六、初审结果（待复核）')
            lines.append(json.dumps(prior_result.model_dump(), ensure_ascii=False, indent=2))
            lines.append('')
            lines.append('七、输出要求')
        else:
            lines.append('六、输出要求')
        lines.append('请返回 JSON 对象，字段必须满足 output_contract。')
        lines.append('顶层字段必须完整包含：summary, overall_verdict, overall_score_raw, confidence, manual_review_needed, item_assessments, missing_items, conflicts, evidence_gaps。')
        lines.append('item_assessments 中每一项必须完整包含：item, verdict, score, confidence, reasoning, supporting_snippet_ids, supporting_path_ids, missing_evidence, dimension_assessments, manual_review_needed。')
        lines.append('overall_verdict 和 verdict 只允许使用：satisfied, partially_satisfied, not_satisfied。')
        lines.append('dimension_assessments 必须完整包含：functional_implementation, scenario_fit, constraint_compliance, exception_handling, non_functional_compliance, evidence_sufficiency。')
        lines.append('每个维度对象必须完整包含：verdict, score, reasoning。')
        lines.append('维度 verdict 允许使用：satisfied, partially_satisfied, not_satisfied, not_applicable, insufficient_evidence。')
        lines.append('所有字段都必须出现；没有内容时返回空数组、空字符串或 0，不允许省略字段。')
        lines.append('不要使用 Markdown 代码块，不要额外包裹 answer、data、result 等外层字段，直接返回目标 JSON 对象本身。')
        lines.append('字段名必须严格一致，不要改写成 item_id、status、items、analysis 等其他名字。')
        lines.append('返回格式示例：{"summary":"...","overall_verdict":"partially_satisfied","overall_score_raw":0.5,"confidence":0.78,"manual_review_needed":true,"item_assessments":[{"item":"验收标准原文","verdict":"satisfied","score":1.0,"confidence":0.84,"reasoning":"...","supporting_snippet_ids":["snippet-1"],"supporting_path_ids":["path-1"],"missing_evidence":[],"dimension_assessments":{"functional_implementation":{"verdict":"satisfied","score":1.0,"reasoning":"..."},"scenario_fit":{"verdict":"satisfied","score":1.0,"reasoning":"..."},"constraint_compliance":{"verdict":"partially_satisfied","score":0.5,"reasoning":"..."},"exception_handling":{"verdict":"insufficient_evidence","score":0.0,"reasoning":"..."},"non_functional_compliance":{"verdict":"not_applicable","score":1.0,"reasoning":"..."},"evidence_sufficiency":{"verdict":"partially_satisfied","score":0.5,"reasoning":"..."}},"manual_review_needed":false}],"missing_items":[],"conflicts":[],"evidence_gaps":[]}')
        lines.append('你需要自己判断每条验收标准与哪些证据相关，不要假设系统已经完成证据归因。')
        lines.append('如果证据不足，请明确说明证据不足，而不是猜测实现存在。')
        return '\n'.join(lines)

    def _render_graph_path_summary(self, path: LlmEvidencePath) -> str:
        segments = []
        for node in path.nodes[:6]:
            label = node.label
            if node.relation_from_prev:
                segments.append(f'{node.relation_from_prev} -> {label}')
            else:
                segments.append(label)
        return ' | '.join(segments)


    def _select_graph_paths_for_prompt(self, paths: List[LlmEvidencePath]) -> List[LlmEvidencePath]:
        clean_call_paths = [
            path
            for path in paths
            if any(node.relation_from_prev in {'CALLS', 'CALLED_BY'} for node in path.nodes)
            and self._is_prompt_path_clean(path)
        ]
        if clean_call_paths:
            return self._dedupe_prompt_paths(clean_call_paths)[:6]

        call_paths = [
            path
            for path in paths
            if any(node.relation_from_prev in {'CALLS', 'CALLED_BY'} for node in path.nodes)
        ]
        if call_paths:
            return self._dedupe_prompt_paths(call_paths)[:6]

        clean_paths = [path for path in paths if self._is_prompt_path_clean(path)]
        if clean_paths:
            return self._dedupe_prompt_paths(clean_paths)[:4]
        return self._dedupe_prompt_paths(paths)[:4]

    def _is_prompt_path_clean(self, path: LlmEvidencePath) -> bool:
        tail = path.nodes[-1] if path.nodes else None
        if not tail:
            return False
        if self._is_internal_graph_label(tail.label):
            return False
        internal_middle = [
            node for node in path.nodes[1:]
            if self._is_internal_graph_label(node.label)
        ]
        if internal_middle and len(path.nodes) <= 3:
            return False
        return True

    def _dedupe_prompt_paths(self, paths: List[LlmEvidencePath]) -> List[LlmEvidencePath]:
        selected: List[LlmEvidencePath] = []
        seen_terminals: set[tuple[str, str]] = set()
        for path in paths:
            terminal = path.nodes[-1] if path.nodes else None
            if not terminal:
                continue
            key = (terminal.label, terminal.relation_from_prev or '')
            if key in seen_terminals:
                continue
            seen_terminals.add(key)
            selected.append(path)
        return selected

    def _is_internal_graph_label(self, label: str) -> bool:
        return (
            label.startswith('%')
            or '.%' in label
            or '%AC$' in label
            or label.endswith('.constructor')
            or label == 'constructor'
            or label.endswith('.%instInit')
            or label.endswith('.%statInit')
        )

    def _trim_excerpt(self, code_excerpt: str) -> str:
        if not code_excerpt:
            return ''
        lines = [line.rstrip() for line in code_excerpt.splitlines()]
        cleaned = [line for line in lines if line.strip()]
        return '\n'.join(cleaned[:12])

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
            logger.warning(
                'consistency_llm_gateway_error provider=%s model=%s error=%s',
                provider,
                model_name,
                error_message,
            )
            return self._build_llm_error_result(
                provider=provider,
                model_name=model_name,
                requirement_items=requirement_items,
                response_text=response_text,
                error_message=error_message,
            )

        try:
            payload = self._load_llm_json_payload(response_text)
        except Exception as exc:
            logger.warning(
                'consistency_llm_json_invalid provider=%s model=%s error=%s',
                provider,
                model_name,
                exc,
            )
            return self._build_llm_error_result(
                provider=provider,
                model_name=model_name,
                requirement_items=requirement_items,
                response_text=response_text,
                error_message=f'LLM response is not valid JSON: {exc}',
            )

        try:
            summary = str(payload['summary']).strip()
            overall_verdict = str(payload['overall_verdict']).strip()
            overall_score_raw = float(payload['overall_score_raw'])
            confidence = float(payload['confidence'])
            if not isinstance(payload['manual_review_needed'], bool):
                raise ValueError('manual_review_needed must be a boolean')
            manual_review_needed = payload['manual_review_needed']
            raw_assessments = payload['item_assessments']
            if not isinstance(payload['missing_items'], list):
                raise ValueError('missing_items must be an array')
            if not isinstance(payload['conflicts'], list):
                raise ValueError('conflicts must be an array')
            if not isinstance(payload['evidence_gaps'], list):
                raise ValueError('evidence_gaps must be an array')
            missing_items = [str(item) for item in payload['missing_items']]
            conflicts = [str(item) for item in payload['conflicts']]
            evidence_gaps = [str(item) for item in payload['evidence_gaps']]
            if overall_verdict not in {'satisfied', 'partially_satisfied', 'not_satisfied'}:
                raise ValueError('overall_verdict is outside the allowed range')
            if not isinstance(raw_assessments, list):
                raise ValueError('item_assessments must be an array')
            if not 0 <= overall_score_raw <= 1:
                raise ValueError('overall_score_raw is outside the allowed range')
            if not 0 <= confidence <= 1:
                raise ValueError('confidence is outside the allowed range')

            item_assessments: List[LlmItemAssessment] = []
            for raw_item in raw_assessments:
                if not isinstance(raw_item, dict):
                    raise ValueError('each item_assessment must be an object')
                item_verdict = str(raw_item['verdict'])
                if item_verdict not in {'satisfied', 'partially_satisfied', 'not_satisfied'}:
                    raise ValueError(f'item verdict is outside the allowed range: {item_verdict}')
                item_score = float(raw_item['score'])
                item_confidence = float(raw_item['confidence'])
                if not 0 <= item_score <= 1:
                    raise ValueError(f'item score is outside the allowed range: {item_score}')
                if not 0 <= item_confidence <= 1:
                    raise ValueError(f'item confidence is outside the allowed range: {item_confidence}')
                raw_dimensions = raw_item['dimension_assessments']
                if not isinstance(raw_dimensions, dict):
                    raise ValueError('dimension_assessments must be an object')
                parsed_dimensions: dict = {}
                for dimension_key in (
                    'functional_implementation',
                    'scenario_fit',
                    'constraint_compliance',
                    'exception_handling',
                    'non_functional_compliance',
                    'evidence_sufficiency',
                ):
                    dimension_value = raw_dimensions[dimension_key]
                    if not isinstance(dimension_value, dict):
                        raise ValueError(f'{dimension_key} must be an object')
                    dimension_verdict = str(dimension_value['verdict'])
                    if dimension_verdict not in {
                        'satisfied',
                        'partially_satisfied',
                        'not_satisfied',
                        'not_applicable',
                        'insufficient_evidence',
                    }:
                        raise ValueError(f'{dimension_key}.verdict is outside the allowed range: {dimension_verdict}')
                    dimension_score = float(dimension_value['score'])
                    if not 0 <= dimension_score <= 1:
                        raise ValueError(f'{dimension_key}.score is outside the allowed range: {dimension_score}')
                    parsed_dimensions[dimension_key] = LlmDimensionAssessment(
                        verdict=dimension_verdict,
                        score=dimension_score,
                        reasoning=str(dimension_value['reasoning']),
                    )
                if not isinstance(raw_item['manual_review_needed'], bool):
                    raise ValueError('item.manual_review_needed must be a boolean')

                item_assessments.append(
                    LlmItemAssessment(
                        item=str(raw_item['item']),
                        verdict=item_verdict,
                        score=item_score,
                        confidence=item_confidence,
                        reasoning=str(raw_item['reasoning']),
                        supporting_snippet_ids=[str(value) for value in raw_item['supporting_snippet_ids']],
                        supporting_path_ids=[str(value) for value in raw_item['supporting_path_ids']],
                        missing_evidence=[str(value) for value in raw_item['missing_evidence']],
                        dimension_assessments=LlmDimensionAssessmentSet(**parsed_dimensions),
                        manual_review_needed=raw_item['manual_review_needed'],
                    )
                )

            return LlmReviewResult(
                status='success',
                provider=provider,
                model_name=model_name,
                summary=summary or 'LLM review completed.',
                overall_verdict=overall_verdict,
                manual_review_needed=manual_review_needed,
                item_assessments=item_assessments,
                missing_items=missing_items,
                conflicts=conflicts,
                evidence_gaps=evidence_gaps,
                overall_score_raw=overall_score_raw,
                confidence=confidence,
                response_text=response_text,
                response_body=payload if isinstance(payload, dict) else {},
                error_message='',
            )
        except Exception as exc:
            logger.warning(
                'consistency_llm_payload_invalid provider=%s model=%s error=%s',
                provider,
                model_name,
                exc,
            )
            return self._build_llm_error_result(
                provider=provider,
                model_name=model_name,
                requirement_items=requirement_items,
                response_text=response_text,
                error_message=f'LLM response schema validation failed: {exc}',
            )

    def _load_llm_json_payload(self, response_text: str) -> dict:
        normalized = response_text.strip()
        if normalized.startswith('```'):
            fenced_match = re.search(r'```(?:json)?\s*(.*?)```', normalized, flags=re.S)
            if fenced_match:
                normalized = fenced_match.group(1).strip()

        payload = json.loads(normalized)
        if not isinstance(payload, dict):
            raise ValueError('LLM 返回结果顶层必须为 JSON 对象')

        if self._looks_like_review_payload(payload):
            return payload

        for key in ('answer', 'data', 'result', 'output'):
            nested = payload.get(key)
            if isinstance(nested, str):
                nested_text = nested.strip()
                if nested_text.startswith('{'):
                    nested_payload = json.loads(nested_text)
                    if isinstance(nested_payload, dict) and self._looks_like_review_payload(nested_payload):
                        return nested_payload
            if isinstance(nested, dict) and self._looks_like_review_payload(nested):
                return nested

        return payload

    def _looks_like_review_payload(self, payload: dict) -> bool:
        required_keys = {
            'summary',
            'overall_verdict',
            'overall_score_raw',
            'confidence',
            'manual_review_needed',
            'item_assessments',
            'missing_items',
            'conflicts',
            'evidence_gaps',
        }
        return required_keys.issubset(payload.keys())

    def _build_llm_error_result(
        self,
        *,
        provider: str,
        model_name: str,
        requirement_items: List[str],
        response_text: str,
        error_message: str,
    ) -> LlmReviewResult:
        logger.error(
            'consistency_llm_error_result provider=%s model=%s requirement_items=%d error=%s',
            provider,
            model_name,
            len(requirement_items),
            error_message,
        )
        return LlmReviewResult(
            status='error',
            provider=provider,
            model_name=model_name,
            summary='LLM review did not complete; the result has been treated as error.',
            overall_verdict='error',
            overall_score_raw=0.0,
            confidence=0.0,
            manual_review_needed=True,
            item_assessments=[
                LlmItemAssessment(
                    item=item,
                    verdict='error',
                    score=0.0,
                    confidence=0.0,
                    reasoning=error_message,
                    supporting_snippet_ids=[],
                    supporting_path_ids=[],
                    missing_evidence=[error_message],
                    dimension_assessments=self._build_uniform_dimension_set('error', 0.0, error_message),
                    manual_review_needed=True,
                )
                for item in requirement_items
            ],
            missing_items=[],
            conflicts=[],
            evidence_gaps=[error_message] if error_message else [],
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
            fallback_score, _ = judgement_map[assessment.verdict]
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
                    'score': assessment.score if llm_result.status == 'success' else fallback_score,
                    'confidence': assessment.confidence if llm_result.status == 'success' else 0.0,
                    'evidence': evidence,
                    'notes': self._format_item_notes(assessment),
                }
            )

        success_scores = [item['score'] for item in judgements if item['status'] != 'error']
        fallback_score = round(sum(success_scores) / len(success_scores), 3) if success_scores else 0.0
        overall_score = llm_result.overall_score_raw if llm_result.status == 'success' else fallback_score
        overall_confidence = llm_result.confidence if llm_result.status == 'success' else 0.0
        status = 'completed' if llm_result.status == 'success' else 'needs_review'
        summary = llm_result.summary

        return report.model_copy(
            update={
                'overall_score': overall_score,
                'overall_confidence': overall_confidence,
                'status': status,
                'judgements': judgements,
                'missing_items': llm_result.missing_items,
                'conflicts': llm_result.conflicts,
                'structural_gaps': list(dict.fromkeys([*report.structural_gaps, *llm_result.evidence_gaps])),
                'llm_result': llm_result,
                'summary': summary,
            }
        )

    def _format_item_notes(self, assessment: LlmItemAssessment) -> str:
        notes = [assessment.reasoning.strip()] if assessment.reasoning.strip() else []
        if assessment.missing_evidence:
            notes.append(f"缺失证据：{'；'.join(assessment.missing_evidence)}")
        dimension_bits = []
        dimension_labels = {
            'functional_implementation': '功能实现',
            'scenario_fit': '场景适配',
            'constraint_compliance': '约束符合',
            'exception_handling': '异常处理',
            'non_functional_compliance': '非功能要求',
            'evidence_sufficiency': '证据充分性',
        }
        for key, label in dimension_labels.items():
            dimension = getattr(assessment.dimension_assessments, key)
            dimension_bits.append(f'{label}:{dimension.verdict}/{dimension.score:.2f}')
        notes.append('维度：' + '，'.join(dimension_bits))
        return '\n'.join(bit for bit in notes if bit)

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
