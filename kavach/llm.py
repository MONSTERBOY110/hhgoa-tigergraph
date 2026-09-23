"""LLM layer: writes prose only (summary, SAR narrative, pattern description) from a facts JSON.

Structured fields never come from here. Every id-like token in the output must appear in
the facts; otherwise we regenerate once and then fall back to the deterministic template.
"""
import json
import re
import time

from kavach.config import env

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
        """Return (text, source) where source is 'llm' or 'template'."""
        if not self.enabled:
            return fallback, "template"
        allowed = set(ID_RE.findall(json.dumps(facts, default=str)))
        prompt = f"TASK:\n{task}\n\nFACTS JSON:\n{json.dumps(facts, indent=1, default=str)}"
        for attempt in range(5):
            try:
                text = clean(self._complete(prompt))
            except Exception as e:  # network, rate limit: never fail the run on the LLM
                limited = "429" in str(e) or "RateLimit" in type(e).__name__
                print(f"  llm {'rate limited, waiting for the token window' if limited else 'error'} ({type(e).__name__})", flush=True)
                time.sleep(25 if limited else 4 * (attempt + 1))
                continue
            bad = set(ID_RE.findall(text)) - allowed
            if text and not bad and (check is None or check(text)):
                return text, "llm"
            prompt += f"\n\nYour previous answer used identifiers not in the facts ({sorted(bad)}) or broke the format. Try again."
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
