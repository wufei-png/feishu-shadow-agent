from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .agent_backend import AgentBackend, AgentRunResult
from .agent_invocation import AgentAttemptOutcome, AgentInvoker
from .context_access import ContextAccessBuilder
from .prompt import (
    BaseTaskSessionOutput,
    FollowupTaskSessionOutput,
    InitialTaskSessionOutput,
    build_task_session_prompt,
)
from .store.sqlite_store import SQLiteStore
from .types import NormalizedMessage, TaskRecord


@dataclass(frozen=True)
class TaskSessionPromptPlan:
    session_id: str | None
    task_message_ids: list[str]
    prompt_message_ids: list[str]
    output_model: type[BaseTaskSessionOutput]
    reply_target_message_ids: list[str]


@dataclass(frozen=True)
class TaskSessionRunResult:
    plan: TaskSessionPromptPlan
    prompt: str
    outcome: AgentAttemptOutcome
    result: AgentRunResult | None
    output: BaseTaskSessionOutput | None
    validation_error: ValidationError | None = None


class TaskSessionRunner:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        agent_backend: AgentBackend,
        agent_invoker: AgentInvoker,
        context_access: ContextAccessBuilder,
    ):
        self.store = store
        self.agent_backend = agent_backend
        self.agent_invoker = agent_invoker
        self.context_access = context_access

    def build_plan(
        self, *, task: TaskRecord, message: NormalizedMessage
    ) -> TaskSessionPromptPlan:
        session_id = self.store.get_initialized_agent_session_id(
            task.id, backend_provider=str(self.agent_backend.provider)
        )
        task_message_ids = self.store.list_task_message_ids(task.id)
        prompt_message_ids = self.prompt_message_ids(
            task=task,
            message=message,
            session_id=session_id,
            task_message_ids=task_message_ids,
        )
        return TaskSessionPromptPlan(
            session_id=session_id,
            task_message_ids=task_message_ids,
            prompt_message_ids=prompt_message_ids,
            output_model=InitialTaskSessionOutput
            if session_id is None
            else FollowupTaskSessionOutput,
            reply_target_message_ids=reply_target_message_ids(
                task=task, current_message_id=message.message_id
            ),
        )

    def prompt_message_ids(
        self,
        *,
        task: TaskRecord,
        message: NormalizedMessage,
        session_id: str | None,
        task_message_ids: list[str] | None = None,
    ) -> list[str]:
        if session_id is not None:
            # Resumed agent sessions already carry task history; sending only
            # the current message keeps follow-up prompts compact and bounded.
            return [message.message_id]
        message_ids = (
            task_message_ids
            if task_message_ids is not None
            else self.store.list_task_message_ids(task.id)
        )
        return message_ids or [message.message_id]

    def run(
        self,
        *,
        task: TaskRecord,
        message: NormalizedMessage,
        plan: TaskSessionPromptPlan,
        resources: list[Any],
        run_id: str,
        cwd: str | Path | None = None,
    ) -> TaskSessionRunResult:
        prompt = build_task_session_prompt(
            task=task,
            current_message_id=message.message_id,
            reply_target_message_ids=plan.reply_target_message_ids,
            messages=self.store.get_messages_by_ids(plan.prompt_message_ids),
            resources=resources,
            output_model=plan.output_model,
            context_metadata=task_session_context_metadata(
                session_id=plan.session_id,
                included_message_count=len(plan.prompt_message_ids),
                task_message_count=len(plan.task_message_ids)
                or len(plan.prompt_message_ids),
            ),
            context_access=self.context_access.task_session_context_access(
                message=message, task=task
            ),
        )
        outcome = self.agent_invoker.call_with_retries(
            lambda: self.agent_backend.task_session(
                prompt, session_id=plan.session_id, cwd=cwd
            ),
            run_id=run_id,
            stage="task_session",
            message_id=message.message_id,
            task_id=task.id,
        )
        result = outcome.result
        if result is None or not result.ok or not isinstance(result.json_data, dict):
            return TaskSessionRunResult(
                plan=plan,
                prompt=prompt,
                outcome=outcome,
                result=result,
                output=None,
            )
        try:
            output = plan.output_model.model_validate(result.json_data)
        except ValidationError as exc:
            return TaskSessionRunResult(
                plan=plan,
                prompt=prompt,
                outcome=outcome,
                result=result,
                output=None,
                validation_error=exc,
            )
        return TaskSessionRunResult(
            plan=plan,
            prompt=prompt,
            outcome=outcome,
            result=result,
            output=output,
        )


def reply_target_message_ids(*, task: TaskRecord, current_message_id: str) -> list[str]:
    ids = [current_message_id]
    if task.root_message_id:
        ids.append(task.root_message_id)
    return list(dict.fromkeys(ids))


def task_session_context_metadata(
    *,
    session_id: str | None,
    included_message_count: int,
    task_message_count: int,
) -> dict[str, Any]:
    history_carried = session_id is not None
    return {
        "message_context_mode": "incremental_current_message"
        if history_carried
        else "full_task_messages",
        "included_message_count": included_message_count,
        "task_message_count": task_message_count,
        "history_carried_by_agent_session": history_carried,
    }
