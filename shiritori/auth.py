"""Password and opaque-session authentication backed by SQLAlchemy.

Passwords use Argon2id.  Browser session tokens are returned only once; the
database stores a SHA-256 digest, so a database read cannot reveal a usable
cookie value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import unicodedata

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .database import Database
from .models import LoginSession, User, utc_now


GENERIC_LOGIN_ERROR = "ユーザー名またはパスワードが正しくありません。"
GENERIC_SESSION_ERROR = "ログインセッションが無効です。"

_USERNAME_MIN_LENGTH = 3
_USERNAME_MAX_LENGTH = 32
_USERNAME_KEY_MAX_LENGTH = 64
_PASSWORD_MIN_LENGTH = 10
_PASSWORD_MAX_LENGTH = 128
_DEFAULT_SESSION_TTL = timedelta(days=14)


class AuthenticationError(RuntimeError):
    """Base class for authentication failures safe to handle at the UI edge."""


class InvalidCredentialsError(AuthenticationError):
    """A deliberately generic username/password failure."""

    def __init__(self) -> None:
        super().__init__(GENERIC_LOGIN_ERROR)


class InvalidSessionError(AuthenticationError):
    """A deliberately generic absent, expired, or revoked session."""

    def __init__(self) -> None:
        super().__init__(GENERIC_SESSION_ERROR)


class UsernameUnavailableError(AuthenticationError):
    """Raised when registration cannot claim the requested username."""


class InvalidRegistrationError(ValueError):
    """Raised for malformed registration fields."""


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    username: str
    display_name: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A newly issued token; ``token`` must be placed in a secure cookie."""

    account: Account
    session_id: str
    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    account: Account
    session_id: str
    expires_at: datetime


def canonicalize_username(username: str) -> tuple[str, str]:
    """Return NFKC display spelling and its stripped, case-folded key."""

    normalized = unicodedata.normalize("NFKC", str(username)).strip()
    return normalized, normalized.casefold()

def normalize_username(username: str) -> tuple[str, str]:
    """Validate and return display spelling and its uniqueness key."""

    normalized, username_key = canonicalize_username(username)
    if not (_USERNAME_MIN_LENGTH <= len(normalized) <= _USERNAME_MAX_LENGTH):
        raise InvalidRegistrationError(
            f"ユーザー名は{_USERNAME_MIN_LENGTH}〜{_USERNAME_MAX_LENGTH}文字にしてください。"
        )
    if normalized[0] in "._-" or normalized[-1] in "._-":
        raise InvalidRegistrationError(
            "ユーザー名の先頭と末尾に記号は使えません。"
        )
    for character in normalized:
        if character in "._-":
            continue
        category = unicodedata.category(character)
        if not category.startswith(("L", "N")):
            raise InvalidRegistrationError(
                "ユーザー名には文字、数字、ピリオド、ハイフン、下線だけを使えます。"
            )
    # Unicode case-folding can expand a string (for example, one code point
    # can become several combining code points). Check the actual database
    # key length before issuing a query or insert against VARCHAR(64).
    if len(username_key) > _USERNAME_KEY_MAX_LENGTH:
        raise InvalidRegistrationError(
            "ユーザー名を正規化した結果が長すぎます。"
        )
    return normalized, username_key


def validate_password(password: str) -> str:
    value = str(password)
    if not (_PASSWORD_MIN_LENGTH <= len(value) <= _PASSWORD_MAX_LENGTH):
        raise InvalidRegistrationError(
            f"パスワードは{_PASSWORD_MIN_LENGTH}〜{_PASSWORD_MAX_LENGTH}文字にしてください。"
        )
    return value


def session_token_hash(token: str) -> str:
    """Return the only representation of a session token stored in the DB."""

    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _aware_utc(value: datetime) -> datetime:
    # SQLite does not preserve timezone information even for timezone=True.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _account(user: User) -> Account:
    return Account(
        id=user.id,
        username=user.username,
        display_name=user.display_name or user.username,
        created_at=_aware_utc(user.created_at),
    )


