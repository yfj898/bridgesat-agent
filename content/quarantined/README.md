# Quarantined content

Content here is **not** loadable by any production API or PWA path.

`starter-questions-v0.json` contains the original 6 skeleton questions used
only by the early prototype. They lack version, source, license snapshot,
reviewer, misconception mapping, and content hash fields required by the
published item schema (`content/schemas/item-v1.json`). They must not be
reloaded as student content; the content pipeline in `scripts/` must not read
from this directory.
