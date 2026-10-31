"""List truncation for relay-sized or preview-sized copies of feed data (used by the DVM)."""
from typing import Optional


def truncate(data, limit: int = 10, path: str = "$", cut: Optional[dict] = None):
    """Cut every list in `data` to `limit` items. Returns (data, {json_path: original_len})."""
    cut = {} if cut is None else cut
    if isinstance(data, list):
        if len(data) > limit:
            cut[path] = len(data)
        return [truncate(v, limit, "%s[%d]" % (path, i), cut)[0] for i, v in enumerate(data[:limit])], cut
    if isinstance(data, dict):
        return {k: truncate(v, limit, "%s.%s" % (path, k), cut)[0] for k, v in data.items()}, cut
    return data, cut
