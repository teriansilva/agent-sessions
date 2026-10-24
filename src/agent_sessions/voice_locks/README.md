# Voice locks (#1397 P4)

`agent-sessions voice setup` installs the call runtime and the local speech server from these
files with `pip install --require-hashes --no-deps -r <kind>.txt`. That way every package, including
the transitive ones, is the exact file that was locked, and no resolver runs on the operator's machine.

The `.in` files are the top-level pins. To regenerate:

    uv pip compile --universal --generate-hashes --python-version 3.11 --no-header runtime.in -o runtime.txt
    uv pip compile --universal --generate-hashes --python-version 3.11 --no-header server.in -o server.txt

A changed lock makes a new generation directory (`voice/<kind>/<lock-sha>/`), and `voice status`
reports the old one as stale until setup runs again.
