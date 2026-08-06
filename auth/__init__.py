"""Authentication primitives used by ATS adapters.

The package deliberately contains interfaces and local implementations only.
Credentials and mailbox contents belong in the user's private runtime home,
never in the source tree.
"""

from .credentials import (
    CredentialStore,
    CredentialStoreError,
    InMemoryCredentialStore,
    MacOSSecurityCredentialStore,
)
from .mailbox import (
    CorrelatedMailboxVerifier,
    MailAuthenticationEvidence,
    MailAuthenticationResult,
    MailboxMessage,
    MailboxProvider,
    MailboxVerifier,
    MailboxVerificationStatus,
    VerificationArtifact,
    VerificationArtifactKind,
    VerificationRequest,
    VerificationResult,
)
from .imap_provider import (
    IMAPMailboxProvider,
    IMAPProviderConfig,
    IMAPProviderDisabled,
    IMAPProviderError,
)
from .workday_hosts import WORKDAY_HOST_SUFFIXES, is_trusted_workday_host

__all__ = [
    "CorrelatedMailboxVerifier",
    "CredentialStore",
    "CredentialStoreError",
    "InMemoryCredentialStore",
    "IMAPMailboxProvider",
    "IMAPProviderConfig",
    "IMAPProviderDisabled",
    "IMAPProviderError",
    "MacOSSecurityCredentialStore",
    "MailAuthenticationEvidence",
    "MailAuthenticationResult",
    "MailboxMessage",
    "MailboxProvider",
    "MailboxVerifier",
    "MailboxVerificationStatus",
    "VerificationArtifact",
    "VerificationArtifactKind",
    "VerificationRequest",
    "VerificationResult",
    "WORKDAY_HOST_SUFFIXES",
    "is_trusted_workday_host",
]
