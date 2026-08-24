import path from "node:path";
import { WebClient } from "@slack/web-api";
import type { ComposedThread, SlackPost } from "./composer.js";

export interface SlackClient {
  postThread(channel: string, thread: ComposedThread): Promise<void>;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** Default until a bot token + explicit send confirmation are wired up — never posts for real. */
export class DryRunSlackClient implements SlackClient {
  constructor(private readonly log: (line: string) => void = console.log) {}

  async postThread(channel: string, thread: ComposedThread): Promise<void> {
    const render = (post: SlackPost) =>
      [post.text, ...(post.filePaths ?? []).map((p) => `  [file: ${p}]`)].join("\n");

    this.log(`\n[DRY RUN] would post to ${channel}:\n${render(thread.topLevel)}`);
    for (const reply of thread.replies) {
      this.log(`\n[DRY RUN] would reply in thread:\n${render(reply)}`);
    }
  }
}

/** Posts for real via the Slack Web API. Requires a bot token with chat:write + files:write. */
export class WebApiSlackClient implements SlackClient {
  private readonly web: WebClient;

  constructor(botToken: string) {
    this.web = new WebClient(botToken);
  }

  async postThread(channel: string, thread: ComposedThread): Promise<void> {
    const top = await this.web.chat.postMessage({ channel, text: thread.topLevel.text });
    const threadTs = top.ts;
    for (const reply of thread.replies) {
      if (reply.filePaths?.length) {
        await this.web.filesUploadV2({
          channel_id: channel,
          thread_ts: threadTs,
          initial_comment: reply.text,
          file_uploads: reply.filePaths.map((file) => ({ file, filename: path.basename(file) })),
        });
        // filesUploadV2 resolving doesn't mean the file has finished
        // rendering in the channel yet — a plain text reply posted right
        // after can visibly land before it, showing out of order even
        // though this call was awaited first. Give it a moment to catch up.
        await sleep(2000);
      } else {
        await this.web.chat.postMessage({ channel, thread_ts: threadTs, text: reply.text });
      }
    }
  }
}
