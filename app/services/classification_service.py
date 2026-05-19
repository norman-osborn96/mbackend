"""VIP / medium rules and priority scoring (facade over PriorityEngine)."""

from __future__ import annotations

from typing import List, Optional

from app.core.exceptions import DatabaseException
from app.core.logging import get_logger
from app.repositories.sender_repository import SenderRepository
from app.services.ai_classifier_service import AIClassifierService
from app.services.priority_engine import PriorityEngine, extract_email

log = get_logger("classification_service", service_name="ClassificationService")


class ClassificationService:
    """Sender rules from Supabase plus AI-assisted priority scoring."""

    def __init__(
        self,
        sender_repository: SenderRepository,
        ai_classifier: AIClassifierService,
    ) -> None:
        self._sender_repository = sender_repository
        self._engine = PriorityEngine(sender_repository, ai_classifier)

    def calculate_priority(
        self,
        email: dict,
        *,
        vip_senders: Optional[List[str]] = None,
        medium_senders: Optional[List[str]] = None,
    ) -> dict:
        return self._engine.calculate(
            email,
            vip_senders=vip_senders,
            medium_senders=medium_senders,
        )

    def load_vip_senders(self) -> List[str]:
        try:
            return self._sender_repository.list_vip_emails()
        except DatabaseException as e:
            log.warning("load_vip_senders failed: %s", e)
            return []

    def load_medium_senders(self) -> List[str]:
        try:
            return self._sender_repository.list_medium_emails()
        except DatabaseException as e:
            log.warning("load_medium_senders failed: %s", e)
            return []

    def save_vip_sender(self, sender: str) -> None:
        normalized = extract_email(sender)
        self._sender_repository.upsert_vip_exclusive(normalized)

    def save_medium_sender(self, sender: str) -> None:
        normalized = extract_email(sender)
        self._sender_repository.upsert_medium_exclusive(normalized)

    def remove_vip_sender(self, sender: str) -> None:
        normalized = extract_email(sender)
        self._sender_repository.delete_vip(normalized)

    def remove_medium_sender(self, sender: str) -> None:
        normalized = extract_email(sender)
        self._sender_repository.delete_medium(normalized)
