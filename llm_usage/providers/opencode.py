"""OpenCode Go quota parsing."""

from ..util import is_finite_number as number

OPENCODE_GO_USAGE_URL = 'https://opencode.ai/zen/go/v1/usage'


def go_limits(store, c, data, checked):
    count = 0
    for key, row in data.get('usage', {}).items():
        # A window the response leaves null does not discard the windows it did report.
        if key in ('rolling', 'weekly', 'monthly') and isinstance(row, dict) and number(row.get('percent')):
            store.limit(
                c, 'opencode-go', key, max(0, 100 - row['percent']), row.get('resetsAt'), checked, 'opencode-go'
            )
            count += 1
    return count
