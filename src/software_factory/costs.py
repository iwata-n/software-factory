"""Append-only USD accounting. Execution rights and late financial receipts are separate."""

from decimal import ROUND_CEILING, Decimal, localcontext

from sqlalchemy import func, select

from .contracts import PricingManifest, digest
from .db import db_now
from .models import Attempt, BudgetVersion, CostEntry, Job, UsageReceipt, Worker
from .security import redact


def price_call(manifest, call):
    rates = PricingManifest.model_validate(manifest)
    with localcontext() as context:
        context.prec = 80
        value = (
            Decimal(call.input_tokens - call.cached_input_tokens)
            * Decimal(rates.input_usd_per_million)
            + Decimal(call.cached_input_tokens) * Decimal(rates.cached_input_usd_per_million)
            + Decimal(call.output_tokens) * Decimal(rates.output_usd_per_million)
        )
        return int(value.to_integral_value(rounding=ROUND_CEILING))


def entry_view(entry):
    return {
        "entry_id": entry.id,
        **{
            key: getattr(entry, key)
            for key in (
                "run_id",
                "job_id",
                "attempt_id",
                "entry_key",
                "account",
                "kind",
                "amount_microusd",
                "currency",
                "origin",
                "metadata_body",
                "created_at",
            )
        },
    }


def receipt_view(receipt):
    return {
        "receipt_id": receipt.id,
        **{
            key: getattr(receipt, key)
            for key in (
                "run_id",
                "job_id",
                "attempt_id",
                "provider",
                "delivery_id",
                "sequence",
                "total_microusd",
                "currency",
                "final",
                "applied",
                "origin",
                "created_at",
                "metadata_body",
            )
        },
    }


