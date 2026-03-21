from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

from .estimators import EstimateResult, MarketEstimator
from .llm_orchestrator import LLMExtraction, LLMOrchestrator, LLMPlan, apply_llm_extraction
from .models import (
    AttemptRecord,
    EvidenceRecord,
    InferenceConfig,
    InferenceInput,
    SearchAction,
    TaskResult,
    TaskStatus,
)
from .providers import BaseMarketProvider, ProviderError, ProviderHit, order_providers
from .scoring import (
    attempt_action_for_score,
    clamp,
    evidence_count_bonus,
    score_evidence,
    score_evidence_chain,
)


@dataclass
class CandidatePath:
    path: List[str]
    parent_market_size: Optional[float]
    depth: int


@dataclass
class BestSnapshot:
    path: List[str]
    market_size: float
    market_share: float
    evidence_score: float


class MarketInferenceEngine:
    """规则驱动引擎：边细分、边搜索、边回退。"""

    def __init__(
        self,
        config: InferenceConfig,
        providers: Optional[Sequence[BaseMarketProvider]] = None,
        llm_orchestrator: Optional[object] = None,
    ) -> None:
        self.config = config
        self.providers: List[BaseMarketProvider] = list(providers) if providers is not None else order_providers(
            config.providers,
            config.provider_priority,
        )
        self.estimator = MarketEstimator(config)
        self.llm_orchestrator = llm_orchestrator or LLMOrchestrator.from_config(config)

    def run(self, task_id: str, input_model: InferenceInput) -> TaskResult:
        started_at = datetime.utcnow()
        latest_year = input_model.latest_sales_year
        threshold = self.config.target_share_threshold

        evidence_chain: List[EvidenceRecord] = []
        attempt_log: List[AttemptRecord] = []

        candidates = self._initial_candidates(input_model)
        best_snapshot: Optional[BestSnapshot] = None

        for round_index in range(1, self.config.max_search_rounds + 1):
            if not candidates:
                candidates = self._initial_candidates(input_model)

            current = self._pick_candidate(candidates)
            plan = self._plan_with_llm(input_model, current.path, latest_year, round_index, evidence_chain)
            active_path = plan.market_path if plan and plan.market_path else current.path
            active_candidate = CandidatePath(path=active_path, parent_market_size=current.parent_market_size, depth=current.depth)
            query = plan.query if plan and plan.query else self._build_query(input_model, active_candidate.path, latest_year)
            result_path = active_candidate.path

            round_evidence, round_attempts = self._collect_evidence(input_model, active_candidate, query, round_index)
            attempt_log.extend(round_attempts)

            estimate = self.estimator.estimate(
                input_model=input_model,
                evidence_chain=round_evidence or evidence_chain,
                round_index=round_index,
                current_path=active_candidate.path,
                parent_market_size=active_candidate.parent_market_size,
            )

            evidence_score = self._score_round(input_model, round_evidence, round_index)
            combined_score = clamp((evidence_score + estimate.confidence) / 2.0)

            summary_action = self._summary_action(round_evidence, combined_score)
            attempt_log.append(
                AttemptRecord(
                    round_index=round_index,
                    provider=self._provider_of(round_evidence),
                    path=result_path,
                    query=query,
                    market_size_latest_year=estimate.market_size_latest_year,
                    market_share_latest_year=estimate.market_share_latest_year,
                    evidence_score=combined_score,
                    action=summary_action,
                    reason=self._summary_reason(round_evidence, estimate, plan),
                    method=estimate.method,
                )
            )

            evidence_chain.extend(round_evidence)
            best_snapshot = self._pick_better(
                best_snapshot,
                BestSnapshot(
                    path=result_path,
                    market_size=estimate.market_size_latest_year,
                    market_share=estimate.market_share_latest_year,
                    evidence_score=combined_score,
                ),
            )

            if self._is_reached(estimate, round_evidence):
                attempt_log.append(
                    AttemptRecord(
                        round_index=round_index,
                        provider="system",
                        path=result_path,
                        query=query,
                        market_size_latest_year=estimate.market_size_latest_year,
                        market_share_latest_year=estimate.market_share_latest_year,
                        evidence_score=combined_score,
                        action=SearchAction.STOP,
                        reason="达到目标阈值，流程停止",
                        method=estimate.method,
                    )
                )
                return self._build_result(
                    task_id=task_id,
                    status=TaskStatus.REACHED,
                    started_at=started_at,
                    input_model=input_model,
                    latest_year=latest_year,
                    threshold=threshold,
                    path=result_path,
                    market_size=estimate.market_size_latest_year,
                    market_share=estimate.market_share_latest_year,
                    evidence_chain=evidence_chain,
                    attempt_log=attempt_log,
                    reached_target=True,
                )

            if plan is not None:
                self._expand_llm_candidates(candidates, plan, estimate, active_candidate.depth)
            self._expand_candidates(candidates, input_model, estimate, active_candidate.depth)

        # 超过最大轮次仍未达标，返回最优候选
        final_path: List[str] = best_snapshot.path if best_snapshot else []
        final_size = best_snapshot.market_size if best_snapshot else None
        final_share = best_snapshot.market_share if best_snapshot else None
        final_score = best_snapshot.evidence_score if best_snapshot else 0.0

        attempt_log.append(
            AttemptRecord(
                round_index=self.config.max_search_rounds,
                provider="system",
                path=final_path,
                query="max rounds reached",
                market_size_latest_year=final_size,
                market_share_latest_year=final_share,
                evidence_score=final_score,
                action=SearchAction.STOP,
                reason="达到最大搜索轮次，返回当前最优候选",
                method=None,
            )
        )

        return self._build_result(
            task_id=task_id,
            status=TaskStatus.NOT_REACHED,
            started_at=started_at,
            input_model=input_model,
            latest_year=latest_year,
            threshold=threshold,
            path=final_path,
            market_size=final_size,
            market_share=final_share,
            evidence_chain=evidence_chain,
            attempt_log=attempt_log,
            reached_target=False,
        )

    def _collect_evidence(
        self,
        input_model: InferenceInput,
        candidate: CandidatePath,
        query: str,
        round_index: int,
    ) -> Tuple[List[EvidenceRecord], List[AttemptRecord]]:
        evidence: List[EvidenceRecord] = []
        attempts: List[AttemptRecord] = []

        if not self.providers:
            attempts.append(
                AttemptRecord(
                    round_index=round_index,
                    provider="system",
                    path=candidate.path,
                    query=query,
                    market_size_latest_year=None,
                    market_share_latest_year=None,
                    evidence_score=0.0,
                    action=SearchAction.SKIP,
                    reason="未配置搜索渠道，进入估算回退",
                    method=None,
                )
            )
            return evidence, attempts

        for provider in self.providers:
            try:
                hits = provider.search(
                    query=query,
                    max_results=min(self.config.evidence_min_sources, self._provider_limit(provider)),
                    market_path=candidate.path,
                )
            except ProviderError as exc:
                attempts.append(
                    AttemptRecord(
                        round_index=round_index,
                        provider=provider.name,
                        path=candidate.path,
                        query=query,
                        market_size_latest_year=None,
                        market_share_latest_year=None,
                        evidence_score=0.0,
                        action=SearchAction.SKIP,
                        reason=f"渠道不可用，自动降级到下一渠道: {exc}",
                        method=None,
                    )
                )
                continue
            except Exception as exc:  # pragma: no cover - 防御兜底
                attempts.append(
                    AttemptRecord(
                        round_index=round_index,
                        provider=provider.name,
                        path=candidate.path,
                        query=query,
                        market_size_latest_year=None,
                        market_share_latest_year=None,
                        evidence_score=0.0,
                        action=SearchAction.SKIP,
                        reason=f"渠道异常，自动降级到下一渠道: {exc}",
                        method=None,
                    )
                )
                continue

            if not hits:
                attempts.append(
                    AttemptRecord(
                        round_index=round_index,
                        provider=provider.name,
                        path=candidate.path,
                        query=query,
                        market_size_latest_year=None,
                        market_share_latest_year=None,
                        evidence_score=0.0,
                        action=SearchAction.SKIP,
                        reason="渠道返回空结果，继续下一渠道",
                        method=None,
                    )
                )
                continue

            converted: List[EvidenceRecord] = []
            for hit in hits:
                enriched_hit = self._maybe_enrich_hit(input_model, hit, candidate.path, round_index)
                converted.append(self._to_evidence_record(input_model, enriched_hit, candidate.path, round_index))
            evidence.extend(converted)
            attempts.append(
                AttemptRecord(
                    round_index=round_index,
                    provider=provider.name,
                    path=candidate.path,
                    query=query,
                    market_size_latest_year=converted[-1].extracted_market_size,
                    market_share_latest_year=converted[-1].extracted_ratio,
                    evidence_score=score_evidence_chain(converted),
                    action=SearchAction.EXPLORE,
                    reason=f"渠道返回 {len(converted)} 条可用证据",
                    method=None,
                )
            )
            if len(evidence) >= self.config.evidence_min_sources:
                break

        if not evidence:
            attempts.append(
                AttemptRecord(
                    round_index=round_index,
                    provider="fallback",
                    path=candidate.path,
                    query=query,
                    market_size_latest_year=None,
                    market_share_latest_year=None,
                    evidence_score=0.0,
                    action=SearchAction.FALLBACK,
                    reason="当前轮次无可用证据，启用估算回退",
                    method=None,
                )
            )

        return evidence, attempts

    def _maybe_enrich_hit(
        self,
        input_model: InferenceInput,
        hit: ProviderHit,
        market_path: Sequence[str],
        round_index: int,
    ) -> ProviderHit:
        if not self._llm_available():
            return hit
        try:
            extraction = self._call_llm_enrich_hit(input_model, hit, market_path, round_index)
        except Exception:  # pragma: no cover - 防御兜底
            return hit
        if extraction is None:
            return hit
        return apply_llm_extraction(hit, extraction)

    def _to_evidence_record(
        self,
        input_model: InferenceInput,
        hit: ProviderHit,
        market_path: Sequence[str],
        round_index: int,
    ) -> EvidenceRecord:
        record = EvidenceRecord(
            provider=hit.provider,
            query=hit.query,
            title=hit.title,
            url=hit.url,
            snippet=hit.snippet,
            captured_at=hit.captured_at,
            extracted_year=hit.extracted_year,
            extracted_market_size=hit.extracted_market_size,
            extracted_ratio=hit.extracted_ratio,
            method=None,
            market_path=list(hit.market_path or market_path),
            confidence=clamp(hit.confidence, 0.0, 1.0),
        )
        breakdown = score_evidence(input_model, record, round_index)
        record.confidence = clamp((record.confidence + breakdown.total) / 2.0)
        return record

    def _summary_action(self, round_evidence: Sequence[EvidenceRecord], score: float) -> SearchAction:
        action = attempt_action_for_score(score, bool(round_evidence))
        if action == "stop":
            return SearchAction.STOP
        if action == "expand":
            return SearchAction.EXPAND
        if action == "fallback":
            return SearchAction.FALLBACK
        return SearchAction.EXPLORE

    def _summary_reason(
        self,
        round_evidence: Sequence[EvidenceRecord],
        estimate: EstimateResult,
        plan: Optional[LLMPlan] = None,
    ) -> str:
        if plan is not None and plan.reason:
            prefix = f"LLM 规划：{plan.reason}"
            if not round_evidence:
                return f"{prefix}；无直接证据，已使用估算路径"
            return f"{prefix}；使用 {estimate.method.value} 完成本轮估算"
        if not round_evidence:
            return "无直接证据，已使用估算路径"
        return f"使用 {estimate.method.value} 完成本轮估算"

    def _score_round(self, input_model: InferenceInput, round_evidence: Sequence[EvidenceRecord], round_index: int) -> float:
        if not round_evidence:
            return 0.0
        item_scores = []
        for item in round_evidence:
            breakdown = score_evidence(input_model, item, round_index)
            item_scores.append(breakdown.total)
        avg = sum(item_scores) / len(item_scores)
        return clamp(avg + evidence_count_bonus(round_evidence))

    def _is_reached(self, estimate: EstimateResult, round_evidence: Sequence[EvidenceRecord]) -> bool:
        if estimate.market_share_latest_year < self.config.target_share_threshold:
            return False
        return len(round_evidence) >= self.config.evidence_min_sources

    def _provider_of(self, round_evidence: Sequence[EvidenceRecord]) -> str:
        if not round_evidence:
            return "fallback"
        return round_evidence[-1].provider

    def _provider_limit(self, provider: BaseMarketProvider) -> int:
        configured = getattr(provider.config, "max_results", 5)
        return max(1, int(configured))

    def _initial_candidates(self, input_model: InferenceInput) -> List[CandidatePath]:
        root = [input_model.market_scope.value]
        if input_model.product_category:
            root.append(input_model.product_category)
        else:
            root.append(input_model.product_name)

        candidates = [CandidatePath(path=root, parent_market_size=None, depth=0)]

        if input_model.product_name not in root:
            candidates.append(CandidatePath(path=[input_model.market_scope.value, input_model.product_name], parent_market_size=None, depth=0))
        return candidates

    def _pick_candidate(self, candidates: List[CandidatePath]) -> CandidatePath:
        candidates.sort(
            key=lambda item: (
                float("inf") if item.parent_market_size is None else float(item.parent_market_size),
                item.depth,
                len(item.path),
            )
        )
        return candidates.pop(0)

    def _expand_candidates(
        self,
        candidates: List[CandidatePath],
        input_model: InferenceInput,
        estimate: EstimateResult,
        current_depth: int,
    ) -> None:
        if current_depth >= self.config.max_search_rounds:
            return

        base = estimate.market_size_latest_year
        multipliers = [0.78, 0.62, 0.49, 0.36]
        labels = [
            "功能细分",
            "技术细分",
            "应用细分",
            "客户群细分",
        ]

        existing = {tuple(c.path) for c in candidates}
        added = 0
        for label, multiplier in zip(labels, multipliers):
            if added >= self.config.max_children_per_round:
                break
            child_path = list(estimate.market_path)
            node_name = f"{input_model.product_name}{label}"
            if node_name in child_path:
                continue
            child_path.append(node_name)
            key = tuple(child_path)
            if key in existing:
                continue
            child_market = max(input_model.latest_sales_value, base * multiplier)
            candidates.append(
                CandidatePath(
                    path=child_path,
                    parent_market_size=child_market,
                    depth=current_depth + 1,
                )
            )
            existing.add(key)
            added += 1

    def _build_query(self, input_model: InferenceInput, path: Sequence[str], latest_year: int) -> str:
        path_text = " ".join(path)
        return f"{input_model.company_name} {input_model.product_name} {path_text} 市场规模 {latest_year} 市占率"

    def _plan_with_llm(
        self,
        input_model: InferenceInput,
        current_path: Sequence[str],
        latest_year: int,
        round_index: int,
        evidence_chain: Sequence[EvidenceRecord],
    ) -> Optional[LLMPlan]:
        if not self._llm_available():
            return None
        evidence_summary = [
            {
                "provider": item.provider,
                "title": item.title,
                "url": item.url,
                "year": item.extracted_year,
                "market_size": item.extracted_market_size,
                "ratio": item.extracted_ratio,
                "confidence": item.confidence,
                "path": item.market_path,
            }
            for item in evidence_chain[-8:]
        ]
        fallback_query = self._build_query(input_model, current_path, latest_year)
        try:
            plan = self.llm_orchestrator.plan_round(  # type: ignore[union-attr]
                input_model=input_model,
                current_path=current_path,
                latest_year=latest_year,
                round_index=round_index,
                evidence_summary=evidence_summary,
                fallback_query=fallback_query,
            )
        except Exception:  # pragma: no cover - 防御兜底
            return None
        return plan

    def _call_llm_enrich_hit(
        self,
        input_model: InferenceInput,
        hit: ProviderHit,
        market_path: Sequence[str],
        round_index: int,
    ) -> Optional[LLMExtraction]:
        if not self._llm_available():
            return None
        return self.llm_orchestrator.enrich_hit(  # type: ignore[union-attr]
            input_model=input_model,
            hit=hit,
            current_path=market_path,
            round_index=round_index,
        )

    def _expand_llm_candidates(
        self,
        candidates: List[CandidatePath],
        plan: LLMPlan,
        estimate: EstimateResult,
        current_depth: int,
    ) -> None:
        if not plan.next_paths:
            return
        existing = {tuple(candidate.path) for candidate in candidates}
        inserted = 0
        for path in plan.next_paths:
            if inserted >= self.config.max_children_per_round:
                break
            if not path:
                continue
            key = tuple(path)
            if key in existing:
                continue
            candidates.append(
                CandidatePath(
                    path=list(path),
                    parent_market_size=estimate.market_size_latest_year,
                    depth=current_depth + 1,
                )
            )
            existing.add(key)
            inserted += 1

    def _llm_available(self) -> bool:
        orchestrator = self.llm_orchestrator
        if orchestrator is None:
            return False
        checker = getattr(orchestrator, "is_available", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return True

    def _pick_better(self, left: Optional[BestSnapshot], right: BestSnapshot) -> BestSnapshot:
        if left is None:
            return right
        if right.market_share > left.market_share:
            return right
        if right.market_share == left.market_share and right.evidence_score > left.evidence_score:
            return right
        if right.market_share == left.market_share and right.evidence_score == left.evidence_score and right.market_size < left.market_size:
            return right
        return left

    def _build_result(
        self,
        task_id: str,
        status: TaskStatus,
        started_at: datetime,
        input_model: InferenceInput,
        latest_year: int,
        threshold: float,
        path: List[str],
        market_size: Optional[float],
        market_share: Optional[float],
        evidence_chain: Sequence[EvidenceRecord],
        attempt_log: Sequence[AttemptRecord],
        reached_target: bool,
    ) -> TaskResult:
        assumptions = self.estimator.summarize_assumptions(input_model)
        assumptions.append("当直接规模缺失时，按占比/增长率/类比三层回退估算")
        assumptions.append("若最大轮次仍未达标，返回最优候选并标记未达标")

        return TaskResult(
            task_id=task_id,
            status=status,
            started_at=started_at,
            finished_at=datetime.utcnow(),
            input=input_model,
            target_scope=input_model.market_scope,
            latest_year=latest_year,
            target_share_threshold=threshold,
            final_market_path=path,
            market_size_latest_year_wan_cny=market_size,
            market_share_latest_year=market_share,
            reached_target=reached_target,
            evidence_score=score_evidence_chain(evidence_chain),
            evidence_chain=list(evidence_chain),
            attempt_log=list(attempt_log),
            assumption_notes=assumptions,
            error_message=None,
        )
