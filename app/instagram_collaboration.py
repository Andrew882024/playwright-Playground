"""Pure collaboration foundation; deliberately unused by production ingestion.

Usernames in this internal model are intentional account metadata, not safe log
output. Never serialize raw media into it. Payload hints cannot confirm acceptance.
"""
from dataclasses import dataclass
import re

_ACCOUNT = re.compile(r'[A-Za-z0-9._]{1,30}\Z', re.ASCII)
_CODE = re.compile(r'[A-Za-z0-9_-]{1,32}\Z', re.ASCII)
_ID = re.compile(r'[0-9]{1,32}\Z', re.ASCII)
MAX_COLLABORATORS = 32


def account_name(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith('@'):
        value = value[1:]
    if not _ACCOUNT.fullmatch(value) or not any(c.isalnum() or c == '_' for c in value):
        return None
    return value.casefold()


def _username(value):
    return account_name(value.get('username')) if isinstance(value, dict) else None


def _shortcode(value):
    return value if isinstance(value, str) and _CODE.fullmatch(value) else None


def _media_id(value):
    if type(value) is int and value > 0:
        value = str(value)
    return value if isinstance(value, str) and _ID.fullmatch(value) and int(value) > 0 else None


@dataclass(frozen=True)
class AcceptedCollaborationEvidence:
    """Trusted caller attestation, NEVER deserialized from Instagram metadata.

    A future reviewed adapter/manual verification must establish accepted (not
    invited) collaboration for this exact post, author and collaborator. No such
    production evidence producer exists yet. Tests use synthetic attestations.
    This type binds evidence identity; it cannot independently verify its truth.
    """
    post_shortcode: str
    primary_author: str
    collaborator: str


@dataclass(frozen=True)
class Collaborator:
    username: str
    status: str  # confirmed | unverified | pending
    evidence: str  # trusted_accepted | coauthor_hint | pending_invitation


@dataclass(frozen=True)
class CollaborationMetadata:
    primary_author: str | None
    requested_profile: str | None
    post_shortcode: str | None
    instagram_media_id: str | None
    collaborators: tuple[Collaborator, ...]
    review_reasons: tuple[str, ...]

    @property
    def review_required(self):
        return bool(self.review_reasons)


def normalize_collaboration(media, requested_profile=None, *, accepted_evidence=()):
    """Normalize only fixed metadata positions, without guessing discovery origin.

    user.username is primary; owner.username is a fallback only if user is absent.
    A malformed/conflicting user is not silently replaced by owner or profile.
    coauthor_producers and invited_coauthors are unverified hints only. Raw
    is_confirmed/accepted/status flags are intentionally ignored.
    """
    reasons = set()
    if not isinstance(media, dict):
        media = {}
        reasons.add('malformed_media')
    requested = account_name(requested_profile)
    if requested_profile is not None and requested is None:
        reasons.add('invalid_requested_profile')
    author = _username(media.get('user')) if 'user' in media else _username(media.get('owner'))
    owner = _username(media.get('owner'))
    if author is None:
        reasons.add('primary_author_missing_or_invalid')
    if 'owner' in media and owner is None:
        reasons.add('invalid_owner')
    if author and owner and author != owner:
        reasons.add('author_owner_conflict')
    code = _shortcode(media.get('code'))
    identity = _media_id(media.get('pk', media.get('id')))
    collaborators = {}
    for field, status, evidence in (
        ('coauthor_producers', 'unverified', 'coauthor_hint'),
        ('invited_coauthors', 'pending', 'pending_invitation'),
    ):
        if field not in media:
            continue
        values = media[field]
        if not isinstance(values, list):
            reasons.add('malformed_collaborators')
            continue
        if len(values) > MAX_COLLABORATORS:
            reasons.add('collaborators_truncated')
        for value in values[:MAX_COLLABORATORS]:
            name = _username(value)
            if name is None:
                reasons.add('invalid_collaborator')
            elif name != author:
                collaborators[name] = Collaborator(name, status, evidence)

    # Separate trusted argument; no payload field can enter this path. Invalid,
    # cross-post or cross-author attestations are rejected, not partially applied.
    if not isinstance(accepted_evidence, (tuple, list)):
        accepted_evidence = ()
        reasons.add('invalid_accepted_evidence')
    if len(accepted_evidence) > MAX_COLLABORATORS:
        reasons.add('accepted_evidence_truncated')
    for item in accepted_evidence[:MAX_COLLABORATORS]:
        if not isinstance(item, AcceptedCollaborationEvidence):
            reasons.add('invalid_accepted_evidence')
            continue
        name = account_name(item.collaborator)
        if (not code or not author or item.post_shortcode != code or
                account_name(item.primary_author) != author or not name or name == author or
                'author_owner_conflict' in reasons):
            reasons.add('accepted_evidence_identity_mismatch')
            continue
        collaborators[name] = Collaborator(name, 'confirmed', 'trusted_accepted')
    if any(item.status != 'confirmed' for item in collaborators.values()):
        reasons.add('collaboration_unverified_or_pending')
    if requested and author and requested != author:
        reasons.add('discovery_profile_differs_from_author')
    return CollaborationMetadata(author, requested, code, identity,
        tuple(collaborators[name] for name in sorted(collaborators)), tuple(sorted(reasons)))


def organizer_presentation(metadata):
    """Return a plain string and separate review state; never HTML or provider ID."""
    names = [metadata.primary_author] if metadata.primary_author else []
    if names:
        names.extend(item.username for item in metadata.collaborators if item.status == 'confirmed')
    return dict(organizer_display=' & '.join('@' + name for name in names),
                review_required=metadata.review_required, review_reasons=metadata.review_reasons)


def stable_post_key(metadata):
    """Cross-profile grouping hint only. Never a replacement for PostgreSQL IDs."""
    return 'instagram:post:' + metadata.post_shortcode if metadata.post_shortcode else None


def compare_post_identity(left, right):
    """Conservative comparison, never a merge/import operation.

    Codes are case-sensitive; numeric IDs corroborate, never guessed/split.
    Conflicting authors or IDs require review even when shortcodes match.
    """
    if not left.post_shortcode or not right.post_shortcode:
        return 'insufficient_identity'
    same_id = (left.instagram_media_id is not None and
               left.instagram_media_id == right.instagram_media_id)
    if left.post_shortcode != right.post_shortcode:
        return 'identity_conflict' if same_id else 'different_posts'
    if (left.instagram_media_id and right.instagram_media_id and not same_id or
            left.primary_author and right.primary_author and left.primary_author != right.primary_author):
        return 'identity_conflict'
    if not left.primary_author or not right.primary_author:
        return 'insufficient_identity'
    return 'same_post_candidate'
