from __future__ import annotations

import base64
import binascii
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from dotenv import load_dotenv
from telethon import TelegramClient, errors, functions, types, utils

DEFAULT_SESSION_NAME = "telegram_poll_answer_filter"


class PollFilterError(RuntimeError):
    """An expected error that can be shown to the operator."""


@dataclass(frozen=True)
class AppConfig:
    api_id: int
    api_hash: str
    session_path: Path


@dataclass(frozen=True)
class PollLocation:
    chat_ref: int | str
    message_id: int


@dataclass(frozen=True)
class PollOptionLocation:
    location: PollLocation
    option: bytes


@dataclass(frozen=True)
class TargetRequest:
    location: PollLocation
    answer_text: str | None = None
    option: bytes | None = None


@dataclass(frozen=True)
class PollContext:
    chat: Any
    message: Any

    @property
    def poll(self) -> Any:
        return self.message.media.poll


@dataclass(frozen=True)
class VoterRecord:
    id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    selected_options: frozenset[bytes]
    used_input_option: bool = False


@dataclass(frozen=True)
class VoteSnapshot:
    voters: tuple[VoterRecord, ...]
    captured_at: datetime

    @property
    def total_voters(self) -> int:
        return len(self.voters)


def load_config(base_dir: Path) -> AppConfig:
    load_dotenv(base_dir / ".env", override=False)

    raw_api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    raw_session = os.getenv("TELEGRAM_SESSION", DEFAULT_SESSION_NAME).strip()

    if not raw_api_id or not api_hash:
        raise PollFilterError(
            "Set TELEGRAM_API_ID and TELEGRAM_API_HASH in the .env file."
        )

    try:
        api_id = int(raw_api_id)
    except ValueError as error:
        raise PollFilterError("TELEGRAM_API_ID must be an integer.") from error
    if api_id <= 0:
        raise PollFilterError("TELEGRAM_API_ID must be a positive integer.")

    session_value = raw_session or DEFAULT_SESSION_NAME
    session_path = Path(session_value).expanduser()
    if not session_path.is_absolute():
        session_path = base_dir / session_path

    return AppConfig(api_id=api_id, api_hash=api_hash, session_path=session_path)


def create_client(config: AppConfig) -> TelegramClient:
    return TelegramClient(
        str(config.session_path),
        config.api_id,
        config.api_hash,
        flood_sleep_threshold=0,
        receive_updates=False,
    )


def session_file_path(session_path: Path) -> Path:
    if session_path.suffix == ".session":
        return session_path
    return Path(f"{session_path}.session")


def tighten_session_permissions(session_path: Path) -> None:
    primary_path = session_file_path(session_path)
    for candidate in (
        primary_path,
        Path(f"{primary_path}-journal"),
        Path(f"{primary_path}-wal"),
        Path(f"{primary_path}-shm"),
    ):
        if candidate.exists():
            candidate.chmod(0o600)


