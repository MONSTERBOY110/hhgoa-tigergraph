"""LLM layer: writes prose only (summary, SAR narrative, pattern description) from a facts JSON.

Structured fields never come from here. Before a text is accepted it must pass the garbage checks (not empty, at
most 10 percent non-ASCII, no code fences, not JSON, not a refusal, the caller's format check) and every id-like
token in it must appear in the facts. Otherwise we regenerate (bounded) and then fall back to the deterministic
template. write() never raises; every outcome is recorded in last_status and status_log so the trace shows it:
"llm", "disabled", or "fallback: <reason>".

Fault injection (drills): KAVACH_FAULT=llm:timeout | llm:garbage | llm:empty is honoured inside _complete without
contacting the provider, even when no key is configured.
"""
import json
import re
import time

from kavach.config import env
from kavach.harness import bounds, faults

REFUSAL_RE = re.compile(r"\b(I cannot|I can't|I can not|I am unable|I'm unable|as an AI|I'm sorry, but|I apologi[sz]e)\b", re.I)
GARBAGE_BLOB = '{"verdict": "legitimate", "fraud_probability": 0.01, "affected_txn_ids": [3999999], "note": "\\u00ff\\u00fe"}'


def garbage_reason(text: str, allowed_ids: set | None = None) -> str | None:
    """Why a model text cannot be used as prose, or None when it can."""
    if text is None or not text.strip():
        return "empty"
    t = text.strip()
    non_ascii = sum(1 for ch in t if ord(ch) > 127)
    if non_ascii / max(len(t), 1) > 0.10:
        return f"non-ASCII ratio {non_ascii / len(t):.0%}"
    if "```" in t:
        return "code fence"
    if t[0] in "{[":
        return "JSON instead of prose"
    if REFUSAL_RE.search(t):
        return "refusal"
    if allowed_ids is not None:
        bad = sorted(set(ID_RE.findall(t)) - set(allowed_ids))
        if bad:
            return f"ids not in the facts {bad[:5]}"
    return None

ID_RE = re.compile(r"\b(?:C\d{5}-K\d|C\d{5}|CC-\d{4}|3\d{6}|CASE-\d{4}-\d{4})\b")
DASH_RE = re.compile("[" + chr(0x2013) + chr(0x2014) + "]")

SYSTEM = (
    "You are a bank fraud investigations writer. Write only from the FACTS JSON you are given. "
    "Never invent identifiers, amounts, dates, merchants or devices; copy them exactly from the facts. "
    "Plain professional English. Never use en dashes or em dashes; use commas, colons or hyphens."
)


