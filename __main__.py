"""Entry point when this directory itself is run: `python <dir>`.

Running the directory puts it first on the import path, so the `x`
package here is the one imported even when the repository being
worked on has its own `x` folder.
"""

import os
import sys

if sys.version_info < (3, 11):
    sys.stderr.write(
        "x: needs Python 3.11 or newer; this is %d.%d\n" % sys.version_info[:2]
    )
    raise SystemExit(127)

from x.main import main  # noqa: E402

try:
    status = main()
    sys.stdout.flush()
except BrokenPipeError:
    # The reader of our output went away, as `x help | head` does.
    # Point stdout somewhere harmless so the interpreter's own final
    # flush does not report it, and end the way a pipeline expects.
    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    status = 141
raise SystemExit(status)