def parse_poll_link(link: str) -> PollLocation:
    parsed = urlparse(link.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in {
        "t.me",
        "www.t.me",
        "telegram.me",
        "www.telegram.me",
    }:
        raise PollFilterError(
            "Expected a poll message link in the form https://t.me/...."
        )

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if parts and parts[0] == "s":
        parts = parts[1:]
    if len(parts) < 2:
        raise PollFilterError("The link does not contain a message ID.")

    try:
        message_id = int(parts[-1])
    except ValueError as error:
        raise PollFilterError(
            "The final part of the link must be a message ID."
        ) from error
    if message_id <= 0:
        raise PollFilterError("The message ID must be a positive integer.")

    if parts[0] == "c":
        if len(parts) < 3 or not parts[1].isdigit():
            raise PollFilterError("Invalid private group message link.")
        chat_ref: int | str = int(f"-100{parts[1]}")
    else:
        username = parts[0].lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{4,}", username):
            raise PollFilterError("The link does not contain a valid group username.")
        chat_ref = f"@{username}"

    return PollLocation(chat_ref=chat_ref, message_id=message_id)


def parse_option_link(link: str) -> PollOptionLocation:
    location = parse_poll_link(link)
    parsed = urlparse(link.strip())
    values = parse_qs(parsed.query, keep_blank_values=True).get("option", [])
    if len(values) != 1 or not values[0]:
        raise PollFilterError(
            "The --option link must contain exactly one non-empty option parameter."
        )

    try:
        encoded_option = values[0].encode("ascii")
    except UnicodeEncodeError as error:
        raise PollFilterError(
            "The option parameter must be a base64url value."
        ) from error

    padding = b"=" * (-len(encoded_option) % 4)
    try:
        option = base64.b64decode(
            encoded_option + padding,
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as error:
        raise PollFilterError("The option parameter is not valid base64url.") from error
    if not option:
        raise PollFilterError("The decoded poll option must not be empty.")
    return PollOptionLocation(location=location, option=option)


def resolve_target_request(
    *, poll_link: str | None, answer: str | None, option_link: str | None
) -> TargetRequest:
    if option_link is not None:
        if poll_link is not None or answer is not None:
            raise PollFilterError(
                "Use either --option by itself or --poll-link together with --answer."
            )
        parsed_option = parse_option_link(option_link)
        return TargetRequest(
            location=parsed_option.location,
            option=parsed_option.option,
        )

    if poll_link is None or answer is None:
        raise PollFilterError(
            "Provide --option, or provide both --poll-link and --answer."
        )
    return TargetRequest(location=parse_poll_link(poll_link), answer_text=answer)


async def resolve_chat(client: TelegramClient, chat_ref: int | str) -> Any:
    try:
        return await client.get_entity(chat_ref)
    except (TypeError, ValueError) as original_error:
        if isinstance(chat_ref, int):
            async for dialog in client.iter_dialogs():
                if utils.get_peer_id(dialog.entity) == chat_ref:
                    return dialog.entity
        raise PollFilterError(
            "Could not find the group. Make sure the account is a member and "
            "the link is correct."
        ) from original_error


def ensure_supported_group(chat: Any) -> None:
    if isinstance(chat, types.Chat):
        if getattr(chat, "deactivated", False):
            raise PollFilterError("The group has been deactivated.")
        return
    if (
        isinstance(chat, types.Channel)
        and bool(getattr(chat, "megagroup", False))
        and not bool(getattr(chat, "broadcast", False))
    ):
        return
    raise PollFilterError(
        "Only basic groups and supergroups are supported; channels are not."
    )


async def load_poll_context(
    client: TelegramClient, chat_ref: int | str, message_id: int
) -> PollContext:
    chat = await resolve_chat(client, chat_ref)
    ensure_supported_group(chat)

    message = await client.get_messages(chat, ids=message_id)
    if message is None:
        raise PollFilterError("No message was found with the specified ID.")
    if not isinstance(getattr(message, "media", None), types.MessageMediaPoll):
        raise PollFilterError("The specified message does not contain a poll.")

    poll = message.media.poll
    if not bool(getattr(poll, "public_voters", False)):
        raise PollFilterError(
            "The poll is anonymous, so Telegram does not expose voter identities."
        )
    return PollContext(chat=chat, message=message)


def text_value(value: Any) -> str:
    return str(getattr(value, "text", value))


def poll_question(poll: Any) -> str:
    return text_value(getattr(poll, "question", ""))


def answer_text(answer: Any) -> str:
    return text_value(getattr(answer, "text", ""))


def select_exact_answer(poll: Any, requested_text: str) -> Any:
    answers = list(getattr(poll, "answers", ()))
    matches = [answer for answer in answers if answer_text(answer) == requested_text]
    if len(matches) == 1:
        return matches[0]

    available = "\n".join(
        f"  {index}. {answer_text(answer)}"
        for index, answer in enumerate(answers, start=1)
    )
    if not available:
        available = "  (none)"
    if not matches:
        detail = "No poll answer exactly matches the supplied text."
    else:
        detail = "More than one poll answer exactly matches the supplied text."
    raise PollFilterError(f"{detail}\nAvailable answers:\n{available}")


def select_option_answer(poll: Any, requested_option: bytes) -> Any:
    matches = [
        answer
        for answer in getattr(poll, "answers", ())
        if bytes(answer.option) == bytes(requested_option)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise PollFilterError(
            "The option from the link does not exist in the referenced poll."
        )
    raise PollFilterError(
        "The poll contains the same option identifier more than once."
    )


def vote_options(vote: Any) -> frozenset[bytes]:
    if isinstance(vote, types.MessagePeerVote):
        return frozenset((bytes(vote.option),))
    if isinstance(vote, types.MessagePeerVoteMultiple):
        return frozenset(bytes(option) for option in vote.options)
    if isinstance(vote, types.MessagePeerVoteInputOption):
        return frozenset()
    raise PollFilterError(
        f"Telegram returned an unsupported vote format: {type(vote).__name__}."
    )


def _user_record(
    user: Any,
    selected_options: frozenset[bytes],
    *,
    used_input_option: bool,
) -> VoterRecord:
    return VoterRecord(
        id=int(user.id),
        username=getattr(user, "username", None),
        first_name=getattr(user, "first_name", None),
        last_name=getattr(user, "last_name", None),
        selected_options=selected_options,
        used_input_option=used_input_option,
    )


async def fetch_vote_snapshot(
    client: TelegramClient, chat: Any, message_id: int
) -> VoteSnapshot:
    selected_options_by_user: dict[int, frozenset[bytes]] = {}
    input_option_user_ids: set[int] = set()
    users_by_id: dict[int, Any] = {}
    offset: str | None = None
    seen_offsets: set[str] = set()
    expected_count: int | None = None

    while True:
        result = await client(
            functions.messages.GetPollVotesRequest(
                peer=chat,
                id=message_id,
                limit=100,
                option=None,
                offset=offset,
            )
        )
        page_count = int(result.count)
        if expected_count is None:
            expected_count = page_count
        elif page_count != expected_count:
            raise PollFilterError(
                "The voter count changed while the snapshot was being read. "
                "Run the command again."
            )

        for user in result.users:
            users_by_id[int(user.id)] = user

        for vote in result.votes:
            peer = getattr(vote, "peer", None)
            if not isinstance(peer, types.PeerUser):
                raise PollFilterError(
                    "A vote submitted as a channel or another chat was found. "
                    "It cannot be safely associated with a person."
                )
            user_id = int(peer.user_id)
            if user_id in selected_options_by_user:
                raise PollFilterError(
                    "Telegram returned the same voter more than once. "
                    "The snapshot may have changed; run the command again."
                )
            selected_options_by_user[user_id] = vote_options(vote)
            if isinstance(vote, types.MessagePeerVoteInputOption):
                input_option_user_ids.add(user_id)

        next_offset = getattr(result, "next_offset", None) or ""
        if not next_offset:
            break
        if next_offset in seen_offsets:
            raise PollFilterError("Telegram returned a repeated vote offset.")
        seen_offsets.add(next_offset)
        offset = next_offset

    expected_count = expected_count or 0
    if len(selected_options_by_user) != expected_count:
        raise PollFilterError(
            "Incomplete voter list: received "
            f"{len(selected_options_by_user)} of {expected_count}."
        )

    missing_user_ids = sorted(set(selected_options_by_user) - set(users_by_id))
    if missing_user_ids:
        preview = ", ".join(str(user_id) for user_id in missing_user_ids[:10])
        suffix = "..." if len(missing_user_ids) > 10 else ""
        raise PollFilterError(
            f"Telegram did not provide profile data for voter IDs: {preview}{suffix}."
        )

    voters = tuple(
        _user_record(
            users_by_id[user_id],
            selected_options_by_user[user_id],
            used_input_option=user_id in input_option_user_ids,
        )
        for user_id in sorted(selected_options_by_user)
    )
    return VoteSnapshot(voters=voters, captured_at=datetime.now(timezone.utc))


def voters_without_answer(
    snapshot: VoteSnapshot, target_option: bytes
) -> tuple[VoterRecord, ...]:
    return tuple(
        voter
        for voter in snapshot.voters
        if bytes(target_option) not in voter.selected_options
    )


def voters_with_answer_count(snapshot: VoteSnapshot, target_option: bytes) -> int:
    option = bytes(target_option)
    return sum(option in voter.selected_options for voter in snapshot.voters)


def voter_answer_text(voter: VoterRecord, poll: Any) -> str:
    answers = list(getattr(poll, "answers", ()))
    known_options = {bytes(answer.option) for answer in answers}
    unknown_options = voter.selected_options - known_options
    if unknown_options:
        raise PollFilterError(
            f"Poll answer text is unavailable for voter ID {voter.id}. "
            "The poll may have changed; run the command again."
        )

    selected_texts = [
        answer_text(answer)
        for answer in answers
        if bytes(answer.option) in voter.selected_options
    ]
    if voter.used_input_option:
        selected_texts.append("[free-text answer; text unavailable]")
    return " | ".join(selected_texts) or "[no answer text available]"


def _display_text(value: Any, *, limit: int | None = 32) -> str:
    if value is None:
        return ""
    normalized = " ".join(str(value).replace("\t", " ").splitlines())
    if limit is not None and len(normalized) > limit:
        return f"{normalized[: limit - 1]}…"
    return normalized


def print_voter_table(
    voters: Sequence[VoterRecord], *, poll: Any | None = None
) -> None:
    headers = ["ID", "username", "first name", "last name"]
    if poll is not None:
        headers.append("voted for")

    rows: list[tuple[str, ...]] = []
    for voter in voters:
        row = [
            str(voter.id),
            _display_text(voter.username),
            _display_text(voter.first_name),
            _display_text(voter.last_name),
        ]
        if poll is not None:
            row.append(_display_text(voter_answer_text(voter, poll), limit=None))
        rows.append(tuple(row))

    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render(row: Sequence[str]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))

    print(render(tuple(headers)))
    print(render(tuple("-" * width for width in widths)))
    for row in rows:
        print(render(row))


def friendly_rpc_error(error: errors.RPCError) -> str:
    messages: Mapping[str, str] = {
        "PollVoteRequiredError": (
            "Telegram did not allow voter retrieval: this account must vote "
            "in the poll first. The command does not vote automatically."
        ),
        "BroadcastForbiddenError": (
            "Telegram does not allow voter retrieval for a broadcast channel."
        ),
        "MessageIdInvalidError": "Telegram did not find a message with that ID.",
        "ChannelPrivateError": (
            "The group is unavailable to the account, or the account is no "
            "longer a member."
        ),
    }
    error_name = type(error).__name__
    return messages.get(error_name, f"Telegram error {error_name}: {error}")
