> [!WARNING]
> This project has moved to
> [ihoru/telegram-automations](https://github.com/ihoru/telegram-automations).
> This repository is archived and no longer maintained here.

English | [Русский](README.ru.md)

# Filter Telegram poll participants by answer

A local, read-only Python and Telethon utility. It accepts either a Telegram
poll link plus exact answer text, or a direct Telegram poll-option link, then
prints the people who participated in the poll but did not select that answer.

The utility does not write to Telegram, remove anyone, or save the people list
to a file.

## Requirements and limitations

- Python 3.12.
- A Telegram user account is required; bot tokens are not supported.
- The account must be able to access the group and must vote in the poll first.
  Otherwise Telegram returns `POLL_VOTE_REQUIRED`.
- Basic groups and supergroups are supported. Broadcast channels are not.
- The poll must be non-anonymous because Telegram does not expose voter
  identities for anonymous polls.
- Open polls are allowed, but their output is only a point-in-time snapshot. If
  the voter count changes while the snapshot is being read, the command aborts
  and asks you to run it again.
- The result includes every voter returned by Telegram, including people who
  have left the group. The current account and administrators are not excluded.
- Votes submitted as channels are rejected because they cannot be safely
  associated with a person.

For a multiple-choice poll, a person is excluded from the output when any of
their selected options matches the target answer. A person who submitted only a
free-text answer participated but did not select the target predefined answer.

## Installation

```bash
cd /home/ihoru/tmp/telegram-poll-answer-filter
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Create the local configuration:

```bash
cp .env.example .env
chmod 600 .env
```

```dotenv
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
TELEGRAM_SESSION=telegram_poll_answer_filter
```

Create `api_id` and `api_hash` in **API development tools** at
[my.telegram.org](https://my.telegram.org). Never publish or share the
`api_hash`, login codes, 2FA password, or generated `*.session` file.

On the first run, Telethon asks for the phone number, Telegram login code, and
2FA password when enabled. Authorization is then stored in the local session
file.

## Usage

### Select an answer by text

```bash
python list_without_answer.py \
  --poll-link "https://t.me/c/1234567890/42" \
  --answer "Exact answer text"
```

Answer text matching is exact and case-sensitive. If there is no match or more
than one answer has the same text, the command displays every available answer
and exits without a result.

### Select an answer by Telegram option link

Use the link copied for a specific poll answer. It contains the base64url-
encoded answer identifier in its `option` query parameter:

```bash
python list_without_answer.py \
  --option "https://t.me/c/2546560986/6968?option=MA"
```

`--option` replaces both `--poll-link` and `--answer`; do not combine the two
input forms. The decoded answer identifier must exist in the referenced poll.
Telegram documents this link format in
[Polls and quizzes](https://core.telegram.org/api/poll).

### Show what each listed voter selected

Add `--voted-for` to either input form:

```bash
python list_without_answer.py \
  --option "https://t.me/c/2546560986/6968?option=MA" \
  --voted-for
```

This appends a `voted for` column containing answer text. Multiple selected
answers are shown in poll order and separated with `|`. Telegram's free-text
vote record does not expose the submitted text, so it is shown as
`[free-text answer; text unavailable]`.

The table columns are ordered as follows:

```text
ID  username  first name  last name
```

With `--voted-for`:

```text
ID  username  first name  last name  voted for
```

The poll question, excluded answer, poll state, and voter counts are printed
after the table. Missing usernames or names are shown as empty cells.

## Development checks

```bash
python -m pip install -r requirements-dev.txt
ruff check .
python -m unittest discover -s tests -v
python -m compileall -q .
```

## Data safety

`.env` and `*.session*` are excluded from Git. A session file grants access to
the Telegram account, so keep it local. If it may have been exposed, terminate
the corresponding session in **Telegram → Settings → Devices**.
