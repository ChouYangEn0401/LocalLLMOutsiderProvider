"""``python -m llmop ...`` is the same as the ``llmop`` command."""

import sys

from .cli import main

sys.exit(main())
