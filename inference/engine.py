from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional, Sequence, Tuple

from .estimators import EstimateResult, MarketEstimator
from .fx import FxRateResult, get_usd_cny_rate
from .llm_orchestrator import (
    LLMEvidenceReview,
    LLMExtraction,
    LLMFitCheck,
    LLMOrchestrator,
    LLMPathProposal,
    LLMPlan,
    apply_llm_extraction,
)
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
    verified_sources: int
    total_sources: int
    fit_passed: bool
    fit_reason: str
    fit_confidence: float


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

    def run(
        self,
        task_id: str,
        input_model: InferenceInput,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> TaskResult:
        started_at = datetime.utcnow()
        latest_year = input_model.latest_sales_year
        threshold = self.config.target_share_threshold
        fx_result = self._resolve_fx_rate()
        self._apply_fx_rate_to_providers(fx_result)

        evidence_chain: List[EvidenceRecord] = []
        attempt_log: List[AttemptRecord] = []
        last_fit_check: Optional[LLMFitCheck] = None

        candidates = self._initial_candidates(input_model)
        seed_pending: set[tuple[str, ...]] = set()
        seed_proposal = self._propose_seed_paths(input_model=input_model, latest_year=latest_year)
        if seed_proposal is not None:
            inserted_paths = self._inject_seed_candidates(candidates, seed_proposal, input_model)
            seed_pending.update(inserted_paths)
            inserted = len(inserted_paths)
            attempt_log.append(
                AttemptRecord(
                    round_index=0,
                    provider="llm-path-proposal",
                    path=candidates[0].path if candidates else [],
                    query="seed market paths",
                    market_size_latest_year=None,
                    market_share_latest_year=None,
                    evidence_score=seed_proposal.confidence,
                    action=SearchAction.EXPLORE if inserted > 0 else SearchAction.SKIP,
                    reason=(
                        f"LLM 预生成 {inserted} 条细分路径候选：{seed_proposal.reason}"
                        if inserted > 0
                        else f"LLM 返回路径已去重/过滤为空：{seed_proposal.reason}"
                    ),
                    method=None,
                )
            )
        best_snapshot: Optional[BestSnapshot] = None
        best_fit_snapshot: Optional[BestSnapshot] = None
        all_snapshots: List[BestSnapshot] = []
        last_fit_passed = not self.config.market_fit_required
        last_fit_reason = "尚未执行主导产品-细分市场一致性校验"
        last_fit_confidence = 0.0

        round_limit = min(50, max(self.config.max_search_rounds, len(seed_pending) + 2))
        for round_index in range(1, round_limit + 1):
            if should_stop is not None and should_stop():
                attempt_log.append(
                    AttemptRecord(
                        round_index=round_index,
                        provider="system",
                        path=best_snapshot.path if best_snapshot else [],
                        query="task cancelled",
                        market_size_latest_year=best_snapshot.market_size if best_snapshot else None,
                        market_share_latest_year=best_snapshot.market_share if best_snapshot else None,
                        evidence_score=best_snapshot.evidence_score if best_snapshot else 0.0,
                        action=SearchAction.STOP,
                        reason="收到停止指令，已中断推理并返回当前最优候选",
                        method=None,
                    )
                )
                return self._build_result(
                    task_id=task_id,
                    status=TaskStatus.CANCELLED,
                    started_at=started_at,
                    input_model=input_model,
                    latest_year=latest_year,
                    threshold=threshold,
                    path=best_snapshot.path if best_snapshot else [],
                    market_size=best_snapshot.market_size if best_snapshot else None,
                    market_share=best_snapshot.market_share if best_snapshot else None,
                    evidence_chain=evidence_chain,
                    attempt_log=attempt_log,
                    reached_target=False,
                    fit_check=last_fit_check,
                    fit_passed_override=last_fit_passed,
                    fit_reason_override=last_fit_reason,
                    fit_confidence_override=last_fit_confidence,
                    fx_result=fx_result,
                )
            if not candidates:
                break

            current = self._pick_candidate(candidates)
            seed_pending.discard(tuple(current.path))
            plan = self._plan_with_llm(input_model, current.path, latest_year, round_index, evidence_chain)
            active_path = plan.market_path if plan and plan.market_path else current.path
            active_candidate = CandidatePath(path=active_path, parent_market_size=current.parent_market_size, depth=current.depth)
            query = plan.query if plan and plan.query else self._build_query(input_model, active_candidate.path, latest_year)
            result_path = active_candidate.path

            round_evidence, round_attempts = self._collect_evidence(
                input_model,
                active_candidate,
                query,
                round_index,
                provider_queries=plan.provider_queries if plan else None,
                should_stop=should_stop,
            )
            attempt_log.extend(round_attempts)
            if should_stop is not None and should_stop():
                return self._build_result(
                    task_id=task_id,
                    status=TaskStatus.CANCELLED,
                    started_at=started_at,
                    input_model=input_model,
                    latest_year=latest_year,
                    threshold=threshold,
                    path=best_snapshot.path if best_snapshot else [],
                    market_size=best_snapshot.market_size if best_snapshot else None,
                    market_share=best_snapshot.market_share if best_snapshot else None,
                    evidence_chain=evidence_chain,
                    attempt_log=attempt_log,
                    reached_target=False,
                    fit_check=last_fit_check,
                    fit_passed_override=last_fit_passed,
                    fit_reason_override=last_fit_reason,
                    fit_confidence_override=last_fit_confidence,
                    fx_result=fx_result,
                )

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

            existing_evidence_keys = {(item.url, item.title, item.quote_text or "") for item in evidence_chain}
            for item in round_evidence:
                key = (item.url, item.title, item.quote_text or "")
                if key in existing_evidence_keys:
                    continue
                evidence_chain.append(item)
                existing_evidence_keys.add(key)

            fit_check = self._validate_market_fit(
                input_model=input_model,
                current_path=result_path,
                latest_year=latest_year,
                estimate=estimate,
                round_evidence=round_evidence,
            )
            if fit_check is not None:
                last_fit_check = fit_check
            fit_passed, fit_reason, fit_confidence = self._resolve_fit_decision(fit_check)
            last_fit_passed = fit_passed
            last_fit_reason = fit_reason
            last_fit_confidence = fit_confidence

            snapshot = BestSnapshot(
                path=result_path,
                market_size=estimate.market_size_latest_year,
                market_share=estimate.market_share_latest_year,
                evidence_score=combined_score,
                verified_sources=sum(1 for item in round_evidence if item.source_verified),
                total_sources=len(round_evidence),
                fit_passed=fit_passed,
                fit_reason=fit_reason,
                fit_confidence=fit_confidence,
            )
            all_snapshots.append(snapshot)
            best_snapshot = self._pick_better(best_snapshot, snapshot)
            if fit_passed:
                best_fit_snapshot = self._pick_better(best_fit_snapshot, snapshot)

            if not fit_passed:
                attempt_log.append(
                    AttemptRecord(
                        round_index=round_index,
                        provider="llm-fit-check",
                        path=result_path,
                        query=query,
                        market_size_latest_year=estimate.market_size_latest_year,
                        market_share_latest_year=estimate.market_share_latest_year,
                        evidence_score=combined_score,
                        action=SearchAction.EXPAND,
                        reason=f"主导产品与细分市场未通过一致性闸门，继续细分：{fit_reason}",
                        method=estimate.method,
                    )
                )

            if self._is_reached(estimate, round_evidence, fit_passed):
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
                        reason=(
                            "已达到目标阈值并通过一致性校验，但按策略继续穷尽候选路径后再择优"
                            if fit_passed
                            else "达到目标阈值，但细分一致性未通过，继续搜索"
                        ),
                        method=estimate.method,
                    )
                )

            if plan is not None:
                self._expand_llm_candidates(candidates, plan, estimate, active_candidate.depth)
            self._expand_candidates(candidates, input_model, estimate, active_candidate.depth)

        if seed_pending:
            attempt_log.append(
                AttemptRecord(
                    round_index=round_limit,
                    provider="system",
                    path=[],
                    query="seed paths remain",
                    market_size_latest_year=None,
                    market_share_latest_year=None,
                    evidence_score=0.0,
                    action=SearchAction.EXPAND,
                    reason=f"达到轮次上限，仍有 {len(seed_pending)} 条 LLM 细分候选未穷尽",
                    method=None,
                )
            )

        final_snapshot = self._select_final_snapshot(
            snapshots=all_snapshots,
            fallback_fit=best_fit_snapshot,
            fallback_any=best_snapshot,
            threshold=threshold,
        )

        final_path: List[str] = final_snapshot.path if final_snapshot else []
        final_size = final_snapshot.market_size if final_snapshot else None
        final_share = final_snapshot.market_share if final_snapshot else None
        final_score = final_snapshot.evidence_score if final_snapshot else 0.0
        final_fit_passed = final_snapshot.fit_passed if final_snapshot else last_fit_passed
        final_fit_reason = final_snapshot.fit_reason if final_snapshot else last_fit_reason
        final_fit_confidence = final_snapshot.fit_confidence if final_snapshot else last_fit_confidence

        attempt_log.append(
            AttemptRecord(
                round_index=round_limit,
                provider="system",
                path=final_path,
                query="search completed",
                market_size_latest_year=final_size,
                market_share_latest_year=final_share,
                evidence_score=final_score,
                action=SearchAction.STOP,
                reason=(
                    "已穷尽可执行候选，返回最具说服力且达标的路径"
                    if final_fit_passed and final_share is not None and final_share >= threshold
                    else f"已完成候选搜索，但未找到达标且可信路径：{final_fit_reason}"
                ),
                method=None,
            )
        )

        reached_target = bool(
            final_fit_passed
            and final_share is not None
            and final_share >= threshold
            and final_snapshot is not None
            and final_snapshot.total_sources >= self.config.evidence_min_sources
        )
        return self._build_result(
            task_id=task_id,
            status=TaskStatus.REACHED if reached_target else TaskStatus.NOT_REACHED,
            started_at=started_at,
            input_model=input_model,
            latest_year=latest_year,
            threshold=threshold,
            path=final_path,
            market_size=final_size,
            market_share=final_share,
            evidence_chain=evidence_chain,
            attempt_log=attempt_log,
            reached_target=reached_target,
            fit_check=last_fit_check,
            fit_passed_override=final_fit_passed,
            fit_reason_override=final_fit_reason,
            fit_confidence_override=final_fit_confidence,
            fx_result=fx_result,
        )

    def _collect_evidence(
        self,
        input_model: InferenceInput,
        candidate: CandidatePath,
        query: str,
        round_index: int,
        provider_queries: Optional[dict[str, str]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
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
            if should_stop is not None and should_stop():
                attempts.append(
                    AttemptRecord(
                        round_index=round_index,
                        provider="system",
                        path=candidate.path,
                        query=query,
                        market_size_latest_year=None,
                        market_share_latest_year=None,
                        evidence_score=0.0,
                        action=SearchAction.STOP,
                        reason="收到停止指令，本轮检索已中断",
                        method=None,
                    )
                )
                break
            try:
                provider_query = query
                if provider_queries:
                    provider_query = provider_queries.get(provider.name, query)
                hits = provider.search(
                    query=provider_query,
                    max_results=min(max(1, self.config.evidence_min_sources), self._provider_limit(provider)),
                    market_path=candidate.path,
                )
            except ProviderError as exc:
                attempts.append(
                    AttemptRecord(
                        round_index=round_index,
                        provider=provider.name,
                        path=candidate.path,
                        query=provider_query,
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
                        query=provider_query,
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
                        query=provider_query,
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
            reviewed_out_count = 0
            for hit in hits:
                enriched_hit = self._maybe_enrich_hit(input_model, hit, candidate.path, round_index)
                record = self._to_evidence_record(input_model, enriched_hit, candidate.path, round_index)
                review = self._review_evidence_by_llm(input_model, candidate.path, input_model.latest_sales_year, enriched_hit)
                if review is not None:
                    record.llm_review_passed = bool(review.is_target_market and review.data_quality_passed)
                    record.llm_review_reason = review.reason
                    if not record.llm_review_passed:
                        reviewed_out_count += 1
                        continue
                converted.append(record)

            verified_converted = [item for item in converted if item.source_verified]
            ai_converted = [item for item in converted if not item.source_verified]

            if not verified_converted and not ai_converted:
                attempts.append(
                    AttemptRecord(
                        round_index=round_index,
                        provider=provider.name,
                        path=candidate.path,
                        query=provider_query,
                        market_size_latest_year=None,
                        market_share_latest_year=None,
                        evidence_score=0.0,
                        action=SearchAction.SKIP,
                        reason=(
                            "渠道结果经 LLM 审核后均不可用，继续下一渠道"
                            if reviewed_out_count > 0
                            else "渠道仅返回 AI 摘要，未拿到可核验原文，继续下一渠道"
                        ),
                        method=None,
                    )
                )
                continue

            if verified_converted:
                evidence.extend(verified_converted)
            else:
                evidence.extend(ai_converted)

            accepted = verified_converted if verified_converted else ai_converted
            verified_count = len(verified_converted)
            unverified_count = len(ai_converted)
            attempts.append(
                AttemptRecord(
                    round_index=round_index,
                    provider=provider.name,
                    path=candidate.path,
                    query=provider_query,
                    market_size_latest_year=accepted[-1].extracted_market_size,
                    market_share_latest_year=accepted[-1].extracted_ratio,
                    evidence_score=score_evidence_chain(accepted),
                    action=SearchAction.EXPLORE,
                    reason=(
                        (
                            f"渠道返回 {verified_count} 条原文已核验证据，{unverified_count} 条未核验证据；"
                            f"LLM 过滤掉 {reviewed_out_count} 条不匹配/低质量证据"
                        )
                        if verified_count > 0
                        else (
                            f"渠道未命中原文证据，暂采信 {unverified_count} 条 AI 检索数据并标注待核验；"
                            f"LLM 过滤掉 {reviewed_out_count} 条不匹配/低质量证据"
                        )
                    ),
                    method=None,
                )
            )

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
        if hit.source_verified and (hit.extracted_market_size is not None or hit.extracted_ratio is not None):
            return hit
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
            search_page_url=hit.search_page_url,
            extracted_year=hit.extracted_year,
            extracted_market_size=hit.extracted_market_size,
            extracted_ratio=hit.extracted_ratio,
            extracted_growth_rate=hit.extracted_growth_rate,
            method=None,
            market_path=list(hit.market_path or market_path),
            confidence=clamp(hit.confidence, 0.0, 1.0),
            source_verified=hit.source_verified,
            quote_text=hit.quote_text,
            market_size_original_value=hit.market_size_original_value,
            market_size_original_unit=hit.market_size_original_unit,
            market_size_original_currency=hit.market_size_original_currency,
            usd_cny_rate_used=hit.usd_cny_rate_used,
            conversion_formula=hit.conversion_formula,
        )
        breakdown = score_evidence(input_model, record, round_index)
        record.confidence = clamp((record.confidence + breakdown.total) / 2.0)
        return record

    def _review_evidence_by_llm(
        self,
        input_model: InferenceInput,
        current_path: Sequence[str],
        latest_year: int,
        hit: ProviderHit,
    ) -> Optional[LLMEvidenceReview]:
        if not self._llm_available():
            return None
        reviewer = getattr(self.llm_orchestrator, "review_evidence_hit", None)  # type: ignore[union-attr]
        if not callable(reviewer):
            return None
        try:
            return reviewer(
                input_model=input_model,
                current_path=current_path,
                latest_year=latest_year,
                hit=hit,
            )
        except Exception:
            return None

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

    def _is_reached(self, estimate: EstimateResult, round_evidence: Sequence[EvidenceRecord], fit_passed: bool) -> bool:
        if estimate.market_share_latest_year < self.config.target_share_threshold:
            return False
        if not fit_passed:
            return False
        return len(round_evidence) >= self.config.evidence_min_sources

    def _resolve_fit_decision(self, fit_check: Optional[LLMFitCheck]) -> Tuple[bool, str, float]:
        if fit_check is None:
            return True, "LLM 未返回一致性结论，按宽松策略暂时通过（建议人工复核）", 0.4

        confidence = clamp(fit_check.confidence)
        reason = fit_check.reason or "LLM 未返回一致性原因"
        if not fit_check.is_aligned:
            return False, reason, confidence
        if confidence < self.config.market_fit_min_confidence:
            return True, f"{reason}（置信度 {confidence:.2f}，建议人工复核）", confidence
        return True, reason, confidence

    def _resolve_fx_rate(self) -> FxRateResult:
        return get_usd_cny_rate(default_rate=self.config.cny_per_usd)

    def _apply_fx_rate_to_providers(self, fx_result: FxRateResult) -> None:
        for provider in self.providers:
            setter = getattr(provider, "set_fx_rate", None)
            if callable(setter):
                try:
                    setter(fx_result.usd_cny, fx_result.source, fx_result.is_realtime)
                except Exception:
                    continue

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
        if input_model.product_code:
            candidates.append(
                CandidatePath(
                    path=[input_model.market_scope.value, f"国统局编码{input_model.product_code}", input_model.product_name],
                    parent_market_size=None,
                    depth=0,
                )
            )
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
        competitor_text = " ".join(input_model.competitors[:3])
        product_code_text = f" 产品代码 {input_model.product_code}。" if input_model.product_code else ""
        intro_product = " ".join((input_model.product_intro or "").split())[:220]
        intro_company = " ".join((input_model.company_intro or "").split())[:220]
        return (
            f"企业：{input_model.company_name}；主导产品：{input_model.product_name}；细分路径：{path_text}。"
            f"{product_code_text}"
            f"产品线索：{intro_product or '无'}。企业线索：{intro_company or '无'}。"
            f"请优先检索最近年份（优先 {latest_year}）细分市场规模，返回单位、币种、年份、CAGR/增长率；"
            "若无直接规模，请返回“占比 + 上级市场规模”的可推导数据链，并附原文标题和链接。"
            f"{(' 对手参考：' + competitor_text) if competitor_text else ''}"
        )

    def _propose_seed_paths(self, *, input_model: InferenceInput, latest_year: int) -> Optional[LLMPathProposal]:
        if not self._llm_available():
            return None
        max_paths = max(self.config.max_children_per_round * 3, 8)
        try:
            return self.llm_orchestrator.propose_market_paths(  # type: ignore[union-attr]
                input_model=input_model,
                latest_year=latest_year,
                max_paths=max_paths,
            )
        except Exception:
            return None

    def _inject_seed_candidates(
        self,
        candidates: List[CandidatePath],
        proposal: LLMPathProposal,
        input_model: InferenceInput,
    ) -> List[tuple[str, ...]]:
        existing = {tuple(item.path) for item in candidates}
        inserted_paths: List[tuple[str, ...]] = []
        for raw_path in proposal.market_paths:
            normalized = self._normalize_seed_path(raw_path, input_model)
            if not normalized:
                continue
            key = tuple(normalized)
            if key in existing:
                continue
            candidates.append(
                CandidatePath(
                    path=normalized,
                    parent_market_size=None,
                    depth=max(0, len(normalized) - 1),
                )
            )
            existing.add(key)
            inserted_paths.append(key)
        return inserted_paths

    def _normalize_seed_path(self, path: Sequence[str], input_model: InferenceInput) -> List[str]:
        if not path:
            return []
        cleaned: List[str] = []
        for node in path:
            text = " ".join(str(node).strip().split())
            if not text:
                continue
            if cleaned and cleaned[-1] == text:
                continue
            cleaned.append(text)
        if not cleaned:
            return []

        scope_alias = {
            "中国": "CN",
            "国内": "CN",
            "中国市场": "CN",
            "全球": "GLOBAL",
            "国际": "GLOBAL",
            "全球市场": "GLOBAL",
        }
        first = cleaned[0]
        if first in {"CN", "GLOBAL"}:
            pass
        elif first in scope_alias:
            cleaned[0] = scope_alias[first]
        else:
            cleaned.insert(0, input_model.market_scope.value)

        if input_model.product_name and input_model.product_name not in cleaned:
            cleaned.insert(1, input_model.product_name)

        return cleaned[:6]

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

    def _validate_market_fit(
        self,
        *,
        input_model: InferenceInput,
        current_path: Sequence[str],
        latest_year: int,
        estimate: EstimateResult,
        round_evidence: Sequence[EvidenceRecord],
    ) -> Optional[LLMFitCheck]:
        if not self._llm_available():
            return None
        evidence_summary = [
            {
                "title": item.title,
                "url": item.url,
                "quote": item.quote_text or item.snippet,
                "market_size_wan_cny": item.extracted_market_size,
                "market_share_ratio": item.extracted_ratio,
                "source_verified": item.source_verified,
                "path": item.market_path,
            }
            for item in round_evidence[:8]
        ]
        try:
            return self.llm_orchestrator.validate_market_fit(  # type: ignore[union-attr]
                input_model=input_model,
                current_path=current_path,
                latest_year=latest_year,
                market_size=estimate.market_size_latest_year,
                market_share=estimate.market_share_latest_year,
                evidence_summary=evidence_summary,
            )
        except Exception:
            return None

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

    def _snapshot_strength(self, snapshot: BestSnapshot) -> float:
        verified_bonus = min(snapshot.verified_sources, 3) * 0.06
        source_bonus = min(snapshot.total_sources, 4) * 0.03
        return clamp(snapshot.evidence_score + verified_bonus + source_bonus + snapshot.fit_confidence * 0.12)

    def _select_final_snapshot(
        self,
        *,
        snapshots: Sequence[BestSnapshot],
        fallback_fit: Optional[BestSnapshot],
        fallback_any: Optional[BestSnapshot],
        threshold: float,
    ) -> Optional[BestSnapshot]:
        if not snapshots:
            return fallback_fit or fallback_any

        fit_candidates = [s for s in snapshots if s.fit_passed]
        reached_fit_candidates = [s for s in fit_candidates if s.market_share >= threshold]

        def ranking_key(item: BestSnapshot):
            return (
                self._snapshot_strength(item),
                item.market_share,
                item.verified_sources,
                item.total_sources,
                -item.market_size,
            )

        if reached_fit_candidates:
            return sorted(reached_fit_candidates, key=ranking_key, reverse=True)[0]
        if fit_candidates:
            return sorted(fit_candidates, key=ranking_key, reverse=True)[0]
        return sorted(list(snapshots), key=ranking_key, reverse=True)[0]

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
        fit_check: Optional[LLMFitCheck],
        fit_passed_override: Optional[bool],
        fit_reason_override: Optional[str],
        fit_confidence_override: Optional[float],
        fx_result: FxRateResult,
    ) -> TaskResult:
        assumptions = self.estimator.summarize_assumptions(input_model)
        assumptions.append("当直接规模缺失时，按占比/增长率/类比三层回退估算")
        assumptions.append("若最大轮次仍未达标，返回最优候选并标记未达标")
        assumptions.append("优先采用原文可核验证据；若未找到原文，则暂采信秘塔/元宝检索数据并显式标注待核验")
        assumptions.append(
            f"美元兑人民币汇率采用 {fx_result.usd_cny:.6f}（来源：{fx_result.source}，实时：{'是' if fx_result.is_realtime else '否'}）"
        )
        assumptions.append(
            f"主导产品-细分市场一致性为{'强约束' if self.config.market_fit_required else '非强约束'}，"
            f"最低置信度阈值 {self.config.market_fit_min_confidence:.2f}"
        )
        verified_count = sum(1 for item in evidence_chain if item.source_verified)
        unverified_count = max(0, len(evidence_chain) - verified_count)
        assumptions.append(
            f"证据核验统计：原文已核验 {verified_count} 条，未核验 {unverified_count} 条（未核验条目已标注）"
        )

        if fit_passed_override is None:
            fit_passed, fit_reason, fit_confidence = self._resolve_fit_decision(fit_check)
        else:
            fit_passed = bool(fit_passed_override)
            fit_reason = fit_reason_override or ("主导产品与细分市场一致" if fit_passed else "主导产品与细分市场不一致")
            if fit_confidence_override is None:
                fit_confidence = 1.0 if fit_passed else 0.0
            else:
                fit_confidence = clamp(fit_confidence_override)

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
            market_fit_passed=fit_passed,
            market_fit_reason=fit_reason,
            market_fit_confidence=fit_confidence,
            usd_cny_rate_used=fx_result.usd_cny,
            usd_cny_rate_source=fx_result.source,
            usd_cny_rate_realtime=fx_result.is_realtime,
            error_message=None,
        )
