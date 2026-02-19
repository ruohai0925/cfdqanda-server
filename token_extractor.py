"""
Extract LLM token usage statistics from Foam-Agent simulation logs.

Foam-Agent's LLMService.print_statistics() outputs a block like:

    <LLM Service Statistics>
    Total calls: 7
    Failed calls: 0
    Total retries: 0
    Total prompt tokens: 3147
    Total completion tokens: 15
    Total tokens: 3162
    Average prompt tokens per call: 449.57
    Average completion tokens per call: 2.14
    Average tokens per call: 451.71
    </LLM Service Statistics>

This module parses that block and returns a structured dict.
No external dependencies (no Supabase, no .env) — safe to import in tests.
"""

import re
import logging

logger = logging.getLogger(__name__)

# Markers for the statistics block
_STATS_START = "<LLM Service Statistics>"
_STATS_END = "</LLM Service Statistics>"

# Mapping from log line labels to output dict keys.
# Order doesn't matter; we match any of these in the block.
_FIELD_MAP = {
    "Total calls": "total_calls",
    "Failed calls": "failed_calls",
    "Total retries": "total_retries",
    "Total prompt tokens": "total_prompt_tokens",
    "Total completion tokens": "total_completion_tokens",
    "Total tokens": "total_tokens",
    "Average prompt tokens per call": "avg_prompt_tokens",
    "Average completion tokens per call": "avg_completion_tokens",
    "Average tokens per call": "avg_tokens",
}

# Pre-compiled regex: "Label: value" where value is int or float
_LINE_RE = re.compile(r"^(.+?):\s+([\d.]+)\s*$")


def extract_token_usage(log_path: str) -> dict | None:
    """
    Parse a simulation log file and extract the LLM Service Statistics block.

    Args:
        log_path: Absolute path to the simulation.log file.

    Returns:
        A dict with token usage fields (values are int or float),
        or None if the statistics block is not found or the file
        cannot be read.

    Example return value:
        {
            "total_calls": 7,
            "failed_calls": 0,
            "total_retries": 0,
            "total_prompt_tokens": 3147,
            "total_completion_tokens": 15,
            "total_tokens": 3162,
            "avg_prompt_tokens": 449.57,
            "avg_completion_tokens": 2.14,
            "avg_tokens": 451.71,
        }
    """
    try:
        with open(log_path, "r", errors="replace") as f:
            content = f.read()
    except FileNotFoundError:
        logger.warning(f"Log file not found: {log_path}")
        return None
    except Exception as e:
        logger.warning(f"Failed to read log file {log_path}: {e}")
        return None

    # Find the last statistics block (in case there are multiple runs appended)
    start_idx = content.rfind(_STATS_START)
    if start_idx == -1:
        logger.info(f"No LLM Service Statistics block found in {log_path}")
        return None

    end_idx = content.find(_STATS_END, start_idx)
    if end_idx == -1:
        logger.warning(f"Found start tag but no end tag for statistics in {log_path}")
        return None

    block = content[start_idx + len(_STATS_START):end_idx]
    result = {}

    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        label, value_str = m.group(1).strip(), m.group(2)
        key = _FIELD_MAP.get(label)
        if key is None:
            # Unknown label — skip but don't fail
            continue
        # Parse as int if possible, otherwise float
        if "." in value_str:
            result[key] = float(value_str)
        else:
            result[key] = int(value_str)

    if not result:
        logger.warning(f"Statistics block found but no fields parsed in {log_path}")
        return None

    logger.info(
        f"Extracted token usage from {log_path}: "
        f"{result.get('total_tokens', '?')} total tokens, "
        f"{result.get('total_calls', '?')} calls"
    )
    return result
