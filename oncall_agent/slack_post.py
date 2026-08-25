import time
from pathlib import Path
from typing import Callable, Dict, Tuple

from slack_sdk import WebClient

from .composer import ComposedThread

"""
Posts a composed thread (see composer.py) to Slack. This module never reads
Slack — only posts — matching the split with decide_resolution.py, which
reads Slack via its own separate connector identity.
"""


def post_composed_thread(
    client: WebClient,
    channel: str,
    thread: ComposedThread,
    evidence_file_paths: Dict[str, Path],
    log: Callable[[str], None] = print,
) -> Tuple[str, str]:
    """Returns the (channel, thread_ts) actually posted to, so the caller can persist it for future recurrences."""
    if not thread.should_post:
        log("Not posting — judgment was should_post=False.")
        return channel, ""

    if thread.existing_thread:
        # Known recurring issue AND we have a record of our own prior post
        # about it — reply there instead of starting a new thread. This
        # overrides the `channel` argument since the recorded thread is
        # itself the channel this pipeline already posts to.
        channel, thread_ts = thread.existing_thread
        log(f"Known recurring issue — appending to our own prior thread {thread_ts} in channel {channel}")
        # reply_broadcast ("also send to channel") so this recurrence's
        # alert line is visible in the main channel feed, not hidden inside
        # the thread — matching real NOC practice for recurring incidents.
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=thread.top_level_text, reply_broadcast=True)
    else:
        top = client.chat_postMessage(channel=channel, text=thread.top_level_text)
        thread_ts = top["ts"]

    for post in thread.posts:
        file_paths = [evidence_file_paths[k] for k in post.evidence_keys if k in evidence_file_paths]
        if file_paths:
            client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                initial_comment=post.text,
                file_uploads=[{"file": str(p), "filename": p.name} for p in file_paths],
            )
            # files_upload_v2 resolving doesn't mean the file has finished
            # rendering in the channel yet — a plain text reply posted right
            # after can visibly land before it. Give it a moment to catch up.
            time.sleep(2)
        else:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=post.text)

    return channel, thread_ts


class DryRunSlackPoster:
    """Prints what would be posted instead of calling the Slack API — default until explicitly wired to a real token+channel."""

    def __init__(self, log: Callable[[str], None] = print):
        self.log = log

    def post_composed_thread(self, thread: ComposedThread, evidence_file_paths: Dict[str, Path]) -> None:
        if not thread.should_post:
            self.log("[DRY RUN] would NOT post — judgment was should_post=False.")
            return
        if thread.existing_thread:
            channel, thread_ts = thread.existing_thread
            self.log(
                f"\n[DRY RUN] KNOWN RECURRING ISSUE — would append (broadcast to channel) to our own prior "
                f"thread {thread_ts} in channel {channel}:\n{thread.top_level_text}"
            )
        else:
            self.log(f"\n[DRY RUN] would post top-level:\n{thread.top_level_text}")
        for post in thread.posts:
            files = [str(evidence_file_paths[k]) for k in post.evidence_keys if k in evidence_file_paths]
            self.log(f"\n[DRY RUN] would reply:\n{post.text}" + (f"\n  files: {files}" if files else ""))
