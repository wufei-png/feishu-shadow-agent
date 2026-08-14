from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from ..agent_backend import AgentBackend
from ..agent_backend_factory import create_agent_backend
from ..config import LoadedConfig
from ..feishu.lark_cli import LarkCliClient
from .artifacts import evals_base_dir
from .capture import CaptureLarkClient, CaptureService
from .ingress_service import IngressEvalService, IngressLarkClient
from .model_service import ModelEvalService
from .promotion import PromotionService

BackendFactory = Callable[[LoadedConfig], AgentBackend]


class EvalService:
    def __init__(
        self,
        *,
        loaded: LoadedConfig,
        lark_client: CaptureLarkClient | IngressLarkClient | None = None,
        backend_factory: BackendFactory | None = None,
    ) -> None:
        self.loaded = loaded
        self.base_dir = evals_base_dir(loaded)
        self.lark_client: CaptureLarkClient | IngressLarkClient = (
            lark_client
            or LarkCliClient(
                path=loaded.config.lark_cli.path,
                timeout_seconds=loaded.config.lark_cli.timeout_seconds,
                cwd=loaded.base_dir,
            )
        )
        self.backend_factory: BackendFactory = (
            backend_factory or _default_backend_factory
        )
        self.capture_service = CaptureService(
            loaded=loaded,
            lark_client=cast(CaptureLarkClient, self.lark_client),
        )
        self.ingress_service = IngressEvalService(
            loaded=loaded,
            lark_client=cast(IngressLarkClient, self.lark_client),
            backend_factory=self.backend_factory,
        )
        self.model_service = ModelEvalService(
            loaded=loaded, backend_factory=self.backend_factory
        )
        self.promotion_service = PromotionService(loaded=loaded, base_dir=self.base_dir)

    def capture_candidates(
        self, *, lookback_days: int, limit: int
    ) -> list[dict[str, Any]]:
        return self.capture_service.candidates(lookback_days=lookback_days, limit=limit)

    def capture_case(
        self,
        *,
        message_id: str,
        context_before: int,
        context_after: int,
        lookback_days: int,
        label: str | None,
        allow_sensitive_config: bool,
    ) -> Path:
        return self.capture_service.capture(
            message_id=message_id,
            context_before=context_before,
            context_after=context_after,
            lookback_days=lookback_days,
            label=label,
            allow_sensitive_config=allow_sensitive_config,
        )

    def run_ingress(
        self,
        *,
        chat_id: str | None,
        snapshot: Path | None,
        start: str | None,
        end: str | None,
        lookback_days: int | None,
        label: str | None,
        dry_run_backend: bool,
        allow_sensitive_config: bool,
    ) -> Path:
        return self.ingress_service.run(
            chat_id=chat_id,
            snapshot=snapshot,
            start=start,
            end=end,
            lookback_days=lookback_days,
            label=label,
            dry_run_backend=dry_run_backend,
            allow_sensitive_config=allow_sensitive_config,
        )

    def run_ingress_golden(
        self,
        *,
        case_dir: Path,
        label: str | None,
        allow_sensitive_config: bool = False,
    ) -> tuple[Path, int]:
        return self.ingress_service.run_golden(
            case_dir=case_dir,
            label=label,
            allow_sensitive_config=allow_sensitive_config,
        )

    def run_router(
        self,
        *,
        case_dir: Path,
        label: str | None,
        dry_run_backend: bool,
        repeat: int = 1,
        allow_sensitive_config: bool = False,
    ) -> tuple[Path, int]:
        return self.model_service.run_case(
            eval_type="router",
            case_dir=case_dir,
            label=label,
            dry_run_backend=dry_run_backend,
            repeat=repeat,
            allow_sensitive_config=allow_sensitive_config,
        )

    def run_router_cases(
        self,
        *,
        cases_dir: Path,
        label: str | None,
        dry_run_backend: bool,
        repeat: int = 1,
        allow_sensitive_config: bool = False,
    ) -> tuple[Path, int]:
        return self.model_service.run_cases(
            eval_type="router",
            cases_dir=cases_dir,
            label=label,
            dry_run_backend=dry_run_backend,
            repeat=repeat,
            allow_sensitive_config=allow_sensitive_config,
        )

    def run_task_session(
        self,
        *,
        case_dir: Path,
        label: str | None,
        dry_run_backend: bool,
        repeat: int = 1,
        allow_sensitive_config: bool = False,
    ) -> tuple[Path, int]:
        return self.model_service.run_case(
            eval_type="task-session",
            case_dir=case_dir,
            label=label,
            dry_run_backend=dry_run_backend,
            repeat=repeat,
            allow_sensitive_config=allow_sensitive_config,
        )

    def run_task_session_cases(
        self,
        *,
        cases_dir: Path,
        label: str | None,
        dry_run_backend: bool,
        repeat: int = 1,
        allow_sensitive_config: bool = False,
    ) -> tuple[Path, int]:
        return self.model_service.run_cases(
            eval_type="task-session",
            cases_dir=cases_dir,
            label=label,
            dry_run_backend=dry_run_backend,
            repeat=repeat,
            allow_sensitive_config=allow_sensitive_config,
        )

    def run_full_chain(
        self,
        *,
        case_dir: Path,
        label: str | None,
        dry_run_backend: bool,
        repeat: int = 1,
        allow_sensitive_config: bool = False,
    ) -> tuple[Path, int]:
        return self.model_service.run_case(
            eval_type="full-chain",
            case_dir=case_dir,
            label=label,
            dry_run_backend=dry_run_backend,
            repeat=repeat,
            allow_sensitive_config=allow_sensitive_config,
        )

    def run_full_chain_cases(
        self,
        *,
        cases_dir: Path,
        label: str | None,
        dry_run_backend: bool,
        repeat: int = 1,
        allow_sensitive_config: bool = False,
    ) -> tuple[Path, int]:
        return self.model_service.run_cases(
            eval_type="full-chain",
            cases_dir=cases_dir,
            label=label,
            dry_run_backend=dry_run_backend,
            repeat=repeat,
            allow_sensitive_config=allow_sensitive_config,
        )

    def promote(
        self,
        *,
        eval_type: str,
        run_dir: Path | None,
        case_dir: Path | None,
        review_path: Path,
        name: str,
        allow_sensitive_config: bool = False,
    ) -> Path:
        return self.promotion_service.promote(
            eval_type=eval_type,
            run_dir=run_dir,
            case_dir=case_dir,
            review_path=review_path,
            name=name,
            allow_sensitive_config=allow_sensitive_config,
        )


def _default_backend_factory(loaded: LoadedConfig) -> AgentBackend:
    return create_agent_backend(loaded.config, base_dir=loaded.base_dir)
