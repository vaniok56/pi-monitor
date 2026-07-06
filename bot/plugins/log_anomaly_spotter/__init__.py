"""
Log Anomaly Spotter plugin.

Every N minutes (default 15): pull docker logs from all running containers,
filter through log_rules.yml (same interest/ignore logic as LogLoopManager),
cluster repeated error signatures, and send a compact digest to the LLM for
one-line triage. Falls back to a plain count digest if the LLM is unavailable.

LLM backend: ollama-mini (qwen2.5:0.5b-instruct) over the shared ollama-net.
System prompt is tuned for low-token output on a weak CPU (i5-3210M).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import docker
import yaml
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from alerts import AlertItem, AlertType
from alerts.notifier import put_alert
from plugins._ctx import PluginMeta

if TYPE_CHECKING:
    from plugins._ctx import PluginContext

logger = logging.getLogger(__name__)

META = PluginMeta(
    name="log_anomaly_spotter",
    description="LLM-powered log triage digest from all running containers",
    default_config={
        "schedule": "*/15 * * * *",
        "since_minutes": 15,
        "max_clusters": 2,
        "cooldown_minutes": 30,
        "state_path": "/data/log_spotter_state.json",
        "ollama_url_env": "OLLAMA_URL",
        "model_env": "LOG_SPOTTER_MODEL",
        "llm_timeout": 120,
    },
)

_CB_MENU = "p.log_spotter:menu"
_CB_RUN = "p.log_spotter:run"
_CB_PLUGINS = "plugins_menu"

# ── Perf-tuned system prompt for 0.5B model on slow hardware ────────────────
# One call per cluster keeps output to 1 line and num_ctx tiny (256).
# ASCII tags [CRITICAL/WARNING/INFO] — small models hallucinate <emoji> XML tags.
# _postprocess() maps tags to emojis before Telegram send.
# Inline few-shot examples so model knows the format without verbose explanation.
_SYSTEM_PROMPT = (
    "Log triage. Per input line output one line: [LEVEL] container:cause->fix. "
    "CRITICAL=down WARNING=slow INFO=noise.\n"
    "immich|x5|conn refused -> [CRITICAL] immich:db down->restart postgres\n"
    "npm|x3|upstream timed out -> [WARNING] npm:upstream slow->check service\n"
    "bot|x2|429 rate limited -> [INFO] bot:rate limit->backoff expected"
)

_LEVEL_EMOJI = {"[CRITICAL]": "🔴", "[WARNING]": "🟠", "[INFO]": "🟡"}


_TAG_SPLIT = re.compile(r"(?=\[(?:CRITICAL|WARNING|INFO)\])")


def _postprocess(raw: str) -> str:
    """Convert [CRITICAL]/[WARNING]/[INFO] tags to emojis.
    Also splits on inline tags (model sometimes puts 2 clusters on 1 line).
    """
    lines = []
    for line in raw.strip().splitlines():
        # Split on any embedded [LEVEL] tag so each segment is processed individually.
        segments = [s.strip() for s in _TAG_SPLIT.split(line) if s.strip()]
        if not segments:
            continue
        for seg in segments:
            matched = False
            for tag, emoji in _LEVEL_EMOJI.items():
                if seg.startswith(tag):
                    lines.append(emoji + " " + seg[len(tag):].lstrip())
                    matched = True
                    break
            if not matched and len(seg) > 10:
                first_cp = ord(seg[0])
                if first_cp > 127:
                    lines.append(seg)
                elif ":" in seg or "—" in seg or "->" in seg:
                    lines.append("🟡 " + seg)
    return "\n".join(lines)

# ── Fingerprint regexes — mirrors alerts/logloop.py ──────────────────────────
_FP_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(
        r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:\d{2})?\s*"
        r"|^\d{2}:\d{2}:\d{2}[.,]\d+\s*\|\s*"
        r"|^\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+"
    ), ""),
    (re.compile(r"[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4}){5,7}"), "<IP>"),
    (re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?"), "<IP>"),
    (re.compile(r"(?:port[=: ]+|:)\d{2,5}\b"), "<PORT>"),
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "<ID>"),
    (re.compile(r"\b[0-9a-fA-F]{8,}\b"), "<ID>"),
    (re.compile(r'(?:^|[\s"])\/(?:[\w.\-]+\/)*[\w.\-]+'), " <PATH>"),
    (re.compile(r'"[^"]{0,120}"'), '"<STR>"'),
    (re.compile(r"'[^']{0,120}'"), "'<STR>'"),
    (re.compile(r"\b\d{4,}\b"), "<NUM>"),
    (re.compile(r"\s+"), " "),
]

_INTERESTING_DEFAULT = re.compile(
    r"ERROR|WARN(?:ING)?|CRITICAL|FATAL|Exception|Traceback"
    r"|Errno \d+|panic:|SIGSEGV|stack trace|timeout|refused"
    r"|reset by peer|5\d\d [A-Z]",
    re.IGNORECASE,
)

# Plugin-level noise filter applied BEFORE log_rules.yml ignore list.
# Catches common false positives that match "timeout" or other interesting words
# for non-error reasons (e.g. Telegram long-poll URL, successful HTTP responses).
_GLOBAL_NOISE = re.compile(
    r"getUpdates\?timeout="          # Telegram bot polling — not an error
    r"|HTTP/1\.\d [123]\d\d "        # successful HTTP responses (1xx/2xx/3xx)
    r"|\"HTTP/1\.\d [123]\d\d\"",    # same quoted
    re.IGNORECASE,
)


def _fingerprint(line: str) -> str:
    sig = line
    for pat, rep in _FP_RULES:
        sig = pat.sub(rep, sig)
    return sig.strip()


def _sig_hash(sig: str) -> str:
    return hashlib.sha1(sig.encode()).hexdigest()[:12]


# ── State helpers ────────────────────────────────────────────────────────────

def _load_state(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("log_spotter: state load failed: %s", exc)
        return {}


def _save_state(path: str, state: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except Exception as exc:
        logger.error("log_spotter: state save failed: %s", exc)


def _recently_alerted(state: dict, sig: str, cooldown_minutes: int) -> bool:
    seen = state.get("seen", {})
    entry = seen.get(sig)
    if not entry:
        return False
    last = datetime.fromisoformat(entry["last_alert"])
    return (datetime.now(timezone.utc) - last).total_seconds() < cooldown_minutes * 60


def _mark_alerted(state: dict, sig: str, container: str, count: int) -> None:
    seen = state.setdefault("seen", {})
    seen[sig] = {
        "last_alert": datetime.now(timezone.utc).isoformat(),
        "container": container,
        "count": count,
    }


# ── Log collection ────────────────────────────────────────────────────────────

def _load_rules() -> dict:
    p = Path(__file__).parent.parent.parent / "config" / "log_rules.yml"
    try:
        with open(p) as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("log_spotter: could not load log_rules.yml: %s", exc)
        return {}


def _interesting_re(rules: dict, container: str) -> re.Pattern:
    defaults = rules.get("defaults", {})
    overrides = rules.get("containers", {}).get(container, {})
    pats = overrides.get("interesting") or defaults.get("interesting") or []
    if not pats:
        return _INTERESTING_DEFAULT
    return re.compile("|".join(f"(?:{p})" for p in pats), re.IGNORECASE)


def _ignore_re(rules: dict, container: str) -> re.Pattern | None:
    defaults = rules.get("defaults", {})
    overrides = rules.get("containers", {}).get(container, {})
    pats = overrides.get("ignore") or defaults.get("ignore") or []
    if not pats:
        return None
    return re.compile("|".join(f"(?:{p})" for p in pats), re.IGNORECASE)


def _collect_clusters(since_minutes: int) -> dict[str, dict]:
    """
    Pull logs from all running containers since N minutes ago.
    Returns {sig_hash: {container, count, sample}}.
    """
    try:
        client = docker.from_env()
    except Exception as exc:
        logger.error("log_spotter: docker connect failed: %s", exc)
        return {}

    rules = _load_rules()
    since_dt = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    clusters: dict[str, dict] = {}

    for container in client.containers.list():
        name = container.name
        interesting = _interesting_re(rules, name)
        ignore = _ignore_re(rules, name)
        try:
            raw = container.logs(since=since_dt, timestamps=False)
            lines = raw.decode("utf-8", errors="replace").splitlines()
        except Exception as exc:
            logger.debug("log_spotter: logs(%s) failed: %s", name, exc)
            continue

        for line in lines:
            if not interesting.search(line):
                continue
            if _GLOBAL_NOISE.search(line):
                continue
            if ignore and ignore.search(line):
                continue
            sig = _fingerprint(line)
            sh = _sig_hash(sig)
            if sh not in clusters:
                clusters[sh] = {
                    "container": name,
                    "count": 0,
                    "sample": line[:300],
                    "sig": sig,
                }
            clusters[sh]["count"] += 1

    return clusters


# ── LLM triage ───────────────────────────────────────────────────────────────



def _plain_digest(top: list[tuple[str, dict]], since_minutes: int) -> str:
    lines = [f"🔍 <b>Log Anomaly Digest</b> (last {since_minutes}m — LLM unavailable)\n"]
    for _, cl in top:
        lines.append(
            f"• <b>{cl['container']}</b> ×{cl['count']}\n"
            f"  <code>{cl['sample'][:200]}</code>"
        )
    return "\n".join(lines)


_LLM_OPTIONS = {
    "temperature": 0,
    "top_p": 1,
    "top_k": 1,
    "num_predict": 40,   # 2 clusters × ~15-20 tok; model stops at \n\n naturally
    "num_ctx": 512,      # 0.5b needs ≥512 to follow instructions reliably
    "num_thread": 4,
    "repeat_penalty": 1.3,
    "stop": ["\n\n", "User:", "```", "Input:"],
}


def _llm_triage(top: list[tuple[str, dict]], since_minutes: int,
                ollama_url: str, model: str, timeout: int) -> str:
    """Single LLM call for all clusters. Falls back to plain digest on any error."""
    import httpx
    lines = [f"{cl['container']}|x{cl['count']}|{cl['sample'][:100].replace(chr(10),' ')}" for _, cl in top]
    prompt = "Input:\n" + "\n".join(lines) + "\n\nOutput:\n"
    payload = {
        "model": model,
        "system": _SYSTEM_PROMPT,
        "prompt": prompt,
        "stream": False,
        "options": _LLM_OPTIONS,
        "keep_alive": "20m",
    }
    try:
        resp = httpx.post(f"{ollama_url}/api/generate", json=payload, timeout=timeout)
        resp.raise_for_status()
        triage_text = _postprocess(resp.json().get("response", "").strip())
        if triage_text:
            header = f"🩺 <b>Log Anomaly Digest</b> (last {since_minutes}m — {len(top)} clusters)\n\n"
            return header + triage_text
    except Exception as exc:
        logger.warning("log_spotter: LLM triage failed (%s), using plain digest", exc)
    return _plain_digest(top, since_minutes)


# ── Main run logic ────────────────────────────────────────────────────────────

async def _run(context, plugin_cfg: dict) -> None:
    since_minutes = int(plugin_cfg.get("since_minutes", 15))
    max_clusters = int(plugin_cfg.get("max_clusters", 15))
    cooldown_minutes = int(plugin_cfg.get("cooldown_minutes", 30))
    state_path = plugin_cfg.get("state_path", "/data/log_spotter_state.json")
    ollama_url = os.environ.get(plugin_cfg.get("ollama_url_env", "OLLAMA_URL"), "")
    model = os.environ.get(plugin_cfg.get("model_env", "LOG_SPOTTER_MODEL"), "qwen2.5:0.5b-instruct")
    llm_timeout = int(plugin_cfg.get("llm_timeout", 60))

    clusters = await asyncio.to_thread(_collect_clusters, since_minutes)
    if not clusters:
        logger.debug("log_spotter: no error clusters found")
        return

    state = await asyncio.to_thread(_load_state, state_path)

    fresh = [
        (sh, cl) for sh, cl in clusters.items()
        if not _recently_alerted(state, sh, cooldown_minutes)
    ]
    if not fresh:
        logger.debug("log_spotter: %d clusters but all within cooldown", len(clusters))
        return

    top = sorted(fresh, key=lambda x: x[1]["count"], reverse=True)[:max_clusters]

    if ollama_url:
        body = await asyncio.to_thread(
            _llm_triage, top, since_minutes, ollama_url, model, llm_timeout
        )
    else:
        logger.warning("log_spotter: OLLAMA_URL not set, using plain digest")
        body = _plain_digest(top, since_minutes)

    key_bucket = math.floor(datetime.now(timezone.utc).timestamp() / (cooldown_minutes * 60))
    put_alert(AlertItem(
        type=AlertType.HOST_RESOURCE,
        title="🩺 Log Anomaly Digest",
        body=body,
        key=f"log_spotter:{key_bucket}",
    ))
    logger.info("log_spotter: emitted digest for %d fresh clusters", len(top))

    for sh, cl in top:
        _mark_alerted(state, sh, cl["container"], cl["count"])
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    await asyncio.to_thread(_save_state, state_path, state)


# ── On-demand button handler ──────────────────────────────────────────────────

async def _handle_action(query, parts, ctx: "PluginContext") -> None:
    sub = parts[1] if len(parts) > 1 else "menu"

    if sub == "menu":
        state = await asyncio.to_thread(_load_state, ctx.plugin_cfg.get("state_path", "/data/log_spotter_state.json"))
        last_run = state.get("last_run", "never")
        seen_count = len(state.get("seen", {}))
        sch = ctx.plugin_cfg.get("schedule", "not configured")
        text = (
            "🩺 <b>Log Anomaly Spotter</b>\n\n"
            f"Schedule: <code>{sch}</code>\n"
            f"Last run: <b>{last_run[:19] if last_run != 'never' else 'never'}</b>\n"
            f"Tracked signatures: <b>{seen_count}</b>\n\n"
            "Tap <b>Run now</b> to scan logs immediately."
        )
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Run now", callback_data=_CB_RUN)],
                [InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS)],
            ]),
        )

    elif sub == "run":
        await query.edit_message_text("⏳ Scanning logs…")
        try:
            await _run(None, ctx.plugin_cfg)
            await query.edit_message_text(
                "✅ Log scan complete. Check alerts above.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Back", callback_data=_CB_MENU),
                ]]),
            )
        except Exception as exc:
            await query.edit_message_text(
                f"❌ Scan failed: <code>{exc}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS),
                ]]),
            )


# ── Plugin registration ───────────────────────────────────────────────────────

def register(ctx: "PluginContext") -> None:
    plugin_cfg = ctx.plugin_cfg

    async def _cron_run(context) -> None:
        await _run(context, plugin_cfg)

    async def _warmup(context) -> None:
        """Fire a cheap prompt at startup to load the model before the first cron fires."""
        ollama_url = os.environ.get(plugin_cfg.get("ollama_url_env", "OLLAMA_URL"), "")
        model = os.environ.get(plugin_cfg.get("model_env", "LOG_SPOTTER_MODEL"), "qwen2.5:0.5b-instruct")
        if not ollama_url:
            return
        try:
            import httpx
            await asyncio.to_thread(
                httpx.post,
                f"{ollama_url}/api/generate",
                json={"model": model, "system": _SYSTEM_PROMPT,
                      "prompt": "Input:\nping|x1|ok\n\nOutput:\n", "stream": False,
                      "options": {**_LLM_OPTIONS, "num_predict": 1},
                      "keep_alive": "20m"},
                timeout=120,
            )
            logger.info("log_spotter: model warm-up complete (%s)", model)
        except Exception as exc:
            logger.warning("log_spotter: warm-up failed (non-fatal): %s", exc)

    ctx.actions.register("p.log_spotter", _handle_action)
    ctx.buttons.add("🩺 Log Digest", _CB_MENU, sort_key=44)

    # Warm up the model ~10s after bot start (PTB fires `first=10`), then daily.
    # Ensures the first cron doesn't hit a cold load.
    ctx.scheduler.every(86400, _warmup, "log_spotter.warmup")

    if sch := plugin_cfg.get("schedule"):
        ctx.scheduler.cron(str(sch), _cron_run, "log_spotter.run")
        logger.info(
            "log_spotter: registered (schedule=%s, since=%sm, max=%s clusters)",
            sch,
            plugin_cfg.get("since_minutes", 15),
            plugin_cfg.get("max_clusters", 15),
        )
    else:
        logger.info("log_spotter: no schedule configured — manual-only mode")