class Llm:
    def __init__(self):
        self.base_url = env("LLM_BASE_URL")
        self.api_key = env("LLM_API_KEY")
        self.model = env("LLM_MODEL", "openai/gpt-oss-120b")
        self.tokens = 0
        self._client = None
        self.enabled = bool(self.api_key) and env("KAVACH_NO_LLM") != "1"
        self.calls = 0                  # completions requested in the current case
        self.writes = 0                 # write() calls in the current case
        self.last_status = "disabled" if not self.enabled else "idle"
        self.status_log: list[dict] = []

    def begin_case(self, case_id: str = "") -> None:
        """Reset the per-case budget and status log (one Llm is shared across a batch run)."""
        self.calls, self.writes, self.status_log = 0, 0, []
        self.last_status = "disabled" if not self.enabled else "idle"
        self.case_id = case_id

    def _record(self, task: str, status: str, attempts: int, reasons: list[str]) -> None:
        self.last_status = status
        self.status_log.append({"task": task.split(":")[0][:60], "status": status, "attempts": attempts,
                                "rejected": reasons[-4:], "fault": faults.llm_fault() or ""})

    def _pace(self, cost: int) -> None:
        """Stay under the provider's tokens-per-minute budget (the max_tokens reservation counts against it)."""
        budget = int(env("LLM_TPM", "8000")) - 500
        now = time.time()
        self._window = [(t, c) for t, c in getattr(self, "_window", []) if now - t < 60]
        while self._window and sum(c for _, c in self._window) + cost > budget:
            time.sleep(max(0.5, 60 - (now - self._window[0][0])))
            now = time.time()
            self._window = [(t, c) for t, c in self._window if now - t < 60]
        self._window.append((now, cost))

    def client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=60, max_retries=2)
        return self._client

    def _complete(self, prompt: str, max_tokens: int = 1800) -> str:
        self.calls += 1
        fault = faults.llm_fault()
        if fault == "timeout":
            raise TimeoutError("LLM request timed out (injected KAVACH_FAULT=llm:timeout)")
        if fault == "garbage":
            return GARBAGE_BLOB
        if fault == "empty":
            return ""
        kw = {"reasoning_effort": "low"} if "gpt-oss" in self.model else {}
        self._pace(len(prompt) // 3 + max_tokens)
        r = self.client().chat.completions.create(
            model=self.model, temperature=0.2, max_tokens=max_tokens,
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}], **kw,
        )
        if r.usage:
            self.tokens += r.usage.total_tokens
        choice = r.choices[0]
        if choice.finish_reason != "stop":
            raise RuntimeError(f"incomplete completion ({choice.finish_reason})")
        return (choice.message.content or "").strip()

    def write(self, task: str, facts: dict, fallback: str, check=None) -> tuple[str, str]:
        """Return (text, source) where source is 'llm' or 'template'. Never raises."""
        reasons: list[str] = []
        attempt = 0
        try:
            fault = faults.llm_fault()
            if not self.enabled and not fault:
                self._record(task, "disabled", 0, [])
                return fallback, "template"
            self.writes += 1
            if self.writes > bounds.limit("llm_calls"):
                self._record(task, f"fallback: LLM budget exhausted ({bounds.limit('llm_calls')} writes per case)", 0, [])
                return fallback, "template"
            allowed = set(ID_RE.findall(json.dumps(facts, default=str)))
            prompt = f"TASK:\n{task}\n\nFACTS JSON:\n{json.dumps(facts, indent=1, default=str)}"
            for attempt in range(1, bounds.limit("llm_attempts") + 1):
                try:
                    raw = self._complete(prompt)
                except Exception as e:  # network, timeout, rate limit: never fail the run on the LLM
                    limited = "429" in str(e) or "RateLimit" in type(e).__name__
                    timed_out = isinstance(e, TimeoutError) or "timeout" in f"{type(e).__name__} {e}".lower() or "timed out" in str(e).lower()
                    reasons.append("rate limited" if limited else "timeout" if timed_out else f"error {type(e).__name__}")
                    print(f"  llm {'rate limited, waiting for the token window' if limited else 'error'} ({type(e).__name__})", flush=True)
                    if not fault:
                        time.sleep(25 if limited else 4 * attempt)
                    continue
                why = garbage_reason(raw)
                text = clean(raw) if why is None else ""
                why = why or garbage_reason(text, allowed) or (None if check is None or check(text) else "format check failed")
                if why is None:
                    self._record(task, "llm", attempt, reasons)
                    return text, "llm"
                reasons.append(why)
                prompt += (f"\n\nYour previous answer was rejected ({why}). Write plain prose only, use only identifiers "
                           "present in the facts, and follow the requested format.")
            self._record(task, f"fallback: {reasons[-1] if reasons else 'no usable answer'}", attempt, reasons)
        except Exception as e:  # noqa: BLE001  (the template is always valid; record why we used it)
            self._record(task, f"fallback: error {type(e).__name__}", attempt, reasons + [str(e)[:120]])
        return fallback, "template"


ASCII_MAP = {0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2015: "-", 0x2212: "-", 0x2018: "'", 0x2019: "'", 0x201C: '"',
             0x201D: '"', 0x00A0: " ", 0x202F: " ", 0x2009: " ", 0x2026: "...", 0x2192: "->", 0x2248: "about ", 0x00D7: "x"}


def clean(text: str) -> str:
    text = text.translate(ASCII_MAP)
    text = DASH_RE.sub(", ", text)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r"^\s*(summary|narrative)\s*:\s*", "", text, flags=re.I)
    text = text.replace("**", "").strip().strip('"')
    return re.sub(r"\s+", " ", text)


def sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s])