class CostCommands:
    def __init__(self, service):
        self.service = service

    def append(self, session, run, attempt, key, account, kind, amount, origin, metadata=None):
        from .service import DomainError

        previous = session.scalar(
            select(CostEntry).where(
                CostEntry.workspace_id == run.workspace_id, CostEntry.entry_key == key
            )
        )
        if previous:
            if (
                previous.run_id != run.id
                or previous.amount_microusd != amount
                or previous.account != account
                or previous.kind != kind
            ):
                raise DomainError(409, "COST_ENTRY_CONFLICT")
            return previous
        entry = CostEntry(
            workspace_id=run.workspace_id,
            run_id=run.id,
            job_id=attempt.job_id if attempt else None,
            attempt_id=attempt.id if attempt else None,
            entry_key=key,
            account=account,
            kind=kind,
            amount_microusd=amount,
            currency="USD",
            origin=origin,
            metadata_body=metadata or {},
            created_at=db_now(session),
        )
        session.add(entry)
        session.flush()
        self.service.checkpoint("cost.entry")
        return entry

    def totals(self, session, run, attempt=None):
        query = select(CostEntry.account, func.sum(CostEntry.amount_microusd)).where(
            CostEntry.workspace_id == run.workspace_id, CostEntry.run_id == run.id
        )
        if attempt:
            query = query.where(CostEntry.attempt_id == attempt.id)
        values = dict(session.execute(query.group_by(CostEntry.account)).all())
        return int(values.get("RESERVED", 0)), int(values.get("SPENT", 0))

    def refresh(self, session, run):
        run.reserved_microusd, run.spent_microusd = self.totals(session, run)
        unresolved = []
        for entry in session.scalars(
            select(CostEntry).where(CostEntry.run_id == run.id, CostEntry.kind == "PROVISIONAL")
        ):
            latest = session.scalar(
                select(UsageReceipt)
                .where(UsageReceipt.attempt_id == entry.attempt_id, UsageReceipt.applied.is_(True))
                .order_by(UsageReceipt.sequence.desc())
                .limit(1)
            )
            if latest is None or not latest.final:
                unresolved.append(entry.metadata_body.get("unknown", False))
        run.cost_state = "UNKNOWN" if any(unresolved) else "PROVISIONAL" if unresolved else "KNOWN"

    def initial_budget(self, session, run):
        session.add(
            BudgetVersion(
                workspace_id=run.workspace_id,
                run_id=run.id,
                version=1,
                limit_microusd=run.budget_microusd,
                reason="compiled initial budget",
                created_at=db_now(session),
            )
        )

    def require_balanced(self, session, run):
        from .service import DomainError

        if self.totals(session, run) != (run.reserved_microusd, run.spent_microusd):
            raise DomainError(503, "COST_LEDGER_INCONSISTENT")
        budget = session.scalar(
            select(BudgetVersion).where(
                BudgetVersion.run_id == run.id, BudgetVersion.version == run.budget_version
            )
        )
        if budget is None or budget.limit_microusd != run.budget_microusd:
            raise DomainError(503, "BUDGET_LEDGER_INCONSISTENT")

    def reserve(self, session, run, attempt):
        self.append(
            session,
            run,
            attempt,
            "reserve:" + attempt.id,
            "RESERVED",
            "RESERVATION",
            attempt.reservation_microusd,
            "control_plane",
        )
        self.refresh(session, run)

    def settle(self, session, run, attempt, usage, *, uncertain=False):
        job = session.get(Job, attempt.job_id)
        latest = session.scalar(
            select(UsageReceipt)
            .where(
                UsageReceipt.workspace_id == run.workspace_id,
                UsageReceipt.attempt_id == attempt.id,
                UsageReceipt.applied.is_(True),
            )
            .order_by(UsageReceipt.sequence.desc())
            .limit(1)
        )
        settled_final = latest is not None and latest.final
        model_job = (
            job.kind in {"PLAN", "IMPLEMENT", "REPAIR", "REVIEW"}
            and run.snapshot["agent_adapter"] != "fake"
        )
        known = settled_final or (
            usage is not None and not uncertain and not model_job and latest is None
        )
        reserved, spent = self.totals(session, run, attempt)
        if settled_final:
            total = latest.total_microusd
        elif known:
            total = usage
        else:
            total = max(spent, attempt.reservation_microusd, usage or 0)
        self.append(
            session,
            run,
            attempt,
            "release:" + attempt.id,
            "RESERVED",
            "RELEASE",
            -reserved,
            "control_plane",
        )
        kind = ("ADJUSTMENT" if spent else "ACTUAL") if known else "PROVISIONAL"
        self.append(
            session,
            run,
            attempt,
            "settlement:" + attempt.id,
            "SPENT",
            kind,
            total - spent,
            "receipt"
            if settled_final
            else "simulation"
            if known and run.snapshot["agent_adapter"] == "fake"
            else "no_model_execution"
            if known
            else "control_plane",
            {
                "unknown": not uncertain and not known,
                "reported_total_microusd": usage,
                "receipt_id": latest.id if latest else None,
            },
        )
        self.refresh(session, run)
        return known

    def receive(self, session, actor, attempt_id, data):
        from .service import DomainError, event, owned, request_secrets, transition

        attempt = owned(session, Attempt, attempt_id, actor.workspace_id)
        if attempt.worker_id != actor.id:
            raise DomainError(404, "NOT_FOUND")
        job = owned(session, Job, attempt.job_id, actor.workspace_id)
        from .models import Run

        run = owned(session, Run, job.run_id, actor.workspace_id)
        worker = owned(session, Worker, actor.id, actor.workspace_id)
        if run.snapshot["agent_adapter"] != "fake" and (
            not worker.managed or "usage:trusted" not in worker.capabilities
        ):
            raise DomainError(403, "TRUSTED_USAGE_AUTHORITY_REQUIRED")
        payload = data.model_dump()
        # The typed numeric token counters are metering data; identifiers are still scrubbed.
        without_calls = {key: value for key, value in payload.items() if key != "calls"}
        if redact(without_calls, request_secrets.get()) != without_calls or any(
            redact(call.call_id, request_secrets.get()) != call.call_id for call in data.calls
        ):
            raise DomainError(422, "SENSITIVE_INPUT")
        manifest = run.snapshot["policy"].get("pricing_manifest")
        expected_provider = manifest["provider"] if manifest else "fixture"
        if data.provider != expected_provider:
            raise DomainError(422, "USAGE_PROVIDER_MISMATCH")
        if data.pricing_manifest_hash is not None and (
            manifest is None or data.pricing_manifest_hash != digest(manifest)
        ):
            raise DomainError(409, "PRICING_MANIFEST_MISMATCH")
        if data.calls:
            if not manifest or data.pricing_manifest_hash != digest(manifest):
                raise DomainError(409, "PRICING_MANIFEST_MISMATCH")
            if sum(price_call(manifest, call) for call in data.calls) != data.total_microusd:
                raise DomainError(409, "USAGE_AMOUNT_MISMATCH")
        elif run.snapshot["agent_adapter"] != "fake" and data.total_microusd != 0:
            raise DomainError(422, "MODEL_USAGE_CALLS_REQUIRED")
        fingerprint = digest({"attempt_id": attempt.id, "delivery": payload})
        previous = session.scalar(
            select(UsageReceipt).where(
                UsageReceipt.workspace_id == actor.workspace_id,
                UsageReceipt.provider == data.provider,
                UsageReceipt.delivery_id == data.delivery_id,
            )
        )
        if previous:
            if previous.payload_hash != fingerprint:
                raise DomainError(409, "USAGE_DELIVERY_CONFLICT")
            return {"receipt": receipt_view(previous), "run": self.view(session, run)}
        latest = session.scalar(
            select(UsageReceipt)
            .where(UsageReceipt.attempt_id == attempt.id, UsageReceipt.applied.is_(True))
            .order_by(UsageReceipt.sequence.desc())
            .limit(1)
        )
        sequence = session.scalar(
            select(UsageReceipt).where(
                UsageReceipt.attempt_id == attempt.id, UsageReceipt.sequence == data.sequence
            )
        )
        if sequence:
            raise DomainError(409, "USAGE_SEQUENCE_CONFLICT")
        applied = (latest is None or data.sequence > latest.sequence) and not (
            latest and latest.final and not data.final
        )
        if applied and not data.final and latest and data.total_microusd < latest.total_microusd:
            raise DomainError(409, "USAGE_TOTAL_DECREASED")
        receipt = UsageReceipt(
            workspace_id=run.workspace_id,
            run_id=run.id,
            job_id=job.id,
            attempt_id=attempt.id,
            provider=data.provider,
            delivery_id=data.delivery_id,
            sequence=data.sequence,
            total_microusd=data.total_microusd,
            currency=data.currency,
            final=data.final,
            applied=bool(applied),
            payload_hash=fingerprint,
            origin="simulation"
            if run.snapshot.get("simulation", run.snapshot.get("agent_adapter", "fake") == "fake")
            else "trusted_supervisor",
            metadata_body={
                "calls": [c.model_dump() for c in data.calls],
                "pricing_manifest_hash": data.pricing_manifest_hash,
            },
            created_at=db_now(session),
        )
        session.add(receipt)
        session.flush()
        if applied:
            reserved, spent = self.totals(session, run, attempt)
            # A partial report cannot refund a conservative settlement after execution rights ended.
            total = (
                data.total_microusd
                if data.final or attempt.state == "LEASED"
                else max(spent, data.total_microusd)
            )
            self.append(
                session,
                run,
                attempt,
                "receipt:" + receipt.id,
                "SPENT",
                "ADJUSTMENT" if spent else "ACTUAL",
                total - spent,
                receipt.origin,
                {
                    "receipt_id": receipt.id,
                    "confirmed_total_microusd": data.total_microusd,
                    "final": data.final,
                },
            )
            if attempt.state == "LEASED":
                remaining = max(0, attempt.reservation_microusd - total)
                self.append(
                    session,
                    run,
                    attempt,
                    "rebalance:" + receipt.id,
                    "RESERVED",
                    "RESERVATION_REBALANCE",
                    remaining - reserved,
                    "control_plane",
                    {"receipt_id": receipt.id},
                )
            self.refresh(session, run)
            event(
                session,
                run,
                "USAGE_RECONCILED",
                {
                    "attempt_id": attempt.id,
                    "receipt_id": receipt.id,
                    "final": data.final,
                    "cost_state": run.cost_state,
                },
            )
            if run.spent_microusd > run.budget_microusd:
                if run.status not in {"SUCCEEDED", "FAILED", "CANCELLED", "CANCELLING"}:
                    active_job = session.scalar(
                        select(Job)
                        .join(Attempt, (Attempt.job_id == Job.id))
                        .where(Job.run_id == run.id, Attempt.state == "LEASED")
                        .limit(1)
                    )
                    if (run.wait_context or {}).get("kind") != "COST_BLOCKED":
                        run.wait_context = {
                            "kind": "COST_BLOCKED",
                            "job_id": active_job.id if active_job else job.id,
                            "cause": "BUDGET_EXCEEDED",
                            "previous_wait": run.wait_context,
                        }
                    if active_job is not None:
                        transition(session, run, "BUDGET_STOP_REQUESTED")
                    else:
                        transition(session, run, "BLOCKED", "BUDGET_EXCEEDED")
        return {"receipt": receipt_view(receipt), "run": self.view(session, run)}

    def view(self, session, run):
        reserved, spent = self.totals(session, run)
        return {
            "run_id": run.id,
            "currency": "USD",
            "budget_microusd": run.budget_microusd,
            "budget_version": run.budget_version,
            "spent_microusd": spent,
            "reserved_microusd": reserved,
            "available_microusd": run.budget_microusd - spent - reserved,
            "cost_state": run.cost_state,
        }
