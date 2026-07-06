"""
Speedtest Outlier Detector plugin.

Runs at 5 * * * * (5 minutes after the hourly speedtest) to score the newest
result using IsolationForest trained on the last N results. No LLM needed —
pure sklearn/numpy anomaly detection on 7 numeric features.

Data source: speedtest-tracker SQLite at /config/database.sqlite inside the
'speedtest-tracker' container, fetched via docker get_archive each run.

Features: download_mbps, upload_mbps, ping_ms, jitter_ms, packet_loss,
          hour_sin, hour_cos  (cyclic-encoded hour-of-day).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import os
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import docker
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from alerts import AlertItem, AlertType
from alerts.notifier import put_alert
from plugins._ctx import PluginMeta

if TYPE_CHECKING:
    from plugins._ctx import PluginContext

logger = logging.getLogger(__name__)

META = PluginMeta(
    name="speedtest_outlier",
    description="IsolationForest outlier detection on speedtest-tracker results",
    default_config={
        "schedule": "5 * * * *",
        "window": 200,
        "contamination": 0.05,
        "container": "speedtest-tracker",
        "db_path": "/config/database.sqlite",
        "state_path": "/data/speedtest_outlier_state.json",
    },
)

_CB_MENU = "p.speedtest_outlier:menu"
_CB_RUN = "p.speedtest_outlier:run"
_CB_PLUGINS = "plugins_menu"


@dataclass
class SpeedRow:
    id: int
    download_mbps: float
    upload_mbps: float
    ping_ms: float
    jitter_ms: float
    packet_loss: float
    created_at: str


# ── State helpers ────────────────────────────────────────────────────────────

def _load_state(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("speedtest_outlier: state load failed: %s", exc)
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
        logger.error("speedtest_outlier: state save failed: %s", exc)


# ── Database helpers ─────────────────────────────────────────────────────────

def _fetch_db_bytes(container_name: str, db_path: str) -> bytes:
    """Pull the sqlite file out of the container as raw bytes."""
    client = docker.from_env()
    container = client.containers.get(container_name)
    bits, _ = container.get_archive(db_path)
    buf = io.BytesIO()
    for chunk in bits:
        buf.write(chunk)
    buf.seek(0)
    with tarfile.open(fileobj=buf) as tf:
        member = tf.getmembers()[0]
        f = tf.extractfile(member)
        if f is None:
            raise RuntimeError(f"Empty archive from {container_name}:{db_path}")
        return f.read()


def _query_rows(db_bytes: bytes, window: int) -> list[SpeedRow]:
    """Write db to a temp file, query, return SpeedRow list newest-first."""
    rows: list[SpeedRow] = []
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp.write(db_bytes)
        tmp_path = tmp.name
    try:
        conn = sqlite3.connect(tmp_path)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, download, upload, ping, data, created_at
                FROM results
                WHERE status = 'completed'
                ORDER BY id DESC
                LIMIT ?
                """,
                (window,),
            )
            for row in cur.fetchall():
                rid, dl, ul, ping, data_json, created_at = row
                jitter = 0.0
                packet_loss = 0.0
                if data_json:
                    try:
                        d = json.loads(data_json)
                        jitter = float(d.get("ping", {}).get("jitter", 0) or 0)
                        packet_loss = float(d.get("packetLoss", 0) or 0)
                    except Exception:
                        pass
                # download/upload are in bits/s
                rows.append(SpeedRow(
                    id=rid,
                    download_mbps=float(dl or 0) / 1e6,
                    upload_mbps=float(ul or 0) / 1e6,
                    ping_ms=float(ping or 0),
                    jitter_ms=jitter,
                    packet_loss=packet_loss,
                    created_at=str(created_at or ""),
                ))
        finally:
            conn.close()
    finally:
        os.unlink(tmp_path)
    return rows


# ── Feature engineering ───────────────────────────────────────────────────────

def _featurize(rows: list[SpeedRow]):
    """
    Returns (X_scaled, rows_ordered) where rows_ordered[0] is the latest result.
    Rows come in newest-first from the query; IsolationForest fits on all,
    scores on [0] (latest).
    """
    import numpy as np
    from sklearn.preprocessing import StandardScaler

    X_raw = []
    for r in rows:
        try:
            dt = datetime.fromisoformat(r.created_at.replace("Z", "+00:00"))
            hour = dt.hour + dt.minute / 60.0
        except Exception:
            hour = 12.0
        X_raw.append([
            r.download_mbps,
            r.upload_mbps,
            r.ping_ms,
            r.jitter_ms,
            r.packet_loss,
            math.sin(2 * math.pi * hour / 24),
            math.cos(2 * math.pi * hour / 24),
        ])

    X = np.array(X_raw, dtype=float)
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    return X_scaled, rows


