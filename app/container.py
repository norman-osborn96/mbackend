"""Application-wide dependency container (constructed at FastAPI lifespan startup)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from supabase import create_client

from app.core.config import Settings, get_settings
from app.repositories.connected_accounts_repository import ConnectedAccountsRepository
from app.repositories.gmail_message_index_repository import GmailMessageIndexRepository
from app.repositories.sender_repository import SenderRepository
from app.repositories.bucket_repository import BucketRepository
from app.services.bucket_engine import BucketEngine
from app.services.ai_cache import AICache
from app.services.ai_classifier_service import AIClassifierService
from app.services.ai_client import AIClient
from app.services.auth_service import AuthService
from app.services.classification_service import ClassificationService
from app.services.email_service import EmailService
from app.services.followup_service import FollowUpService
from app.services.gmail_sync_service import GmailSyncService
from app.services.insight_service import InsightService

_container: Optional[MailPulseContainer] = None


@dataclass
class MailPulseContainer:
    settings: Settings
    sender_repository: SenderRepository
    connected_accounts_repo: ConnectedAccountsRepository
    gmail_message_index_repo: GmailMessageIndexRepository
    bucket_repo: BucketRepository
    bucket_engine: BucketEngine
    ai_cache: AICache
    ai_client: AIClient
    ai_classifier: AIClassifierService
    classification: ClassificationService
    gmail_sync: GmailSyncService
    email_service: EmailService
    insight_service: InsightService
    followup_service: FollowUpService
    auth_service: AuthService

    @classmethod
    def build(cls, settings: Settings) -> "MailPulseContainer":
        from app.services import gmail_service

        gmail_service.configure_gmail(settings)

        client = create_client(settings.supabase_url, settings.supabase_key.get_secret_value())
        sender_repository = SenderRepository(client)
        connected_accounts_repo = ConnectedAccountsRepository(client)
        gmail_message_index_repo = GmailMessageIndexRepository(client)
        bucket_repo = BucketRepository(client)
        bucket_engine = BucketEngine(bucket_repo)

        ttl_seconds = max(3600, settings.ai_cache_ttl_hours * 3600)
        ai_cache = AICache(ttl_seconds=ttl_seconds, file_path=settings.ai_cache_path)
        ai_client = AIClient(settings)
        ai_classifier = AIClassifierService(settings, ai_cache, ai_client, sender_repository)

        classification = ClassificationService(sender_repository, ai_classifier)
        gmail_sync = GmailSyncService()
        email_service = EmailService(
            classification,
            gmail_sync,
            ai_classifier,
            gmail_message_index_repo,
            bucket_engine,
        )
        insight_service = InsightService(classification)
        followup_service = FollowUpService(classification)
        auth_service = AuthService(settings)

        return cls(
            settings=settings,
            sender_repository=sender_repository,
            connected_accounts_repo=connected_accounts_repo,
            gmail_message_index_repo=gmail_message_index_repo,
            bucket_repo=bucket_repo,
            bucket_engine=bucket_engine,
            ai_cache=ai_cache,
            ai_client=ai_client,
            ai_classifier=ai_classifier,
            classification=classification,
            gmail_sync=gmail_sync,
            email_service=email_service,
            insight_service=insight_service,
            followup_service=followup_service,
            auth_service=auth_service,
        )


def set_container(container: MailPulseContainer) -> None:
    global _container
    _container = container


def get_container() -> MailPulseContainer:
    if _container is None:
        raise RuntimeError("Application container is not initialized")
    return _container