class AuthService:
    """Transactional account and session operations."""

    def __init__(
        self,
        database: Database,
        *,
        password_hasher: PasswordHasher | None = None,
        session_ttl: timedelta = _DEFAULT_SESSION_TTL,
    ) -> None:
        if session_ttl <= timedelta(0):
            raise ValueError("session_ttl must be positive")
        self.database = database
        self.password_hasher = password_hasher or PasswordHasher()
        self.session_ttl = session_ttl
        # An absent username still performs one Argon2 verification.  Generate
        # this with the configured hasher so its cost matches real hashes.
        self._dummy_password = secrets.token_urlsafe(24)
        self._dummy_hash = self.password_hasher.hash(self._dummy_password)

    def register(
        self,
        username: str,
        password: str,
        *,
        display_name: str | None = None,
    ) -> Account:
        clean_username, username_key = normalize_username(username)
        clean_password = validate_password(password)
        clean_display_name = (
            unicodedata.normalize("NFKC", display_name).strip()
            if display_name is not None
            else clean_username
        )
        if not clean_display_name or len(clean_display_name) > 40:
            raise InvalidRegistrationError(
                "表示名は1〜40文字にしてください。"
            )
        password_hash = self.password_hasher.hash(clean_password)
        if not password_hash.startswith("$argon2id$"):
            raise RuntimeError("password hasher must produce Argon2id hashes")

        try:
            with self.database.transaction() as session:
                user = User(
                    username=clean_username,
                    username_key=username_key,
                    display_name=clean_display_name,
                    password_hash=password_hash,
                )
                session.add(user)
                session.flush()
                return _account(user)
        except IntegrityError as error:
            # The unique index is the final authority under concurrent signup.
            with self.database.read_session() as session:
                already_exists = session.scalar(
                    select(User.id).where(User.username_key == username_key)
                )
            if already_exists is not None:
                raise UsernameUnavailableError(
                    "そのユーザー名は使用できません。"
                ) from error
            raise

    def login(
        self,
        username: str,
        password: str,
        *,
        now: datetime | None = None,
        ttl: timedelta | None = None,
    ) -> IssuedSession:
        issued_at = _aware_utc(now) if now is not None else utc_now()
        requested_ttl = self.session_ttl if ttl is None else ttl
        if requested_ttl <= timedelta(0):
            raise ValueError("session TTL must be positive")

        invalid_shape = False
        try:
            _, username_key = normalize_username(username)
        except InvalidRegistrationError:
            # Still query and verify the dummy hash to keep the outward failure
            # path the same as a well-formed but unknown username.
            username_key = f"!invalid:{hashlib.sha256(str(username).encode()).hexdigest()}"
            invalid_shape = True

        candidate_password = str(password)
        if not (
            _PASSWORD_MIN_LENGTH
            <= len(candidate_password)
            <= _PASSWORD_MAX_LENGTH
        ):
            candidate_for_verification = self._dummy_password
            invalid_shape = True
        else:
            candidate_for_verification = candidate_password

        with self.database.transaction() as session:
            user = session.scalar(
                select(User)
                .where(User.username_key == username_key)
                .with_for_update()
            )
            stored_hash = user.password_hash if user is not None else self._dummy_hash
            verified = False
            try:
                verified = self.password_hasher.verify(
                    stored_hash, candidate_for_verification
                )
            except (VerificationError, InvalidHashError):
                verified = False

            if (
                invalid_shape
                or user is None
                or user.disabled_at is not None
                or not verified
            ):
                raise InvalidCredentialsError()

            if self.password_hasher.check_needs_rehash(user.password_hash):
                user.password_hash = self.password_hasher.hash(candidate_password)
                user.updated_at = issued_at

            return self._issue_session(
                session,
                user,
                issued_at=issued_at,
                expires_at=issued_at + requested_ttl,
            )

    def issue_session(
        self,
        user_id: str,
        *,
        now: datetime | None = None,
        ttl: timedelta | None = None,
    ) -> IssuedSession:
        """Issue another session after an already-authenticated operation."""

        issued_at = _aware_utc(now) if now is not None else utc_now()
        requested_ttl = self.session_ttl if ttl is None else ttl
        if requested_ttl <= timedelta(0):
            raise ValueError("session TTL must be positive")
        with self.database.transaction() as session:
            user = session.scalar(
                select(User).where(User.id == user_id).with_for_update()
            )
            if user is None or user.disabled_at is not None:
                raise InvalidSessionError()
            return self._issue_session(
                session,
                user,
                issued_at=issued_at,
                expires_at=issued_at + requested_ttl,
            )

    @staticmethod
    def _issue_session(
        session: Session,
        user: User,
        *,
        issued_at: datetime,
        expires_at: datetime,
    ) -> IssuedSession:
        # 32 random bytes provide 256 bits of entropy before URL-safe encoding.
        token = "srt_" + secrets.token_urlsafe(32)
        login_session = LoginSession(
            user_id=user.id,
            token_hash=session_token_hash(token),
            created_at=issued_at,
            last_seen_at=issued_at,
            expires_at=expires_at,
        )
        session.add(login_session)
        session.flush()
        return IssuedSession(
            account=_account(user),
            session_id=login_session.id,
            token=token,
            expires_at=expires_at,
        )

    def authenticate_session(
        self, token: str, *, now: datetime | None = None
    ) -> SessionPrincipal:
        checked_at = _aware_utc(now) if now is not None else utc_now()
        token_digest = session_token_hash(token)
        principal: SessionPrincipal | None = None

        with self.database.transaction() as session:
            login_session = session.scalar(
                select(LoginSession).where(
                    LoginSession.token_hash == token_digest
                )
            )
            if login_session is not None:
                user = session.get(User, login_session.user_id)
                expired = _aware_utc(login_session.expires_at) <= _aware_utc(
                    checked_at
                )
                invalid = (
                    login_session.revoked_at is not None
                    or expired
                    or user is None
                    or user.disabled_at is not None
                )
                if invalid:
                    if login_session.revoked_at is None:
                        login_session.revoked_at = checked_at
                else:
                    login_session.last_seen_at = checked_at
                    principal = SessionPrincipal(
                        account=_account(user),
                        session_id=login_session.id,
                        expires_at=_aware_utc(login_session.expires_at),
                    )

        if principal is None:
            raise InvalidSessionError()
        return principal

    def logout(self, token: str, *, now: datetime | None = None) -> bool:
        """Revoke a token.  Repeating logout is harmless and returns ``False``."""

        revoked_at = _aware_utc(now) if now is not None else utc_now()
        with self.database.transaction() as session:
            login_session = session.scalar(
                select(LoginSession).where(
                    LoginSession.token_hash == session_token_hash(token)
                )
            )
            if login_session is None or login_session.revoked_at is not None:
                return False
            login_session.revoked_at = revoked_at
            return True

    def revoke_user_sessions(
        self,
        user_id: str,
        *,
        now: datetime | None = None,
        except_session_id: str | None = None,
    ) -> int:
        revoked_at = _aware_utc(now) if now is not None else utc_now()
        statement = update(LoginSession).where(
            LoginSession.user_id == user_id,
            LoginSession.revoked_at.is_(None),
        )
        if except_session_id is not None:
            statement = statement.where(LoginSession.id != except_session_id)
        with self.database.transaction() as session:
            # Serialize with login/issue_session and password changes, all of
            # which lock the same User row before creating or revoking tokens.
            user_id_lock = session.scalar(
                select(User.id).where(User.id == user_id).with_for_update()
            )
            if user_id_lock is None:
                return 0
            result = session.execute(statement.values(revoked_at=revoked_at))
            return int(result.rowcount or 0)

    def change_password(
        self,
        user_id: str,
        current_password: str,
        new_password: str,
        *,
        now: datetime | None = None,
    ) -> None:
        changed_at = _aware_utc(now) if now is not None else utc_now()
        validated_new_password = validate_password(new_password)
        with self.database.transaction() as session:
            user = session.scalar(
                select(User).where(User.id == user_id).with_for_update()
            )
            if user is None or user.disabled_at is not None:
                # Preserve the same public error as an incorrect old password.
                try:
                    self.password_hasher.verify(
                        self._dummy_hash, self._dummy_password
                    )
                except VerificationError:
                    pass
                raise InvalidCredentialsError()
            try:
                verified = self.password_hasher.verify(
                    user.password_hash, str(current_password)
                )
            except (VerificationError, InvalidHashError):
                verified = False
            if not verified:
                raise InvalidCredentialsError()

            user.password_hash = self.password_hasher.hash(validated_new_password)
            user.updated_at = changed_at
            session.execute(
                update(LoginSession)
                .where(
                    LoginSession.user_id == user_id,
                    LoginSession.revoked_at.is_(None),
                )
                .values(revoked_at=changed_at)
            )


__all__ = [
    "Account",
    "AuthService",
    "AuthenticationError",
    "GENERIC_LOGIN_ERROR",
    "GENERIC_SESSION_ERROR",
    "InvalidCredentialsError",
    "InvalidRegistrationError",
    "InvalidSessionError",
    "IssuedSession",
    "SessionPrincipal",
    "UsernameUnavailableError",
    "canonicalize_username",
    "normalize_username",
    "session_token_hash",
    "validate_password",
]