def _window_medians(rows: list[SpeedRow]) -> dict:
    """Simple medians (skip the latest point so comparison is fair)."""
    tail = rows[1:] if len(rows) > 1 else rows
    def med(vals):
        s = sorted(v for v in vals if v >= 0)
        if not s:
            return 0.0
        n = len(s)
        return (s[n // 2] + s[(n - 1) // 2]) / 2
    return {
        "download_mbps": med([r.download_mbps for r in tail]),
        "upload_mbps": med([r.upload_mbps for r in tail]),
        "ping_ms": med([r.ping_ms for r in tail]),
        "jitter_ms": med([r.jitter_ms for r in tail]),
        "packet_loss": med([r.packet_loss for r in tail]),
    }


# ── Outlier scoring ───────────────────────────────────────────────────────────

def _score(rows: list[SpeedRow], contamination: float) -> tuple[int, float]:
    """
    Fit IsolationForest on all rows, score rows[0] (latest).
    Returns (prediction, anomaly_score): prediction==-1 means outlier.
    """
    from sklearn.ensemble import IsolationForest

    X_scaled, ordered = _featurize(rows)
    clf = IsolationForest(
        n_estimators=100,
        contamination=contamination,
        random_state=42,
    )
    clf.fit(X_scaled)
    pred = int(clf.predict(X_scaled[:1])[0])
    score = float(clf.score_samples(X_scaled[:1])[0])
    return pred, score


# ── Alert body formatter ──────────────────────────────────────────────────────

def _fmt_alert(latest: SpeedRow, medians: dict, score: float) -> str:
    def _pct_diff(val, med):
        if med <= 0:
            return ""
        diff = (val - med) / med * 100
        arrow = "⬇️" if diff < 0 else "⬆️"
        return f" {arrow} {abs(diff):.0f}% vs median"

    dl_diff = _pct_diff(latest.download_mbps, medians["download_mbps"])
    ul_diff = _pct_diff(latest.upload_mbps, medians["upload_mbps"])
    ping_flag = " ⚠️" if latest.ping_ms > medians["ping_ms"] * 1.5 else ""
    jitter_flag = " ⚠️" if latest.jitter_ms > medians["jitter_ms"] * 2 else ""

    ts = latest.created_at[:16] if latest.created_at else "?"
    return (
        f"📡 <b>Speedtest anomaly — run #{latest.id} @ {ts}</b>\n\n"
        f"⬇️ Download: <b>{latest.download_mbps:.1f} Mbps</b>"
        f" (median {medians['download_mbps']:.1f}){dl_diff}\n"
        f"⬆️ Upload:   <b>{latest.upload_mbps:.1f} Mbps</b>"
        f" (median {medians['upload_mbps']:.1f}){ul_diff}\n"
        f"📶 Ping: <b>{latest.ping_ms:.1f} ms</b>"
        f" (median {medians['ping_ms']:.1f}){ping_flag}\n"
        f"〰️ Jitter: <b>{latest.jitter_ms:.1f} ms</b>"
        f" (median {medians['jitter_ms']:.1f}){jitter_flag}\n"
        f"📉 Packet loss: <b>{latest.packet_loss:.1f}%</b>\n\n"
        f"Isolation score: <code>{score:.3f}</code> (outlier detected)"
    )


# ── Main run logic ────────────────────────────────────────────────────────────

def _run_sync(plugin_cfg: dict) -> Optional[str]:
    """
    Synchronous worker — called via asyncio.to_thread.
    Returns alert body string if an outlier is found, else None.
    """
    container = plugin_cfg.get("container", "speedtest-tracker")
    db_path = plugin_cfg.get("db_path", "/config/database.sqlite")
    window = int(plugin_cfg.get("window", 200))
    contamination = float(plugin_cfg.get("contamination", 0.05))
    state_path = plugin_cfg.get("state_path", "/data/speedtest_outlier_state.json")

    try:
        db_bytes = _fetch_db_bytes(container, db_path)
    except Exception as exc:
        logger.error("speedtest_outlier: fetch db failed: %s", exc)
        return None

    rows = _query_rows(db_bytes, window)
    if len(rows) < 30:
        logger.info("speedtest_outlier: only %d rows, need 30 minimum", len(rows))
        return None

    state = _load_state(state_path)
    latest = rows[0]
    if latest.id == state.get("last_scored_id"):
        logger.debug("speedtest_outlier: row #%d already scored, skip", latest.id)
        return None

    try:
        pred, score = _score(rows, contamination)
    except Exception as exc:
        logger.error("speedtest_outlier: scoring failed: %s", exc)
        state["last_scored_id"] = latest.id
        _save_state(state_path, state)
        return None

    state["last_scored_id"] = latest.id
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    _save_state(state_path, state)

    if pred != -1:
        logger.debug("speedtest_outlier: run #%d within normal range (score=%.3f)", latest.id, score)
        return None

    medians = _window_medians(rows)
    body = _fmt_alert(latest, medians, score)
    logger.info("speedtest_outlier: outlier detected — run #%d, score=%.3f", latest.id, score)
    return body


async def _run(context, plugin_cfg: dict) -> None:
    body = await asyncio.to_thread(_run_sync, plugin_cfg)
    if body is None:
        return
    state_path = plugin_cfg.get("state_path", "/data/speedtest_outlier_state.json")
    state = await asyncio.to_thread(_load_state, state_path)
    row_id = state.get("last_scored_id", "?")
    put_alert(AlertItem(
        type=AlertType.HOST_RESOURCE,
        title="📡 Speedtest anomaly",
        body=body,
        key=f"speedtest_outlier:{row_id}",
    ))


# ── On-demand button handler ──────────────────────────────────────────────────

async def _handle_action(query, parts, ctx: "PluginContext") -> None:
    sub = parts[1] if len(parts) > 1 else "menu"

    if sub == "menu":
        state = await asyncio.to_thread(
            _load_state, ctx.plugin_cfg.get("state_path", "/data/speedtest_outlier_state.json")
        )
        last_run = state.get("last_run", "never")
        last_id = state.get("last_scored_id", "none")
        sch = ctx.plugin_cfg.get("schedule", "not configured")
        text = (
            "📡 <b>Speedtest Outlier Detector</b>\n\n"
            f"Schedule: <code>{sch}</code>\n"
            f"Last run: <b>{last_run[:19] if last_run != 'never' else 'never'}</b>\n"
            f"Last scored row: <b>#{last_id}</b>\n\n"
            "Tap <b>Run now</b> to score the latest speedtest result."
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
        await query.edit_message_text("⏳ Scoring latest speedtest result…")
        try:
            body = await asyncio.to_thread(_run_sync, ctx.plugin_cfg)
            if body:
                state_path = ctx.plugin_cfg.get("state_path", "/data/speedtest_outlier_state.json")
                state = await asyncio.to_thread(_load_state, state_path)
                row_id = state.get("last_scored_id", "?")
                put_alert(AlertItem(
                    type=AlertType.HOST_RESOURCE,
                    title="📡 Speedtest anomaly",
                    body=body,
                    key=f"speedtest_outlier:manual:{row_id}",
                ))
                await query.edit_message_text(
                    "⚠️ Outlier detected — alert sent.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀️ Back", callback_data=_CB_MENU),
                    ]]),
                )
            else:
                await query.edit_message_text(
                    "✅ Latest speedtest result is within normal range.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀️ Back", callback_data=_CB_MENU),
                    ]]),
                )
        except Exception as exc:
            await query.edit_message_text(
                f"❌ Check failed: <code>{exc}</code>",
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

    ctx.actions.register("p.speedtest_outlier", _handle_action)
    ctx.buttons.add("📡 Speedtest Check", _CB_MENU, sort_key=45)

    if sch := plugin_cfg.get("schedule"):
        ctx.scheduler.cron(str(sch), _cron_run, "speedtest_outlier.run")
        logger.info(
            "speedtest_outlier: registered (schedule=%s, window=%s, contamination=%s)",
            sch,
            plugin_cfg.get("window", 200),
            plugin_cfg.get("contamination", 0.05),
        )
    else:
        logger.info("speedtest_outlier: no schedule configured — manual-only mode")
