from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from telethon import errors

from telegram_poll_answer_filter import (
    PollFilterError,
    answer_text,
    create_client,
    fetch_vote_snapshot,
    friendly_rpc_error,
    load_config,
    load_poll_context,
    poll_question,
    print_voter_table,
    resolve_target_request,
    select_exact_answer,
    select_option_answer,
    tighten_session_permissions,
    voters_with_answer_count,
    voters_without_answer,
)

BASE_DIR = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "List people who participated in a non-anonymous Telegram poll "
            "but did not select one exact answer."
        )
    )
    parser.add_argument(
        "--poll-link",
        help="t.me link to the poll message; use together with --answer",
    )
    parser.add_argument(
        "--answer",
        help="exact, case-sensitive answer text; use with --poll-link",
    )
    parser.add_argument(
        "--option",
        help=(
            "t.me poll-option link containing ?option=...; replaces both "
            "--poll-link and --answer"
        ),
    )
    parser.add_argument(
        "--voted-for",
        action="store_true",
        help="add a final column with the answer text selected by each voter",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    target_request = resolve_target_request(
        poll_link=args.poll_link,
        answer=args.answer,
        option_link=args.option,
    )
    config = load_config(BASE_DIR)
    client = create_client(config)

    try:
        await client.start(
            phone=lambda: input(
                "Please enter your phone number (bot tokens are not supported): "
            )
        )
        tighten_session_permissions(config.session_path)
        me = await client.get_me()
        if me is None:
            raise PollFilterError("Could not identify the authorized account.")
        if bool(getattr(me, "bot", False)):
            raise PollFilterError(
                "A user account is required; bot tokens are not supported."
            )

        context = await load_poll_context(
            client,
            target_request.location.chat_ref,
            target_request.location.message_id,
        )
        if target_request.answer_text is not None:
            target_answer = select_exact_answer(
                context.poll, target_request.answer_text
            )
        else:
            assert target_request.option is not None
            target_answer = select_option_answer(context.poll, target_request.option)
        snapshot = await fetch_vote_snapshot(client, context.chat, context.message.id)
        matching_voters = voters_without_answer(snapshot, target_answer.option)
        selected_count = voters_with_answer_count(snapshot, target_answer.option)

        if not bool(getattr(context.poll, "closed", False)):
            print(
                "WARNING: The poll is still open. This is a changing snapshot "
                f"captured at {snapshot.captured_at.isoformat()}."
            )
            print()

        print_voter_table(
            matching_voters,
            poll=context.poll if args.voted_for else None,
        )
        print()
        print(f"Poll: {poll_question(context.poll)}")
        print(f"Excluded answer: {answer_text(target_answer)}")
        print(
            "Poll state: "
            f"{'closed' if bool(getattr(context.poll, 'closed', False)) else 'open'}"
        )
        print(f"Unique voters retrieved: {snapshot.total_voters}")
        print(f"Voters who selected the excluded answer: {selected_count}")
        print(f"Voters who did not select the excluded answer: {len(matching_voters)}")
        return 0
    finally:
        await client.disconnect()
        tighten_session_permissions(config.session_path)


def main() -> int:
    os.umask(0o077)
    args = build_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except PollFilterError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except errors.FloodWaitError as error:
        print(
            "Telegram requested a delay. Do not retry for at least "
            f"{error.seconds} seconds.",
            file=sys.stderr,
        )
        return 3
    except errors.PeerFloodError:
        print(
            "Telegram restricted the account (PeerFlood). Stop and check @SpamBot.",
            file=sys.stderr,
        )
        return 3
    except errors.RPCError as error:
        print(f"Error: {friendly_rpc_error(error)}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"System or network error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Operation cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
