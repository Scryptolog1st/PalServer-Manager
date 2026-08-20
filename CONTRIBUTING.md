# Contributing

1. Create a feature branch.
2. Keep OS-specific behavior behind the service/transport abstractions.
3. Do not add code that exposes Palworld's REST API directly to the Internet.
4. Add tests for configuration parsing, backup safety and remote authentication changes.
5. Run `python -m pytest` and `python -m compileall -q palserver_manager`.
6. Test Windows GUI changes on Windows and headless/server changes on Linux.

New Palworld settings should work even before metadata is added; add a human-readable description when practical.
