"""@mailgent - LLM agent that monitors emails for tagged prompts and responds using tools."""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from datetime import datetime

import litellm

from .backend import create_backend
from .config import Settings
from .formatting import (
    extract_prompt_style,
    html_to_annotated_text,
    md_to_html,
    md_to_plain,
)
from .tools import TOOL_DEFINITIONS, ToolExecutor

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are @mailgent, a personal email and calendar assistant. You have access to \
tools for searching emails, reading email content, managing calendar \
events, and finding free meeting slots.

Today's date is {today}.

When responding:
- Be concise and professional
- Format results clearly using Markdown
- Use exact data from the tools (don't guess)
- If you need to look something up, use the appropriate tool
- Respond in the same language as the user's prompt
- By default, your response replaces the area directly below the prompt \
  in the email draft. Just provide the answer text.
- If the user asks you to modify existing text, reply to someone, or write \
  to a specific recipient, use the appropriate tools (draft_reply, create_draft).

Reading the email context:
- Text marked as ~~strikethrough~~ means it was crossed out by the author. \
  Treat it as cancelled, not available, or rejected depending on context.
- Text marked as [HIGHLIGHT: ...] was highlighted by the author for emphasis.
"""


# ID and separator helpers

_ID_LEN = 8


def _gen_id() -> str:
    return secrets.token_hex(_ID_LEN // 2)


def _sep_plain(rid: str) -> str:
    return f"------------ mailgent[{rid}] ------------"


def _sep_plain_err(rid: str) -> str:
    return f"------------ mailgent[{rid}] ERROR ------------"


_MARKER_STYLE = "margin:4px 0;padding:0;font-size:9px;color:#aaa;font-family:monospace"
_MARKER_ERR_STYLE = "margin:4px 0;padding:0;font-size:9px;color:#c33;font-family:monospace"


def _sep_html_open(rid: str) -> str:
    return (
        f'<div style="{_MARKER_STYLE}">'
        f'<hr style="border:none;border-top:1px solid #ccc;margin:2px 0">'
        f"mailgent[{rid}]</div>"
        f'<div data-mailgent="{rid}">'
    )


def _sep_html_close(rid: str) -> str:
    return (
        f'</div><div style="{_MARKER_STYLE}">'
        f"mailgent[{rid}]"
        f'<hr style="border:none;border-top:1px solid #ccc;margin:2px 0">'
        f"</div>"
    )


def _sep_html_open_err(rid: str) -> str:
    return (
        f'<div style="{_MARKER_ERR_STYLE}">'
        f'<hr style="border:none;border-top:1px solid #c33;margin:2px 0">'
        f"mailgent[{rid}] ERROR</div>"
        f'<div data-mailgent="{rid}">'
    )


# Regex patterns


def _compile_patterns(tag: str):
    esc = re.escape(tag)
    prompt = re.compile(
        rf"({esc})\s*:\s*(.+?)(?=\n\s*\n|\Z)",
        re.DOTALL | re.IGNORECASE,
    )
    processed = re.compile(
        rf"{esc}\[([0-9a-f]{{{_ID_LEN}}})\]",
        re.IGNORECASE,
    )
    return prompt, processed


# Subject prefix stripping (multilingual)

_PREFIX_WORDS = (
    r"Re|Fwd|Fw|AW|WG|TR|RV|ENC|RES"
    r"|Antw|Doorst"
    r"|Odp|PD|Odg"
    r"|SV|VB|VS|VL|FS"
    r"|Předat|Răsp"
    r"|Vá|Tov"
    r"|YNT|İLT"
    r"|ΑΠ|ΠΡΘ"
    r"|Vast|Edasi"
    r"|Atb|Pārs"
    r"|Ats"
)
_IT = r"[RI]"
_SUBJECT_PREFIX = re.compile(
    rf"^((?:(?:{_PREFIX_WORDS})\s*[:：]"
    rf"|{_IT}\s*[:：]"
    rf"|\[(?:{_PREFIX_WORDS})\]"
    rf"|\((?:{_PREFIX_WORDS})\))\s*)+",
    re.IGNORECASE,
)


def _strip_subject_prefixes(subject: str) -> str:
    return _SUBJECT_PREFIX.sub("", subject).strip()


# Mailgent class


class Mailgent:
    """LLM agent that processes @mailgent-tagged emails."""

    def __init__(self, settings: Settings | None = None, backend=None) -> None:
        self._settings = settings or Settings()
        self.backend = backend or create_backend(self._settings)
        self.backend.connect()
        self.executor = ToolExecutor(self.backend)

        self.tag = self._settings.mailgent_tag
        self.model = self._settings.effective_mailgent_model
        self.auto_reply = self._settings.mailgent_auto_reply
        self.auto_send = self._settings.mailgent_auto_send
        self._trusted_reply = self._parse_trusted(self._settings.mailgent_trusted_reply)
        self._trusted_send = self._parse_trusted(self._settings.mailgent_trusted_send)

        self._re_prompt, self._re_processed = _compile_patterns(self.tag)
        self._setup_llm_env()

    # Config helpers

    @staticmethod
    def _parse_trusted(value: str) -> list[str]:
        return [p.strip().lower() for p in value.split(",") if p.strip()]

    def _is_trusted(self, sender_email: str, trusted_list: list[str]) -> bool:
        if not trusted_list:
            return True
        import fnmatch

        email_lower = sender_email.lower()
        return any(fnmatch.fnmatch(email_lower, p) for p in trusted_list)

    def _setup_llm_env(self) -> None:
        m = self.model.lower()
        is_anthropic = m.startswith("claude") or m.startswith("anthropic/")
        azure_key = self._settings.azure_api_key or os.environ.get("AZURE_API_KEY", "")
        azure_base = self._settings.azure_api_base or os.environ.get("AZURE_API_BASE", "")
        if is_anthropic and not os.environ.get("ANTHROPIC_API_KEY") and azure_key:
            os.environ["ANTHROPIC_API_KEY"] = azure_key
            if azure_base:
                base = azure_base.rstrip("/")
                if not base.endswith("/anthropic"):
                    base += "/anthropic"
                os.environ["ANTHROPIC_API_BASE"] = base

    def _litellm_model(self) -> str:
        m = self.model
        if m.lower().startswith("claude") and "/" not in m:
            return f"anthropic/{m}"
        return m

    # Polling

    def poll(self) -> list[dict]:
        """Find emails with unprocessed @mailgent prompts."""
        if self.backend.supports_com:
            return self._poll_com()
        return self._poll_imap()

    def _poll_com(self) -> list[dict]:
        """COM-specific polling with server-side filtering."""
        import pythoncom

        from .com_client import OL_FOLDER_DRAFTS, OL_FOLDER_INBOX

        found = []
        com = self.backend.raw_com
        folders = [(OL_FOLDER_DRAFTS, "Drafts"), (OL_FOLDER_INBOX, "Inbox")]

        for folder_id, folder_name in folders:
            try:
                folder = com._ns.GetDefaultFolder(folder_id)
                items = folder.Items
                filt = (
                    f"@SQL=("
                    f"\"urn:schemas:httpmail:subject\" LIKE '%{self.tag}%'"
                    f" OR "
                    f"\"urn:schemas:httpmail:textdescription\" LIKE '%{self.tag}%'"
                    f")"
                )
                try:
                    items = items.Restrict(filt)
                except Exception:
                    pass

                items.Sort("[ReceivedTime]", True)
                item = items.GetFirst()
                checked = 0
                while item and checked < 50:
                    checked += 1
                    try:
                        self._check_com_item(item, folder_name, found)
                    except Exception as e:
                        logger.debug("Skip item in %s: %s", folder_name, e)
                    pythoncom.PumpWaitingMessages()
                    item = items.GetNext()
            except Exception as e:
                logger.error("Error scanning %s: %s", folder_name, e)

        if found:
            logger.info("Found %d email(s) with unprocessed prompts", len(found))
        return found

    def _check_com_item(self, item, folder_name: str, found: list) -> None:
        from .com_client import OL_MAIL

        subject = str(item.Subject or "")
        body = str(getattr(item, "Body", "") or "")

        if self.tag not in subject and self.tag not in body:
            return

        if folder_name == "Inbox":
            try:
                verb = item.PropertyAccessor.GetProperty("http://schemas.microsoft.com/mapi/proptag/0x10810003")
                if verb in (102, 103):
                    return
            except Exception:
                pass

        has_unprocessed = bool(self._re_prompt.search(body))
        if not has_unprocessed:
            has_unprocessed = bool(self._re_prompt.search(subject))
        if not has_unprocessed:
            return

        if item.Class != OL_MAIL and folder_name != "Drafts":
            return

        body_format = getattr(item, "BodyFormat", 1)
        is_html = body_format == 2
        found.append(
            {
                "entry_id": item.EntryID,
                "subject": subject,
                "body": body,
                "html_body": str(item.HTMLBody or "") if is_html else "",
                "is_html": is_html,
                "folder": folder_name,
                "sender": str(getattr(item, "SenderName", "") or ""),
                "sender_email": str(getattr(item, "SenderEmailAddress", "") or ""),
            }
        )

    def _poll_imap(self) -> list[dict]:
        """IMAP polling - search by subject for the tag."""
        found = []
        for folder_name in ("Drafts", "INBOX"):
            try:
                display_name = "Drafts" if folder_name == "Drafts" else "Inbox"
                msgs = self.backend.search_emails(self.tag, folder=folder_name, limit=50)
                for msg in msgs:
                    body = msg.body_text or msg.body_html or ""
                    has_unprocessed = bool(self._re_prompt.search(body))
                    if not has_unprocessed:
                        has_unprocessed = bool(self._re_prompt.search(msg.subject))
                    if not has_unprocessed:
                        continue
                    found.append(
                        {
                            "entry_id": msg.uid,
                            "subject": msg.subject,
                            "body": msg.body_text or "",
                            "html_body": msg.body_html or "",
                            "is_html": bool(msg.body_html),
                            "folder": display_name,
                            "sender": msg.sender_name or "",
                            "sender_email": msg.sender or "",
                        }
                    )
            except Exception as e:
                logger.error("Error scanning %s: %s", folder_name, e)

        if found:
            logger.info("Found %d email(s) with unprocessed prompts", len(found))
        return found

    # Processing

    def process(self, email: dict) -> list[str]:
        """Process all unprocessed @mailgent prompts in one email."""
        body = email["body"]
        is_html = email.get("is_html", False)

        if is_html and email.get("html_body"):
            annotated = html_to_annotated_text(email["html_body"])
        else:
            annotated = ""

        prompts = list(self._re_prompt.finditer(body))
        if not prompts:
            prompts = list(self._re_prompt.finditer(email["subject"]))
        if not prompts:
            logger.warning("No unprocessed prompts in: %s", email["subject"])
            return []

        thread_ctx = self._gather_thread_context(email["subject"])

        responses = []
        for match in prompts:
            prompt_text = match.group(2).strip()
            rid = _gen_id()

            logger.info(
                "[%s] %s mailgent[%s]: %s",
                email["folder"],
                email["subject"][:40],
                rid,
                prompt_text[:60],
            )

            html_style = ""
            if is_html and email.get("html_body"):
                html_style = extract_prompt_style(email["html_body"], self.tag)

            try:
                raw = self._run_llm(
                    prompt_text,
                    thread_context=thread_ctx,
                    email_body=annotated or body,
                )
                if is_html:
                    response_text = md_to_html(raw, style=html_style)
                else:
                    response_text = md_to_plain(raw)
                error = False
            except Exception as e:
                logger.error("LLM error for [%s]: %s", rid, e)
                response_text = f"Error: {e}"
                error = True

            if email["folder"] == "Drafts":
                self._update_draft(
                    email["entry_id"],
                    prompt_text,
                    rid,
                    response_text,
                    is_html=is_html,
                    error=error,
                )
            elif email["folder"] == "Inbox":
                sender = email.get("sender_email", "")
                can_reply = self.auto_reply or self._is_trusted(sender, self._trusted_reply)
                can_send = self.auto_send or self._is_trusted(sender, self._trusted_send)
                if can_reply:
                    self._reply_to_email(email["entry_id"], response_text, auto_send=can_send)
                else:
                    logger.info("Inbox from %s -- auto-reply disabled", sender)

            responses.append(response_text)

        return responses

    # Thread context

    def _gather_thread_context(self, subject: str, limit: int = 10) -> str:
        base = _strip_subject_prefixes(subject)
        if not base or len(base) < 3:
            return ""

        try:
            related = self.backend.search_emails(base, limit=limit)
            # Also search sent
            if self.backend.supports_com:
                from .com_client import OL_FOLDER_SENT

                sent_raw = self.backend.raw_com.search_emails(base, folder_id=OL_FOLDER_SENT, limit=limit)
                from .backend import _com_dict_to_email

                sent = [_com_dict_to_email(d) for d in sent_raw]
            else:
                sent = self.backend.search_emails(base, folder="Sent", limit=limit)
            related.extend(sent)
        except Exception as e:
            logger.warning("Thread context search failed: %s", e)
            return ""

        if not related:
            return ""

        seen = set()
        unique = []
        for r in related:
            if r.uid not in seen:
                seen.add(r.uid)
                unique.append(r)
        unique.sort(key=lambda r: r.date or datetime.min)

        parts = []
        for r in unique[-limit:]:
            try:
                full = self.backend.get_message(r.uid)
                body = full.body
                if len(body) > 2000:
                    body = body[:2000] + "\n[... truncated]"
                date_str = str(r.date)[:19] if r.date else "?"
                parts.append(f"--- {date_str} From: {r.sender_name or r.sender} ---\nSubject: {r.subject}\n\n{body}")
            except Exception:
                date_str = str(r.date)[:19] if r.date else "?"
                parts.append(
                    f"--- {date_str} From: {r.sender_name or r.sender} ---\nSubject: {r.subject}\n(body not available)"
                )

        return "\n\n".join(parts)

    # LLM with tools

    def _run_llm(
        self,
        prompt: str,
        thread_context: str = "",
        email_body: str = "",
        max_rounds: int = 25,
    ) -> str:
        system = SYSTEM_PROMPT.format(today=datetime.now().strftime("%A, %d.%m.%Y"))
        parts = []
        if thread_context:
            parts.append(f"EMAIL THREAD CONTEXT (related emails, oldest first):\n\n{thread_context}")
        if email_body:
            parts.append(
                f"CURRENT EMAIL BODY "
                f"(~~text~~ = strikethrough/cancelled, "
                f"[HIGHLIGHT: text] = highlighted):\n\n{email_body}"
            )
        parts.append(f"USER REQUEST:\n{prompt}")
        user_content = "\n\n---\n\n".join(parts)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]

        for _ in range(max_rounds):
            response = litellm.completion(
                model=self._litellm_model(),
                messages=messages,
                tools=TOOL_DEFINITIONS,
                tool_choice="auto",
                temperature=0,
            )

            msg = response.choices[0].message

            if not msg.tool_calls:
                return msg.content or ""

            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": msg.tool_calls,
                }
            )

            for tc in msg.tool_calls:
                args = tc.function.arguments
                if isinstance(args, str):
                    args = json.loads(args)
                logger.info("  Tool: %s(%s)", tc.function.name, json.dumps(args, ensure_ascii=False)[:100])
                result = self.executor.execute(tc.function.name, args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    }
                )

        raise RuntimeError("max tool rounds exceeded")

    # Draft update

    def _update_draft(
        self,
        entry_id: str,
        prompt_text: str,
        rid: str,
        response: str,
        is_html: bool = False,
        error: bool = False,
    ) -> None:
        if self.backend.supports_com:
            self._update_draft_com(entry_id, prompt_text, rid, response, is_html=is_html, error=error)
        else:
            self._update_draft_imap(entry_id, prompt_text, rid, response, is_html=is_html, error=error)

    def _update_draft_com(
        self,
        entry_id: str,
        prompt_text: str,
        rid: str,
        response: str,
        is_html: bool = False,
        error: bool = False,
    ) -> None:
        com = self.backend.raw_com
        for attempt in range(3):
            try:
                item = com._ns.GetItemFromID(entry_id)
                if is_html:
                    self._update_html(item, prompt_text, rid, response, error=error)
                else:
                    self._update_plain(item, prompt_text, rid, response, error=error)
                item.Save()
                logger.info("Draft updated [%s]: %s", rid, item.Subject)
                return
            except Exception as e:
                if attempt < 2:
                    logger.warning("Draft update failed (%d/3): %s", attempt + 1, e)
                    time.sleep(2)
                else:
                    logger.error("Draft update failed after 3: %s", e)

    def _update_draft_imap(
        self,
        entry_id: str,
        prompt_text: str,
        rid: str,
        response: str,
        is_html: bool = False,
        error: bool = False,
    ) -> None:
        """IMAP draft update: delete old draft, save new one with response inserted."""
        try:
            msg = self.backend.get_message(entry_id)
            body = msg.body_text or ""

            # Insert response into body
            tag_pos = body.find(self.tag)
            if tag_pos < 0:
                logger.warning("Tag not found in IMAP draft body for [%s]", rid)
                return

            line_end = body.find("\n", tag_pos)
            if line_end < 0:
                line_end = len(body)

            tagged = f"{self.tag}[{rid}]: {prompt_text}"
            sep = _sep_plain_err(rid) if error else _sep_plain(rid)
            block = f"\n{sep}\n{response}\n{_sep_plain(rid)}"
            new_body = body[:tag_pos] + tagged + block + body[line_end:]

            # Delete old, save new
            client = self.backend._ensure_client()
            client.delete_draft(entry_id)
            client.save_draft(
                to=msg.to,
                subject=msg.subject,
                body=new_body,
                body_type="plain",
            )
            logger.info("IMAP draft updated [%s]: %s", rid, msg.subject)
        except Exception as e:
            logger.error("IMAP draft update failed [%s]: %s", rid, e)

    def _update_plain(self, item, prompt_text: str, rid: str, response: str, error: bool = False) -> None:
        body = str(item.Body or "")
        tag_pos = body.find(self.tag)
        if tag_pos < 0:
            logger.warning("Tag not found in body for [%s]", rid)
            return

        line_end = body.find("\n", tag_pos)
        if line_end < 0:
            line_end = len(body)

        tagged = f"{self.tag}[{rid}]: {prompt_text}"
        sep = _sep_plain_err(rid) if error else _sep_plain(rid)
        block = f"\n{sep}\n{response}\n{_sep_plain(rid)}"
        item.Body = body[:tag_pos] + tagged + block + body[line_end:]

    def _update_html(self, item, prompt_text: str, rid: str, response: str, error: bool = False) -> None:
        html = str(item.HTMLBody or "")
        tag_pos = html.find(self.tag)
        if tag_pos < 0:
            logger.warning("Tag not found in HTMLBody for [%s]", rid)
            return

        after_tag = tag_pos + len(self.tag)
        colon_match = re.match(r"\s*:\s*", html[after_tag:])
        if colon_match:
            prompt_start = after_tag + colon_match.end()
        else:
            prompt_start = after_tag

        next_tag = html.find("<", prompt_start)
        if next_tag < 0:
            next_tag = len(html)

        tagged = f"{self.tag}[{rid}]: {prompt_text}"
        html = html[:tag_pos] + tagged + html[next_tag:]

        tag_pos = html.find(f"{self.tag}[{rid}]")
        if tag_pos < 0:
            item.HTMLBody = html
            return

        search_from = tag_pos + len(tagged)
        end_markers = ["</p>", "</div>", "</span>", "<br>", "<br/>", "<br />"]
        insert_pos = len(html)
        for marker in end_markers:
            pos = html.lower().find(marker.lower(), search_from)
            if pos != -1 and pos < insert_pos:
                insert_pos = pos + len(marker)

        opener = _sep_html_open_err(rid) if error else _sep_html_open(rid)
        block = f"{opener}{response}{_sep_html_close(rid)}"
        item.HTMLBody = html[:insert_pos] + block + html[insert_pos:]

    # Reply to inbox

    def _reply_to_email(self, entry_id: str, response: str, auto_send: bool = False) -> None:
        for attempt in range(3):
            try:
                if auto_send:
                    self.backend.send_reply(entry_id, response)
                    logger.info("Reply sent for: %s", entry_id[:20])
                else:
                    self.backend.draft_reply(entry_id, response)
                    logger.info("Reply draft for: %s", entry_id[:20])
                return
            except Exception as e:
                if attempt < 2:
                    logger.warning("Reply failed (%d/3): %s", attempt + 1, e)
                    time.sleep(2)
                else:
                    logger.error("Reply failed after 3: %s", e)

    # Main loop

    def run(self, poll_interval: int = 30) -> None:
        logger.info(
            "@mailgent started (model=%s, poll=%ds, auto_reply=%s, auto_send=%s)",
            self.model,
            poll_interval,
            self.auto_reply,
            self.auto_send,
        )
        consecutive_errors = 0
        while True:
            try:
                emails = self.poll()
                for email in emails:
                    self.process(email)
                consecutive_errors = 0
            except KeyboardInterrupt:
                logger.info("Agent stopped by user")
                break
            except Exception as e:
                consecutive_errors += 1
                backoff = min(poll_interval * (2**consecutive_errors), 600)
                logger.error("Poll error (%d): %s -- retry in %ds", consecutive_errors, e, backoff)
                time.sleep(backoff)
                continue
            time.sleep(poll_interval)

    def run_once(self) -> int:
        emails = self.poll()
        count = 0
        for email in emails:
            results = self.process(email)
            count += len(results)
        return count
